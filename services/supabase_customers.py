# -*- coding: utf-8 -*-
"""Supabase public.customers 마스터 (사업자번호·업체명·공시번호·종목코드)."""

from __future__ import annotations

import csv
import io
import os
import time
from typing import Any

from services.dart_service import normalize_disclosure_corp_code
from services.env_config import bootstrap_env
from services.news_rss import normalize_biz_number

bootstrap_env()

_client: Any = None
_rows_cache: list[dict[str, Any]] | None = None
_rows_cache_at: float = 0.0
_CACHE_TTL_SEC = 300.0
_PAGE_SIZE = 1000


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


def invalidate_customer_cache() -> None:
    global _rows_cache, _rows_cache_at
    _rows_cache = None
    _rows_cache_at = 0.0


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client
    url = _supabase_url()
    key = _supabase_key()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL 및 SUPABASE_KEY(또는 SERVICE_ROLE)가 필요합니다.")
    try:
        from supabase import create_client
    except ImportError as e:
        raise RuntimeError("pip install supabase 가 필요합니다.") from e
    _client = create_client(url, key)
    return _client


def fetch_all_customer_rows(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """customers 테이블 전체 (짧은 TTL 메모리 캐시)."""
    global _rows_cache, _rows_cache_at
    if (
        not force_refresh
        and _rows_cache is not None
        and (time.time() - _rows_cache_at) < _CACHE_TTL_SEC
    ):
        return list(_rows_cache)

    client = _get_client()
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        end = offset + _PAGE_SIZE - 1
        res = (
            client.table("customers")
            .select("business_number,company_name,disclosure_corp_code,stock_code")
            .order("business_number")
            .range(offset, end)
            .execute()
        )
        batch = list(res.data or [])
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


def filter_customers_with_disclosure(
    customers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """공시번호 8자리 있는 행만. 반환: (분석 대상, 제외 건수)."""
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
    """business_number 기준 upsert."""
    if not rows:
        return 0
    client = _get_client()
    n = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        client.table("customers").upsert(chunk, on_conflict="business_number").execute()
        n += len(chunk)
    invalidate_customer_cache()
    return n


def merge_into_biz_registry(registry: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Supabase 행을 dart_service 레지스트리에 병합."""
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
