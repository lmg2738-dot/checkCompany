# -*- coding: utf-8 -*-
"""고객 목록 일괄 위험 분석 (Cron·이메일용)."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any

from customer_analyzer import analyze_customer
from services.analysis_context import analyze_runtime, set_thread_bulk_analyze
from services.dart_service import load_biz_registry


def _workers() -> int:
    try:
        n = int(os.environ.get("ANALYZE_BULK_WORKERS", "6"))
    except ValueError:
        n = 6
    return max(1, min(n, 12))


def _analyze_one(customer: dict[str, Any]) -> dict[str, Any]:
    set_thread_bulk_analyze(True)
    try:
        return analyze_customer(
            customer.get("company_name") or "",
            customer.get("business_number") or "",
            customer.get("ticker_override"),
            customer.get("dart_corp_code"),
        )
    finally:
        set_thread_bulk_analyze(False)


def serialize_analysis_row(row: dict[str, Any]) -> dict[str, Any]:
    """Jinja·JSON용 — estimate dataclass 를 dict 로."""
    out = dict(row)
    est = out.get("estimate")
    if est is not None and hasattr(est, "__dataclass_fields__"):
        out["estimate"] = asdict(est)
    return out


def run_bulk_customer_analysis(customers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not customers:
        return []

    gap = float(os.environ.get("DART_ANALYZE_SLEEP_SEC") or "0")
    workers = _workers()
    load_biz_registry()
    results: list[dict[str, Any]] = []

    with analyze_runtime():
        if len(customers) <= 2 or workers <= 1:
            for i, c in enumerate(customers):
                if i and gap > 0:
                    time.sleep(gap)
                results.append(serialize_analysis_row(_analyze_one(c)))
        else:
            payload: list[dict[str, Any] | None] = [None] * len(customers)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_analyze_one, c): i for i, c in enumerate(customers)}
                for fut in as_completed(futures):
                    i = futures[fut]
                    payload[i] = serialize_analysis_row(fut.result())
            results = [r for r in payload if r is not None]

    return results
