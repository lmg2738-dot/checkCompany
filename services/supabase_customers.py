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


def fetch_all_customer_rows(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """customers 테이블 전체 (짧은 TTL 메모리 캐시)."""
    global _rows_cache, _rows_cache_at
    if (
        not force_refresh
        and _rows_cache is not None
        and (time.time() - _rows_cache_at) < _CACHE_TTL_SEC
    ):
        return list(_rows_cache)

    if not is_configured():
        raise RuntimeError("SUPABASE_URL 및 SUPABASE_KEY(또는 SERVICE_ROLE)가 필요합니다.")

    endpoint = f"{_rest_base()}/customers"
    params = {"select": _SELECT, "order": "business_number.asc"}
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

    _rows_cache = out
    _rows_cache_at = time.time()
    return list(out)


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


def is_missing_disclosure_code(raw: str | None) -> bool:
    cc = normalize_disclosure_corp_code(raw or "")
    return len(cc) != 8


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
