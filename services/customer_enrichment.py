# -*- coding: utf-8 -*-
"""공시번호 미보유 고객 — DART API로 업체명·공시번호·종목코드 보강."""

from __future__ import annotations

import os
import time
from typing import Any

from services.dart_service import (
    _normalize_stock_code,
    get_dart_api_key,
    normalize_disclosure_corp_code,
    resolve_corp_code,
)
from services.news_rss import normalize_biz_number


def _enrich_sleep_sec() -> float:
    try:
        return float(os.environ.get("DART_ENRICH_SLEEP_SEC") or "0.15")
    except ValueError:
        return 0.15


def _enrich_max_per_run() -> int:
    try:
        n = int(os.environ.get("DART_DISCLOSURE_REFRESH_MAX") or "60")
    except ValueError:
        n = 60
    return max(1, min(n, 500))


def enrich_customer_from_dart(
    business_number: str,
    company_name: str = "",
    *,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """
    사업자번호로 DART 매칭 → Supabase customers upsert용 dict.
    공시번호 8자리 확정 시에만 반환.
    """
    key = api_key or get_dart_api_key()
    if not key:
        raise RuntimeError("DART_API_KEY 가 설정되지 않았습니다.")

    biz = normalize_biz_number(business_number)
    if len(biz) != 10:
        return None

    name = (company_name or "").strip()
    row, prof, _msg = resolve_corp_code(name, biz, key)
    if not row or not prof:
        return None

    cc = normalize_disclosure_corp_code(
        str(row.get("corp_code") or prof.get("corp_code") or "")
    )
    if len(cc) != 8:
        return None

    corp_name = (row.get("corp_name") or prof.get("corp_name") or name or "").strip()
    stock = _normalize_stock_code(
        str(row.get("stock_code") or prof.get("stock_code") or "")
    )

    return {
        "business_number": biz,
        "company_name": corp_name,
        "disclosure_corp_code": cc,
        "stock_code": stock if stock and stock != "000000" else "",
    }


def run_disclosure_refresh_batch(
    pending_rows: list[dict[str, Any]],
    *,
    max_items: int | None = None,
    sleep_sec: float | None = None,
) -> dict[str, Any]:
    """
    공시번호 없는 고객 목록을 DART로 조회해 Supabase에 반영.
    반환: 통계 dict (processed, updated, failed, remaining_hint 등).
    """
    from services import supabase_customers

    if not supabase_customers.is_configured():
        raise RuntimeError("Supabase 가 설정되지 않았습니다.")

    key = get_dart_api_key()
    if not key:
        raise RuntimeError("DART_API_KEY 가 설정되지 않았습니다.")

    cap = max_items if max_items is not None else _enrich_max_per_run()
    gap = _enrich_sleep_sec() if sleep_sec is None else sleep_sec
    todo = pending_rows[:cap]

    updated_rows: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []

    for i, raw in enumerate(todo):
        biz = normalize_biz_number(str(raw.get("business_number") or ""))
        if len(biz) != 10:
            continue
        name = (str(raw.get("company_name") or "")).strip()
        try:
            enriched = enrich_customer_from_dart(biz, name, api_key=key)
        except Exception as e:
            failed.append({"business_number": biz, "error": str(e)[:200]})
            if i and gap > 0:
                time.sleep(gap)
            continue

        if enriched:
            updated_rows.append(enriched)
        else:
            failed.append(
                {
                    "business_number": biz,
                    "error": "DART 매칭 실패 또는 공시번호 미확정",
                }
            )

        if i + 1 < len(todo) and gap > 0:
            time.sleep(gap)

    upserted = 0
    if updated_rows:
        upserted = supabase_customers.upsert_customer_rows(updated_rows)

    remaining = max(0, len(pending_rows) - len(todo))

    return {
        "pending_total": len(pending_rows),
        "processed": len(todo),
        "updated": upserted,
        "matched": len(updated_rows),
        "failed_count": len(failed),
        "failed_sample": failed[:15],
        "remaining_after_cap": remaining,
        "max_per_run": cap,
    }
