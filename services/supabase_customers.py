# -*- coding: utf-8 -*-
"""
Supabase public.customers — PostgREST + requests (supabase 패키지 불필요).

Vercel 서버리스에서 supabase-py 번들 누락을 피하기 위해 REST API만 사용합니다.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from typing import Any

import requests

from services.dart_service import normalize_disclosure_corp_code
from services.env_config import bootstrap_env
from services.news_rss import normalize_biz_number

bootstrap_env()

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ChurnMonitor/1.0)"})

_rows_cache: list[dict[str, Any]] | None = None
_rows_cache_at: float = 0.0
_CACHE_TTL_SEC = 300.0
_PAGE_SIZE = 1000
_SELECT = "business_number,company_name,disclosure_corp_code,stock_code"
_SELECT_WITH_CREATED = f"{_SELECT},created_at"


def _supabase_url() -> str:
    return (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")


def _supabase_key() -> str:
    return (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_PUBLISHABLE_KEY")
        or ""
    ).strip()


def is_configured() -> bool:
    return bool(_supabase_url() and _supabase_key())


def config_status() -> dict[str, Any]:
    """배포 확인용(키 값은 노출하지 않음)."""
    url = _supabase_url()
    key = _supabase_key()
    return {
        "supabase_url_set": bool(url),
        "supabase_key_set": bool(key),
        "supabase_ready": bool(url and key),
        "transport": "postgrest+requests",
    }


def invalidate_customer_cache() -> None:
    global _rows_cache, _rows_cache_at
    _rows_cache = None
    _rows_cache_at = 0.0


def _rest_headers(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    key = _supabase_key()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _rest_base() -> str:
    url = _supabase_url()
    if not url:
        raise RuntimeError("SUPABASE_URL 이 설정되지 않았습니다.")
    return f"{url}/rest/v1"


def is_missing_disclosure_code(raw: str | None) -> bool:
    cc = normalize_disclosure_corp_code(raw or "")
    return len(cc) != 8


def is_biz_number_only_row(row: dict[str, Any]) -> bool:
    """사업자번호만 있고 업체명·공시·종목이 비어 있는 행."""
    biz = normalize_biz_number(str(row.get("business_number") or ""))
    if len(biz) != 10:
        return False
    if is_missing_disclosure_code(str(row.get("disclosure_corp_code") or "")):
        pass
    else:
        return False
    name = (str(row.get("company_name") or "")).strip()
    stock = (str(row.get("stock_code") or "")).strip()
    return not name and not stock


def fetch_all_customer_rows(
    *,
    force_refresh: bool = False,
    order: str = "business_number.asc",
    select: str = _SELECT,
) -> list[dict[str, Any]]:
    """customers 테이블 전체 (짧은 TTL 메모리 캐시). order: PostgREST order 파라미터."""
    global _rows_cache, _rows_cache_at
    use_cache = (
        not force_refresh
        and order == "business_number.asc"
        and select == _SELECT
        and _rows_cache is not None
        and (time.time() - _rows_cache_at) < _CACHE_TTL_SEC
    )
    if use_cache:
        return list(_rows_cache)

    if not is_configured():
        raise RuntimeError("SUPABASE_URL 및 SUPABASE_KEY(또는 SERVICE_ROLE)가 필요합니다.")

    endpoint = f"{_rest_base()}/customers"
    params = {"select": select, "order": order}
    out: list[dict[str, Any]] = []
    offset = 0

    while True:
        end = offset + _PAGE_SIZE - 1
        headers = _rest_headers(extra={"Range": f"{offset}-{end}"})
        r = _SESSION.get(endpoint, params=params, headers=headers, timeout=90)
        if r.status_code >= 400:
            detail = (r.text or "")[:500]
            raise RuntimeError(f"Supabase 조회 실패 HTTP {r.status_code}: {detail}")
        batch = r.json()
        if not isinstance(batch, list):
            raise RuntimeError("Supabase 응답 형식 오류(배열 아님)")
        out.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    if order == "business_number.asc" and select == _SELECT:
        _rows_cache = out
        _rows_cache_at = time.time()
    return list(out)


def _fetch_rows_ordered(order: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
    try:
        return fetch_all_customer_rows(
            force_refresh=force_refresh,
            order=order,
            select=_SELECT_WITH_CREATED,
        )
    except RuntimeError as e:
        msg = str(e)
        if "created_at" in msg or "42703" in msg or "column" in msg.lower():
            return fetch_all_customer_rows(
                force_refresh=force_refresh,
                order="business_number.desc",
            )
        raise


def fetch_rows_biz_number_only(
    *,
    force_refresh: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """사업자번호만 등록된 행 — 최신 등록(created_at) 우선."""
    rows = _fetch_rows_ordered("created_at.desc", force_refresh=force_refresh)
    pending = [r for r in rows if is_biz_number_only_row(r)]
    if limit is not None and limit > 0:
        return pending[:limit]
    return pending


def fetch_customer_by_business_number(
    business_number: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """단일 사업자번호 행 조회."""
    biz = normalize_biz_number(business_number)
    if len(biz) != 10:
        return None
    if not is_configured():
        raise RuntimeError("SUPABASE_URL 및 SUPABASE_KEY(또는 SERVICE_ROLE)가 필요합니다.")

    endpoint = f"{_rest_base()}/customers"
    for select in (_SELECT_WITH_CREATED, _SELECT):
        params = {
            "select": select,
            "business_number": f"eq.{biz}",
            "limit": "1",
        }
        r = _SESSION.get(endpoint, params=params, headers=_rest_headers(), timeout=60)
        if r.status_code >= 400:
            msg = (r.text or "")[:500]
            if select == _SELECT_WITH_CREATED and (
                "created_at" in msg or "42703" in msg or "column" in msg.lower()
            ):
                continue
            raise RuntimeError(f"Supabase 조회 실패 HTTP {r.status_code}: {msg}")
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            return None
        return dict(batch[0])
    return None


def _format_created_at_display(raw: Any) -> str:
    if raw is None or raw == "":
        return ""
    try:
        from datetime import datetime

        s = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(raw)[:19]


def pending_row_display(row: dict[str, Any]) -> dict[str, Any]:
    """업체정보 갱신 페이지·API용 표시 dict."""
    biz = normalize_biz_number(str(row.get("business_number") or ""))
    cc = normalize_disclosure_corp_code(str(row.get("disclosure_corp_code") or ""))
    created = row.get("created_at")
    return {
        "business_number": biz,
        "company_name": (str(row.get("company_name") or "")).strip(),
        "disclosure_corp_code": cc,
        "stock_code": (str(row.get("stock_code") or "")).strip(),
        "created_at": created,
        "created_at_display": _format_created_at_display(created),
    }


class DuplicateBusinessNumberError(ValueError):
    """동일 사업자번호가 customers에 이미 있음."""


class CustomerNotFoundError(ValueError):
    """customers에 해당 사업자번호 없음."""


def insert_business_number_only(business_number: str) -> dict[str, Any]:
    """사업자번호만 신규 등록. 동일 번호가 있으면 등록하지 않음."""
    biz = normalize_biz_number(business_number)
    if len(biz) != 10:
        raise ValueError("사업자번호는 10자리 숫자여야 합니다.")

    existing = fetch_customer_by_business_number(biz)
    if existing:
        raise DuplicateBusinessNumberError("이미 등록된 사업자번호입니다.")

    row = {
        "business_number": biz,
        "company_name": "",
        "disclosure_corp_code": "",
        "stock_code": "",
    }
    upsert_customer_rows([row])
    return fetch_customer_by_business_number(biz, force_refresh=True) or row


def delete_customer_by_business_number(business_number: str) -> dict[str, Any]:
    """사업자번호로 customers 행 삭제."""
    biz = normalize_biz_number(business_number)
    if len(biz) != 10:
        raise ValueError("사업자번호는 10자리 숫자여야 합니다.")

    existing = fetch_customer_by_business_number(biz)
    if not existing:
        raise CustomerNotFoundError(f"DB에 등록된 사업자번호가 없습니다: {biz}")

    endpoint = f"{_rest_base()}/customers"
    params = {"business_number": f"eq.{biz}"}
    r = _SESSION.delete(endpoint, params=params, headers=_rest_headers(), timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(
            f"Supabase 삭제 실패 HTTP {r.status_code}: {(r.text or '')[:500]}"
        )

    invalidate_customer_cache()
    return pending_row_display(existing)


def db_row_to_customer(row: dict[str, Any]) -> dict[str, Any]:
    biz = normalize_biz_number(str(row.get("business_number") or ""))
    cc = normalize_disclosure_corp_code(str(row.get("disclosure_corp_code") or "")) or None
    stock = (str(row.get("stock_code") or "")).strip()
    name = (str(row.get("company_name") or "")).strip()
    return {
        "company_name": name,
        "business_number": biz,
        "ticker_override": stock or None,
        "dart_corp_code": cc if cc and len(cc) == 8 else None,
    }


def customers_as_analyze_dicts(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    raw = rows if rows is not None else fetch_all_customer_rows()
    out: list[dict[str, Any]] = []
    for r in raw:
        c = db_row_to_customer(r)
        if len(c["business_number"]) == 10:
            out.append(c)
    return out


def fetch_rows_missing_disclosure(
    *,
    force_refresh: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """disclosure_corp_code 가 비었거나 8자리가 아닌 customers 행."""
    rows = fetch_all_customer_rows(force_refresh=force_refresh)
    missing = [
        r
        for r in rows
        if is_missing_disclosure_code(str(r.get("disclosure_corp_code") or ""))
    ]
    if limit is not None and limit > 0:
        return missing[:limit]
    return missing


def count_missing_disclosure(*, force_refresh: bool = False) -> int:
    return len(fetch_rows_missing_disclosure(force_refresh=force_refresh))


def filter_customers_with_disclosure(
    customers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    ok: list[dict[str, Any]] = []
    skipped = 0
    for c in customers:
        cc = normalize_disclosure_corp_code(c.get("dart_corp_code") or "") or ""
        if len(cc) == 8:
            ok.append({**c, "dart_corp_code": cc})
        else:
            skipped += 1
    return ok, skipped


def customers_to_csv_text(rows: list[dict[str, Any]] | None = None) -> str:
    raw = rows if rows is not None else fetch_all_customer_rows()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["사업자번호", "업체명", "공시번호", "종목코드"])
    for r in raw:
        biz = normalize_biz_number(str(r.get("business_number") or ""))
        if len(biz) != 10:
            continue
        w.writerow(
            [
                biz,
                (str(r.get("company_name") or "")).strip(),
                normalize_disclosure_corp_code(str(r.get("disclosure_corp_code") or "")),
                (str(r.get("stock_code") or "")).strip(),
            ]
        )
    return buf.getvalue()


def upsert_customer_rows(rows: list[dict[str, Any]], *, batch_size: int = 200) -> int:
    if not rows:
        return 0
    endpoint = f"{_rest_base()}/customers"
    headers = _rest_headers(
        extra={
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
    )
    n = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        r = _SESSION.post(endpoint, headers=headers, data=json.dumps(chunk), timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase upsert 실패 HTTP {r.status_code}: {(r.text or '')[:500]}")
        n += len(chunk)
    invalidate_customer_cache()
    return n


def merge_into_biz_registry(registry: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    if not is_configured():
        return registry
    try:
        rows = fetch_all_customer_rows()
    except Exception:
        return registry
    for r in rows:
        biz = normalize_biz_number(str(r.get("business_number") or ""))
        if len(biz) != 10:
            continue
        cc = normalize_disclosure_corp_code(str(r.get("disclosure_corp_code") or ""))
        name = (str(r.get("company_name") or "")).strip()
        stock = (str(r.get("stock_code") or "")).strip()
        entry = {
            "corp_name": name,
            "corp_code": cc if len(cc) == 8 else "",
            "stock_code": stock,
        }
        cur = registry.get(biz)
        if cur is None:
            registry[biz] = entry
            continue
        if not (cur.get("corp_code") or "").strip() and entry["corp_code"]:
            cur["corp_code"] = entry["corp_code"]
        if not (cur.get("stock_code") or "").strip() and entry["stock_code"]:
            cur["stock_code"] = entry["stock_code"]
        if not (cur.get("corp_name") or "").strip() and entry["corp_name"]:
            cur["corp_name"] = entry["corp_name"]
    return registry
