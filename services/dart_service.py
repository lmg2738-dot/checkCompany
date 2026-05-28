# -*- coding: utf-8 -*-
"""
금융감독원 전자공시 Open DART 연동.

공식 개발가이드 기준 사용 API:
- 고유번호: GET /api/corpCode.xml — 공시대상 고유번호·회사명·종목코드
- 기업개황: GET /api/company.json — corp_code 또는 bizr_no(사업자번호)로 조회(환경에 따라 다름)
- 공시검색: GET /api/list.json — corp_code 선택, 기간·유형·페이징
- 단일회사 주요 재무지표: GET /api/fnlttSinglIndx.json — 수익성/안정성 등 지표

문서: https://opendart.fss.or.kr/guide/main.do
"""

from __future__ import annotations

import csv
import io
import os
import re
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from services import ssl_setup

ssl_setup.init_truststore()

import requests

from services.analysis_context import in_analyze_runtime
from services.news_rss import normalize_biz_number

_CACHE_ROOT = Path(__file__).resolve().parent.parent / ".cache"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
# 레거시(영문 헤더). 우선순위는 DART_BIZ_MASTER_CSV.
REGISTRY_CSV = DATA_DIR / "dart_company_registry.csv"
# 사업자번호 → 업체명·공시번호(DART 고유번호 8자리)·종목코드 — 빌드 결과·분석 기준 리스트
DART_BIZ_MASTER_CSV = DATA_DIR / "dart_biz_master_list.csv"
# 프로젝트 고정 사업자번호 입력(빌드 스크립트·웹 분석 공통)
DART_BIZNO_INPUT_LIST = DATA_DIR / "dart_bizno_input_list.txt"

_biz_registry: dict[str, dict[str, str]] | None = None
_registry_mtime: float | None = None

_bizno_input_mtime: float | None = None
_bizno_input_list: list[str] | None = None
_bizno_input_set: frozenset[str] | None = None

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ChurnMonitor/1.0)"})
SESSION.verify = ssl_setup.get_requests_verify()

BASE = "https://opendart.fss.or.kr/api"

# 공시 제목·비고에서 보는 이슈 키워드 (list.json 결과 스캔)
CREDIT_KEYWORDS = [
    "자본잠식",
    "감사의견",
    "의견거절",
    "연결감사",
    "분식",
    "기업회생",
    "파산",
    "상장폐지",
    "관리종목",
    "감사보고서",
    "연결감사보고서",
    "결손",
    "유동성",
    "불확실성",
    "계속기업",
    "허위",
    "분식회계",
]

# 재무지표(idx_nm) 키워드 기반 스트레스 (fnlttSinglIndx.json)
NEGATIVE_FIN_IDX = (
    "부채비율",
    "차입금의존도",
    "이자보상배율",
    "유동비율",
    "당좌비율",
    "ROA",
    "ROE",
    "영업이익률",
    "순이익률",
    "매출액순이익률",
)


def get_dart_api_key() -> str | None:
    return (os.environ.get("DART_API_KEY") or os.environ.get("OPENDART_API_KEY") or "").strip() or None


def _normalize_stock_code(stock: str) -> str:
    """숫자만 남겨 6자리 KRX 종목코드 형태로."""
    d = re.sub(r"\D", "", (stock or "").strip())
    if not d:
        return ""
    return d.zfill(6) if len(d) <= 6 else d[-6:]


def normalize_disclosure_corp_code(raw: str | None) -> str:
    """DART 공시대상 고유번호(corp_code) 8자리. 엑셀에서 앞자리 0이 빠진 숫자도 0패딩."""
    if not raw:
        return ""
    s = re.sub(r"\s+", "", str(raw).strip())
    s = re.sub(r"[^\w]", "", s)
    if not s:
        return ""
    if s.isdigit():
        if len(s) <= 8:
            return s.zfill(8)
        return s[:8]
    return s[:8] if len(s) >= 8 else ""


def parse_biz_master_csv_row(row: dict) -> tuple[str, dict[str, str]] | None:
    key = normalize_biz_number(
        row.get("사업자번호") or row.get("business_number") or row.get("사업자등록번호") or ""
    )
    if len(key) != 10:
        return None
    cc = normalize_disclosure_corp_code(
        row.get("공시번호") or row.get("corp_code") or row.get("고유번호") or row.get("dart_corp_code") or ""
    )
    name = (row.get("업체명") or row.get("corp_name") or row.get("기업명") or "").strip()
    stock_raw = (row.get("종목코드") or row.get("stock_code") or "").strip()
    stock6 = _normalize_stock_code(stock_raw)
    if len(cc) == 8:
        return key, {"corp_name": name, "corp_code": cc, "stock_code": stock6 or stock_raw}
    # 공시번호가 비었거나(또는 엑셀 깨짐) 8자리가 아니어도, 사업자+종목/상호가 있으면 레지스트리에 넣어 분석 시 보강
    if name or (stock6 and len(stock6) == 6):
        return key, {"corp_name": name, "corp_code": "", "stock_code": stock6 or stock_raw}
    return None


def _biz_registry_file_stamp() -> float | None:
    t: list[float] = []
    for p in (DART_BIZ_MASTER_CSV, REGISTRY_CSV):
        try:
            t.append(p.stat().st_mtime)
        except OSError:
            pass
    return max(t) if t else None


def load_biz_registry() -> dict[str, dict[str, str]]:
    """마스터 CSV(dart_biz_master_list) + 레거시(dart_company_registry). 키: 10자리 사업자번호."""
    global _biz_registry, _registry_mtime
    mt = _biz_registry_file_stamp()
    if _biz_registry is not None and mt == _registry_mtime:
        return _biz_registry
    out: dict[str, dict[str, str]] = {}

    def _merge_entry(key: str, entry: dict[str, str]) -> None:
        if key not in out:
            out[key] = {
                "corp_name": entry.get("corp_name") or "",
                "corp_code": entry.get("corp_code") or "",
                "stock_code": entry.get("stock_code") or "",
            }
            return
        cur = out[key]
        if not (cur.get("corp_code") or "").strip() and (entry.get("corp_code") or "").strip():
            cur["corp_code"] = entry["corp_code"].strip()
        if not (cur.get("stock_code") or "").strip() and (entry.get("stock_code") or "").strip():
            cur["stock_code"] = entry["stock_code"].strip()
        if not (cur.get("corp_name") or "").strip() and (entry.get("corp_name") or "").strip():
            cur["corp_name"] = entry["corp_name"].strip()

    for path in (DART_BIZ_MASTER_CSV, REGISTRY_CSV):
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    parsed = parse_biz_master_csv_row(row)
                    if not parsed:
                        continue
                    key, entry = parsed
                    _merge_entry(key, entry)
        except OSError:
            pass
    _biz_registry = out
    _registry_mtime = mt
    return out


def registry_lookup_biz(business_number: str) -> dict[str, str] | None:
    b = normalize_biz_number(business_number)
    if len(b) != 10:
        return None
    return load_biz_registry().get(b)


def load_bizno_input_numbers() -> list[str]:
    """
    data/dart_bizno_input_list.txt — 한 줄에 하나의 사업자번호(# 주석 가능).
    순서 유지·중복 제거. mtime 변경 시 다시 읽습니다.
    """
    global _bizno_input_mtime, _bizno_input_list, _bizno_input_set
    path = DART_BIZNO_INPUT_LIST
    try:
        mt = path.stat().st_mtime
    except OSError:
        _bizno_input_mtime = None
        _bizno_input_list = []
        _bizno_input_set = frozenset()
        return []

    if _bizno_input_list is not None and mt == _bizno_input_mtime:
        return _bizno_input_list

    text = path.read_text(encoding="utf-8")
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        b = normalize_biz_number(s)
        if len(b) != 10:
            continue
        if b not in seen:
            seen.add(b)
            out.append(b)

    _bizno_input_list = out
    _bizno_input_set = frozenset(out)
    _bizno_input_mtime = mt
    return out


def dart_master_stock_gap_stats(master_csv: Path | None = None) -> dict[str, int]:
    """
    마스터 CSV + dart_bizno_input_list 기준 종목코드 공란 통계(API 없음).
    `python build_dart_registry.py --list-incomplete` 와 같은 정의입니다.
    """
    path = master_csv if master_csv is not None else DART_BIZ_MASTER_CSV
    ordered = load_bizno_input_numbers()
    by_biz: dict[str, dict[str, str]] = {}
    if path.is_file():
        try:
            with path.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    key = normalize_biz_number(row.get("사업자번호") or row.get("business_number") or "")
                    if len(key) != 10:
                        continue
                    stk = (
                        row.get("종목코드") or row.get("stock_code") or ""
                    ).strip()
                    by_biz[key] = {
                        "업체명": (row.get("업체명") or row.get("corp_name") or "").strip(),
                        "종목코드": stk,
                    }
        except OSError:
            pass
    missing_for_input_order = sum(
        1
        for b in ordered
        if not (
            ((by_biz.get(b) or {}).get("종목코드") or "").strip()
        )
    )
    master_rows_empty_stock = sum(
        1 for _b, r in by_biz.items() if not (r.get("종목코드") or "").strip()
    )
    return {
        "input_total": len(ordered),
        "input_missing_stock": missing_for_input_order,
        "master_rows_empty_stock": master_rows_empty_stock,
        "master_rows_total": len(by_biz),
    }


def dart_bizno_input_customer_rows() -> list[dict[str, Any]]:
    """웹/CLI: dart_bizno_input_list 순서로, 마스터 리스트에 있으면 업체명·공시번호·종목까지 채움."""
    reg = load_biz_registry()
    rows: list[dict[str, Any]] = []
    for b in load_bizno_input_numbers():
        hit = reg.get(b)
        sc = _normalize_stock_code((hit.get("stock_code") or "") if hit else "") or None
        cc_raw = (hit.get("corp_code") or "").strip() if hit else ""
        cc_norm = normalize_disclosure_corp_code(cc_raw) if cc_raw else ""
        dart_cc = cc_norm if len(cc_norm) == 8 else None
        rows.append(
            {
                "company_name": (hit.get("corp_name") if hit else "") or "",
                "business_number": b,
                "ticker_override": sc,
                "dart_corp_code": dart_cc,
            }
        )
    return rows


def biz_in_dart_input_list(business_number: str) -> bool:
    """사업자번호가 dart_bizno_input_list.txt 에 등록돼 있는지."""
    b = normalize_biz_number(business_number)
    if len(b) != 10:
        return False
    load_bizno_input_numbers()
    return bool(_bizno_input_set and b in _bizno_input_set)


def _input_list_build_hint() -> str:
    return (
        " — data/dart_bizno_input_list.txt 등록 번호입니다. "
        "이미 공시대상 스캔(DART_RUNTIME_BIZ_SCAN_MAX)까지 시도했습니다. "
        "`python build_dart_registry.py` 로 data/dart_biz_master_list.csv(사업자번호·업체명·공시번호)를 "
        "넉넉히 생성하거나, DART_RUNTIME_BIZ_SCAN_MAX 를 늘려 보세요."
    )


def _writable_cache_dir() -> Path:
    for d in (_CACHE_ROOT, Path(tempfile.gettempdir()) / "issue-app-cache"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    return _CACHE_ROOT


def download_corp_code_xml(api_key: str) -> bytes:
    """corpCode.zip. 캐시(corpCode.zip.raw)가 7일 이내면 재다운로드 생략."""
    cache_dir = _writable_cache_dir()
    cache_file = cache_dir / "corpCode.zip.raw"
    max_age = 86400 * 7
    ignore_cache = os.environ.get("DART_CORPCODE_IGNORE_CACHE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if cache_file.is_file() and not ignore_cache:
        try:
            if time.time() - cache_file.stat().st_mtime < max_age:
                return cache_file.read_bytes()
        except OSError:
            pass
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            r = SESSION.get(f"{BASE}/corpCode.xml", params={"crtfc_key": api_key}, timeout=120)
            r.raise_for_status()
            raw = r.content
            cache_file.write_bytes(raw)
            return raw
        except Exception as e:
            last_err = e
            if cache_file.is_file():
                try:
                    return cache_file.read_bytes()
                except OSError:
                    pass
            time.sleep(1.5 * (attempt + 1))
    raise last_err if last_err else RuntimeError("corpCode.xml 다운로드 실패")


def load_corp_list(api_key: str) -> list[dict]:
    """상장·기타 공시대상 전체(종목코드 없을 수 있음)."""
    raw = download_corp_code_xml(api_key)

    z = zipfile.ZipFile(io.BytesIO(raw))
    xml_name = next((n for n in z.namelist() if n.lower().endswith(".xml")), z.namelist()[0])
    root = ElementTree.fromstring(z.read(xml_name))
    rows: list[dict] = []
    for el in root.findall(".//list"):
        cc = (el.findtext("corp_code") or "").strip()
        if not cc:
            continue
        sc = (el.findtext("stock_code") or "").strip()
        if not sc or sc == " " * len(sc):
            sc = ""
        rows.append(
            {
                "corp_code": cc,
                "corp_name": (el.findtext("corp_name") or "").strip(),
                "stock_code": sc,
            }
        )
    return rows


_corp_cache: list[dict] | None = None
_corp_cache_key: str | None = None


def get_corpus_cached(api_key: str) -> list[dict]:
    global _corp_cache, _corp_cache_key
    if _corp_cache is None or _corp_cache_key != api_key:
        _corp_cache = load_corp_list(api_key)
        _corp_cache_key = api_key
    return _corp_cache


def find_corp_by_stock_code(stock: str | None, corp: list[dict]) -> dict | None:
    """6자리 종목코드로 고유번호 목록에서 1건 탐색."""
    want = _normalize_stock_code(stock or "")
    if len(want) != 6:
        return None
    for r in corp:
        sc = re.sub(r"\D", "", (r.get("stock_code") or "").strip())
        if not sc:
            continue
        if sc.zfill(6) == want:
            return r
    return None


def normalize_corp_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"(주식회사|\(주\)|㈜)", "", s)
    return s


def _name_candidates(company_name: str, corp: list[dict]) -> list[dict]:
    """회사명 유사도 상위 후보(이후 company.json으로 사업자번호 검증)."""
    target = normalize_corp_name(company_name)
    if not target:
        return []

    exact: list[dict] = []
    partial: list[tuple[int, dict]] = []

    for r in corp:
        cn = normalize_corp_name(r["corp_name"])
        if not cn:
            continue
        if target == cn:
            exact.append(r)
        elif len(target) >= 2 and (target in cn or cn in target):
            score = -abs(len(cn) - len(target))
            partial.append((score, r))

    if exact:
        return exact[:15]

    partial.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in partial[:12]]


def _dart_api_success(js: dict[str, Any] | None) -> bool:
    """Open DART JSON 정상 응답(status == '000'). 타입·공백 차이 흡수."""
    if not js or not isinstance(js, dict):
        return False
    st = js.get("status")
    return str(st).strip() == "000"


def fetch_company_json(corp_code: str, api_key: str) -> dict[str, Any] | None:
    """기업개황 API (company.json). 일시 네트워크 오류 시 소수 재시도."""
    js: dict[str, Any] | None = None
    for attempt in range(4):
        try:
            r = SESSION.get(
                f"{BASE}/company.json",
                params={"crtfc_key": api_key, "corp_code": corp_code},
                timeout=25,
            )
            r.raise_for_status()
            js = r.json()
            break
        except Exception:
            if attempt == 3:
                return None
            time.sleep(0.4 * (attempt + 1))
    if not js or not _dart_api_success(js):
        return None
    return js


def fetch_company_json_by_bizr_no(bizr_no: str, api_key: str) -> dict[str, Any] | None:
    """기업개황 company.json — 사업자등록번호(bizr_no)만으로 조회."""
    biz = normalize_biz_number(bizr_no)
    if len(biz) != 10:
        return None
    dashed = f"{biz[:3]}-{biz[3:5]}-{biz[5:]}"
    for param_biz in (biz, dashed):
        try:
            r = SESSION.get(
                f"{BASE}/company.json",
                params={"crtfc_key": api_key, "bizr_no": param_biz},
                timeout=20,
            )
            r.raise_for_status()
            js = r.json()
        except Exception:
            continue
        if _dart_api_success(js):
            return js
    return None


def _runtime_biz_scan_max_calls() -> int:
    raw = (os.environ.get("DART_RUNTIME_BIZ_SCAN_MAX") or "").strip()
    if raw.isdigit():
        return max(100, min(int(raw), 200_000))
    return 12_000


def _corp_row_has_listed_stock(row: dict) -> bool:
    sc = re.sub(r"\D", "", (row.get("stock_code") or "").strip())
    return len(sc) == 6 and sc != "000000"


def resolve_by_corpus_scan_for_biz(api_key: str, user_biz: str) -> tuple[dict, dict[str, Any]] | None:
    """
    company.json 의 bizr_no 단독 파라미터가 실패할 때,
    공시대상 고유번호 목록을 순회하며 기업개황의 사업자번호로 매칭 (build_dart_registry 2단계와 동일).
    호출 상한은 DART_RUNTIME_BIZ_SCAN_MAX (기본 12000).
    """
    biz = normalize_biz_number(user_biz)
    if len(biz) != 10:
        return None
    max_calls = _runtime_biz_scan_max_calls()
    corp = list(get_corpus_cached(api_key))
    corp.sort(key=lambda r: (0 if _corp_row_has_listed_stock(r) else 1, r.get("corp_name") or ""))
    calls = 0
    sleep_s = 0.03
    for c in corp:
        if calls >= max_calls:
            break
        cc = (c.get("corp_code") or "").strip()
        if not cc:
            continue
        prof = fetch_company_json(cc, api_key)
        calls += 1
        if not prof:
            time.sleep(sleep_s)
            continue
        b = normalize_biz_number(str(prof.get("bizr_no") or ""))
        if b == biz:
            row = {
                "corp_code": cc,
                "corp_name": (prof.get("corp_name") or c.get("corp_name") or "").strip(),
                "stock_code": (str(prof.get("stock_code") or c.get("stock_code") or "").strip()),
            }
            return row, prof
        time.sleep(sleep_s)
    return None


def invalidate_biz_registry_cache() -> None:
    global _biz_registry, _registry_mtime
    _biz_registry = None
    _registry_mtime = None


def upsert_dart_biz_master_csv(
    biz: str,
    corp_name: str,
    corp_code: str,
    stock_code: str,
) -> None:
    """스캔·API로 확인된 사업자번호 한 건을 data/dart_biz_master_list.csv 에 반영(병합)."""
    if in_analyze_runtime():
        return
    biz = normalize_biz_number(biz)
    cc = normalize_disclosure_corp_code(corp_code)
    if len(biz) != 10 or len(cc) != 8:
        return
    fieldnames = ["사업자번호", "업체명", "공시번호", "종목코드"]
    rows: list[dict[str, str]] = []
    if DART_BIZ_MASTER_CSV.is_file():
        try:
            with DART_BIZ_MASTER_CSV.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    rows.append({fn: (row.get(fn) or "").strip() for fn in fieldnames})
        except OSError:
            rows = []
    idx: int | None = None
    for i, r in enumerate(rows):
        if normalize_biz_number(r.get("사업자번호", "")) == biz:
            idx = i
            break
    new_row = {
        "사업자번호": biz,
        "업체명": (corp_name or "").strip(),
        "공시번호": cc,
        "종목코드": (stock_code or "").strip(),
    }
    if idx is not None:
        old = rows[idx]
        rows[idx] = {
            "사업자번호": biz,
            "업체명": new_row["업체명"] or old["업체명"],
            "공시번호": new_row["공시번호"] or old["공시번호"],
            "종목코드": new_row["종목코드"] or old["종목코드"],
        }
    else:
        rows.append(new_row)
    DART_BIZ_MASTER_CSV.parent.mkdir(parents=True, exist_ok=True)
    with DART_BIZ_MASTER_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    invalidate_biz_registry_cache()


def _resolve_fail_msg(user_biz: str, msg: str) -> tuple[None, None, str]:
    if len(user_biz) == 10 and biz_in_dart_input_list(user_biz):
        return None, None, msg + _input_list_build_hint()
    return None, None, msg


def resolve_corp_code(
    company_name: str,
    business_number: str,
    api_key: str,
    stock_code: str | None = None,
    corp_code_override: str | None = None,
) -> tuple[dict | None, dict | None, str | None]:
    """
    1) 명시 공시번호(corp_code_override)+사업자번호
    2) 마스터/레지스트리·bizr_no·공시대상 스캔(10자리 사업자번호) — 종목코드보다 우선
    3) 종목코드 보조 매칭(위에서 해결되지 않았을 때)
    4) 회사명 후보 + 기업개황 검증
    """
    user_biz = normalize_biz_number(business_number)
    name_s = (company_name or "").strip()

    cc_hint = normalize_disclosure_corp_code(corp_code_override)
    if in_analyze_runtime() and len(user_biz) == 10 and len(cc_hint) == 8:
        reg = load_biz_registry().get(user_biz) or {}
        stk = _normalize_stock_code((stock_code or reg.get("stock_code") or "").strip())
        row_fast = {
            "corp_code": cc_hint,
            "corp_name": (name_s or reg.get("corp_name") or "").strip(),
            "stock_code": stk or (reg.get("stock_code") or "").strip(),
        }
        prof_fast: dict[str, Any] = {
            "corp_name": row_fast["corp_name"],
            "stock_code": row_fast["stock_code"],
            "corp_code": cc_hint,
            "bizr_no": user_biz,
        }
        return row_fast, prof_fast, None

    # CSV 등에 적힌 공시번호가 오래됐거나 오기입이면 개황이 실패·불일치할 수 있음.
    if len(user_biz) == 10 and len(cc_hint) == 8:
        prof_h = fetch_company_json(cc_hint, api_key)
        if prof_h:
            dart_biz = normalize_biz_number(str(prof_h.get("bizr_no") or ""))
            if not dart_biz or dart_biz == user_biz:
                row_h = {
                    "corp_code": cc_hint,
                    "corp_name": (name_s or (prof_h.get("corp_name") or "")).strip(),
                    "stock_code": (prof_h.get("stock_code") or "").strip(),
                }
                return row_h, prof_h, None
        # prof_h 없음 또는 사업자번호 불일치 → 아래 레지스트리·bizr_no·스캔·종목으로 계속

    if len(user_biz) == 10:
        reg = load_biz_registry().get(user_biz)
        if reg:
            cc = (reg.get("corp_code") or "").strip()
            if cc:
                prof = fetch_company_json(cc, api_key)
                if prof:
                    dart_biz = normalize_biz_number(str(prof.get("bizr_no") or ""))
                    if not dart_biz or dart_biz == user_biz:
                        row = {
                            "corp_code": cc,
                            "corp_name": (reg.get("corp_name") or (prof.get("corp_name") or "")).strip(),
                            "stock_code": (reg.get("stock_code") or (prof.get("stock_code") or "")).strip(),
                        }
                        return row, prof, None
                # corp_code 기반 개황 실패·불일치 시에도 bizr_no 등으로 동일 사업자 복구 시도

        prof_b = fetch_company_json_by_bizr_no(user_biz, api_key)
        if prof_b:
            cc_b = str(prof_b.get("corp_code") or "").strip()
            if cc_b:
                dart_biz = normalize_biz_number(str(prof_b.get("bizr_no") or ""))
                if not dart_biz or dart_biz == user_biz:
                    row = {
                        "corp_code": cc_b,
                        "corp_name": (prof_b.get("corp_name") or "").strip(),
                        "stock_code": (prof_b.get("stock_code") or "").strip(),
                    }
                    try:
                        upsert_dart_biz_master_csv(
                            user_biz,
                            row["corp_name"],
                            row["corp_code"],
                            row.get("stock_code") or "",
                        )
                    except OSError:
                        pass
                    return row, prof_b, None
            if not in_analyze_runtime():
                scanned = resolve_by_corpus_scan_for_biz(api_key, user_biz)
                if scanned:
                    row_s, prof_s = scanned
                    try:
                        upsert_dart_biz_master_csv(
                            user_biz,
                            row_s["corp_name"],
                            row_s["corp_code"],
                            row_s.get("stock_code") or "",
                        )
                    except OSError:
                        pass
                    return row_s, prof_s, "기업개황(bizr_no)에 고유번호 없음 → 공시대상 스캔으로 매칭"
            return _resolve_fail_msg(user_biz, "기업개황(bizr_no) 응답에 고유번호가 없습니다.")

        scanned_b = None if in_analyze_runtime() else resolve_by_corpus_scan_for_biz(api_key, user_biz)
        if scanned_b:
            row_b, prof_b2 = scanned_b
            try:
                upsert_dart_biz_master_csv(
                    user_biz,
                    row_b["corp_name"],
                    row_b["corp_code"],
                    row_b.get("stock_code") or "",
                )
            except OSError:
                pass
            return row_b, prof_b2, None

        if reg and (reg.get("corp_code") or "").strip():
            cc_fb = (reg.get("corp_code") or "").strip()
            row_fb = {
                "corp_code": cc_fb,
                "corp_name": (reg.get("corp_name") or "").strip(),
                "stock_code": (reg.get("stock_code") or "").strip(),
            }
            stub_fb: dict[str, Any] = {
                "corp_code": cc_fb,
                "corp_name": row_fb["corp_name"],
                "stock_code": row_fb["stock_code"],
                "bizr_no": user_biz,
            }
            return (
                row_fb,
                stub_fb,
                "기업개황(company.json) 조회 실패: 레지스트리 공시번호로 공시·재무는 계속합니다.",
            )

    if stock_code and _normalize_stock_code(stock_code):
        corp = get_corpus_cached(api_key)
        by_st = find_corp_by_stock_code(stock_code, corp)
        if by_st:
            prof = fetch_company_json(by_st["corp_code"], api_key)
            if prof:
                dart_biz = normalize_biz_number(str(prof.get("bizr_no") or ""))
                if not user_biz:
                    return by_st, prof, "사업자번호 미입력: 종목코드로 DART 매칭(검증 권장)"
                if not dart_biz:
                    return by_st, prof, "기업개황에 사업자번호 없음(종목코드 매칭만)"
                if dart_biz == user_biz:
                    return by_st, prof, None
                return _resolve_fail_msg(
                    user_biz,
                    "입력 사업자번호가 기업개황(해당 종목)과 다릅니다. 번호·종목을 확인하세요.",
                )
            row_st = {
                "corp_code": by_st["corp_code"],
                "corp_name": (name_s or (by_st.get("corp_name") or "")).strip(),
                "stock_code": (by_st.get("stock_code") or "").strip(),
            }
            profile_stub_st: dict[str, Any] = {
                "corp_code": row_st["corp_code"],
                "corp_name": row_st["corp_name"],
                "stock_code": row_st["stock_code"],
                "bizr_no": user_biz or None,
            }
            return (
                row_st,
                profile_stub_st,
                "기업개황(company.json) 조회 실패(종목 매칭 후): 네트워크·DART 일시 오류 또는 호출 한도 가능. "
                "고유번호로 공시·재무는 계속 시도합니다.",
            )

    if not name_s:
        return _resolve_fail_msg(
            user_biz,
            "회사명이 없고 사업자번호·레지스트리로 DART 매칭도 실패했습니다.",
        )

    corp = get_corpus_cached(api_key)
    cands = _name_candidates(name_s, corp)

    if not cands:
        return _resolve_fail_msg(user_biz, "DART 고유번호 목록에서 회사명 매칭 실패")

    for c in cands[:8]:
        prof = fetch_company_json(c["corp_code"], api_key)
        if not prof:
            continue
        dart_biz = normalize_biz_number(str(prof.get("bizr_no") or ""))
        if user_biz and dart_biz:
            if dart_biz == user_biz:
                return c, prof, None
        elif not user_biz:
            return c, prof, "사업자번호 미입력: 회사명 매칭만 적용(오탐 가능)"

    if user_biz:
        return _resolve_fail_msg(
            user_biz,
            "기업개황 기준 사업자번호 불일치(동명이인·비상장·미등록 가능)",
        )

    c0 = cands[0]
    prof0 = fetch_company_json(c0["corp_code"], api_key)
    if prof0:
        return c0, prof0, "사업자번호 미입력: 첫 후보만 사용(검증 권장)"
    return _resolve_fail_msg(user_biz, "기업개황 조회 실패")


def fetch_disclosure_stress(
    corp_code: str,
    api_key: str,
    *,
    quick: bool = False,
) -> dict[str, Any]:
    """공시검색 list.json — quick: 최근 6개월·50건."""
    end = datetime.now()
    days = 183 if quick else 365
    start = end - timedelta(days=days)
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": start.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_count": 50 if quick else 100,
        "page_no": 1,
        "sort": "date",
        "sort_mth": "desc",
    }
    try:
        r = SESSION.get(
            f"{BASE}/list.json",
            params=params,
            timeout=8 if quick else 25,
        )
        r.raise_for_status()
        js = r.json()
    except Exception as e:
        return {"hits": 0, "boost": 0.12, "sample": [], "error": str(e)}

    if not _dart_api_success(js):
        return {
            "hits": 0,
            "boost": 0.1,
            "sample": [],
            "error": js.get("message", "list.json"),
        }

    hits = 0
    sample: list[str] = []
    for item in js.get("list", []) or []:
        title = (item.get("report_nm") or "") + " " + (item.get("rm") or "")
        matched = False
        for kw in CREDIT_KEYWORDS:
            if kw in title:
                matched = True
                break
        if matched:
            hits += 1
            if len(sample) < 5:
                sample.append((item.get("report_nm") or "")[:80])

    boost = min(0.5, 0.06 * hits + (0.08 if hits else 0))
    return {"hits": hits, "boost": boost, "sample": sample, "error": None, "total_count": js.get("total_count")}


def _parse_idx_val(val: str | None) -> float | None:
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_financial_index_stress(
    corp_code: str,
    api_key: str,
    *,
    quick: bool = False,
) -> dict[str, Any]:
    """
    단일회사 주요 재무지표 fnlttSinglIndx.json
    quick=True: 전년 사업보고서만(일괄 분석용, 최대 2회 HTTP).
    """
    now_y = datetime.now().year
    if quick:
        reprt_codes = ("11011",)
        years = [now_y - 1]
    else:
        reprt_codes = ("11011", "11014", "11012", "11013")
        years = [now_y - 1, now_y - 2, now_y - 3]

    rows_out: list[dict[str, Any]] = []
    for y in years:
        for reprt in reprt_codes:
            block: list[dict[str, Any]] = []
            for idx_cl in ("M210000", "M220000"):
                try:
                    r = SESSION.get(
                        f"{BASE}/fnlttSinglIndx.json",
                        params={
                            "crtfc_key": api_key,
                            "corp_code": corp_code,
                            "bsns_year": str(y),
                            "reprt_code": reprt,
                            "idx_cl_code": idx_cl,
                        },
                        timeout=20,
                    )
                    r.raise_for_status()
                    js = r.json()
                except Exception:
                    continue
                if not _dart_api_success(js):
                    continue
                for row in js.get("list", []) or []:
                    block.append(row)
            if block:
                rows_out = block
                meta = {"bsns_year": y, "reprt_code": reprt}
                stress, detail = _score_fin_rows(rows_out)
                return {
                    "financial_stress": stress,
                    "financial_detail": detail[:12],
                    "financial_meta": meta,
                    "error": None,
                }

    return {
        "financial_stress": 0.12,
        "financial_detail": [],
        "financial_meta": None,
        "error": "재무지표 미조회(비대상·미제출·연도 불일치)",
    }


def _score_fin_rows(rows: list[dict]) -> tuple[float, list[str]]:
    """지표명·값으로 0~0.35 스트레스."""
    stress = 0.0
    detail: list[str] = []
    for row in rows:
        nm = (row.get("idx_nm") or "").strip()
        val = _parse_idx_val(row.get("idx_val"))
        if val is None:
            continue
        if not any(k in nm for k in NEGATIVE_FIN_IDX):
            continue
        detail.append(f"{nm}={val}")

        if "부채비율" in nm and val > 400:
            stress += 0.14
        elif "부채비율" in nm and val > 200:
            stress += 0.08
        elif "이자보상배율" in nm and val < 1.0:
            stress += 0.12
        elif "영업이익률" in nm or "순이익률" in nm or "매출액순이익률" in nm:
            if val < -5:
                stress += 0.12
            elif val < 0:
                stress += 0.06
        elif "ROA" in nm or "ROE" in nm:
            if val < -5:
                stress += 0.1
            elif val < 0:
                stress += 0.05
        elif "유동비율" in nm or "당좌비율" in nm:
            if val < 80:
                stress += 0.06
        elif "차입금의존도" in nm and val > 80:
            stress += 0.06

    return min(0.35, stress), detail


def _dart_enrich_from_corp_code(
    key: str,
    corp_code: str,
    company_name: str,
    stock_code: str | None,
    business_number: str,
) -> dict[str, Any]:
    """공시번호 확정: company.json 생략, 공시·재무(quick)만."""
    user_biz = normalize_biz_number(business_number)
    reg = load_biz_registry().get(user_biz) if len(user_biz) == 10 else None
    name_s = (company_name or (reg or {}).get("corp_name") or "").strip()
    stk = _normalize_stock_code(
        (stock_code or (reg or {}).get("stock_code") or "").strip()
    )
    stock_raw = stk or ""
    listed = bool(stock_raw and stock_raw != "000000")
    quick = in_analyze_runtime()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_disc = pool.submit(fetch_disclosure_stress, corp_code, key, quick=quick)
        fut_fin = pool.submit(fetch_financial_index_stress, corp_code, key, quick=quick)
        disc = fut_disc.result()
        fin = fut_fin.result()

    boost = min(
        0.55,
        float(disc.get("boost") or 0) + float(fin.get("financial_stress") or 0),
    )
    profile = {
        "corp_name": name_s or None,
        "stock_name": None,
        "stock_code": stock_raw or None,
        "ceo_nm": None,
        "corp_cls": None,
        "bizr_no": user_biz or None,
        "jurir_no": None,
        "adres": None,
        "est_dt": None,
        "hm_url": None,
    }
    out_msg = None
    if disc.get("error"):
        out_msg = str(disc["error"])
    if fin.get("error") and not fin.get("financial_detail"):
        out_msg = (out_msg + "; " if out_msg else "") + str(fin["error"])
    return {
        "dart_available": True,
        "listed": listed,
        "corp_code": corp_code,
        "stock_code": stock_raw or None,
        "company_profile": profile,
        "disclosure_hits": disc.get("hits"),
        "disclosure_sample": disc.get("sample"),
        "financial_detail": fin.get("financial_detail"),
        "financial_meta": fin.get("financial_meta"),
        "credit_risk_boost": round(boost, 4),
        "financial_stress": fin.get("financial_stress"),
        "message": out_msg,
    }


def dart_enrich(
    company_name: str,
    business_number: str,
    stock_code: str | None = None,
    corp_code_override: str | None = None,
) -> dict[str, Any]:
    key = get_dart_api_key()
    if not key:
        return {
            "dart_available": False,
            "listed": False,
            "corp_code": None,
            "stock_code": None,
            "company_profile": None,
            "credit_risk_boost": 0.0,
            "financial_stress": 0.0,
            "message": None,
        }

    cc_fast = normalize_disclosure_corp_code(corp_code_override)
    if in_analyze_runtime() and len(cc_fast) == 8:
        return _dart_enrich_from_corp_code(
            key,
            cc_fast,
            company_name,
            stock_code,
            business_number,
        )

    row, prof, msg = resolve_corp_code(
        company_name,
        business_number,
        key,
        stock_code=stock_code,
        corp_code_override=corp_code_override,
    )
    if not row or not prof:
        hint = _normalize_stock_code(stock_code or "")
        listed_hint = bool(hint and len(hint) == 6 and hint != "000000")
        extra = ""
        if msg and ("한도" in msg or "013" in msg or "limit" in msg.lower()):
            extra = " (DART 호출 한도·일시 오류 가능)"
        return {
            "dart_available": True,
            "listed": listed_hint,
            "corp_code": None,
            "stock_code": hint or None,
            "company_profile": None,
            "credit_risk_boost": 0.08 if msg else 0.0,
            "financial_stress": 0.0,
            "message": (msg or "매칭 실패") + extra,
        }

    corp_code = row["corp_code"]
    stock_raw = (prof.get("stock_code") or row.get("stock_code") or "").strip()
    listed = bool(stock_raw and stock_raw.replace("0", ""))

    disc = fetch_disclosure_stress(corp_code, key)
    fin = fetch_financial_index_stress(corp_code, key)

    boost = min(
        0.55,
        float(disc.get("boost") or 0) + float(fin.get("financial_stress") or 0),
    )

    profile = {
        "corp_name": prof.get("corp_name"),
        "stock_name": prof.get("stock_name"),
        "stock_code": stock_raw or None,
        "ceo_nm": prof.get("ceo_nm"),
        "corp_cls": prof.get("corp_cls"),
        "bizr_no": prof.get("bizr_no"),
        "jurir_no": prof.get("jurir_no"),
        "adres": (prof.get("adres") or "")[:120] or None,
        "est_dt": prof.get("est_dt"),
        "hm_url": prof.get("hm_url"),
    }

    out_msg = msg
    if disc.get("error"):
        out_msg = (out_msg + "; " if out_msg else "") + str(disc["error"])
    if fin.get("error") and not fin.get("financial_detail"):
        out_msg = (out_msg + "; " if out_msg else "") + str(fin["error"])

    return {
        "dart_available": True,
        "listed": listed,
        "corp_code": corp_code,
        "stock_code": stock_raw or None,
        "company_profile": profile,
        "disclosure_hits": disc.get("hits"),
        "disclosure_sample": disc.get("sample"),
        "financial_detail": fin.get("financial_detail"),
        "financial_meta": fin.get("financial_meta"),
        "credit_risk_boost": round(boost, 4),
        "financial_stress": fin.get("financial_stress"),
        "message": out_msg,
    }
