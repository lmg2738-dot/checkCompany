# -*- coding: utf-8 -*-
"""공시번호 미보유 고객 — DART API로 업체명·공시번호·종목코드 보강."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Iterator

from services.dart_service import (
    _normalize_stock_code,
    get_dart_api_key,
    normalize_disclosure_corp_code,
    resolve_corp_code,
)
from services.news_rss import normalize_biz_number

_DART_QUOTA_HINTS = (
    "013",
    "한도",
    "할당",
    "quota",
    "limit exceeded",
    "too many",
    "429",
    "rate limit",
    "exceeded",
)


class DartQuotaError(RuntimeError):
    """DART 일일 호출 한도 등 — 배치 중단."""


@dataclass
class EnrichOutcome:
    row: dict[str, Any] | None = None
    error: str | None = None
    fatal: bool = False


def is_dart_quota_error(text: str | None) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if "013" in (text or ""):
        return True
    return any(h in t for h in _DART_QUOTA_HINTS)


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


def enrich_manual_max_items() -> int | None:
    """수동 「업체정보 갱신」 1회 상한. None 이면 pending 전체."""
    return _enrich_manual_max()


def _enrich_manual_max() -> int | None:
    """수동 갱신 1회 상한. 0 또는 비우면 전체 pending."""
    raw = (os.environ.get("DART_ENRICH_MANUAL_MAX") or "0").strip()
    if not raw or raw == "0":
        return None
    try:
        return max(1, min(int(raw), 2000))
    except ValueError:
        return None


def enrich_customer_from_dart(
    business_number: str,
    company_name: str = "",
    *,
    api_key: str | None = None,
) -> EnrichOutcome:
    key = api_key or get_dart_api_key()
    if not key:
        return EnrichOutcome(error="DART_API_KEY 가 설정되지 않았습니다.", fatal=True)

    biz = normalize_biz_number(business_number)
    if len(biz) != 10:
        return EnrichOutcome(error="사업자번호 10자리 아님", fatal=False)

    name = (company_name or "").strip()
    try:
        row, prof, msg = resolve_corp_code(name, biz, key)
    except Exception as e:
        err = str(e)
        fatal = is_dart_quota_error(err)
        if fatal:
            return EnrichOutcome(error=f"DART API 오류(한도 가능): {err}", fatal=True)
        return EnrichOutcome(error=err[:200], fatal=False)

    if msg and is_dart_quota_error(msg):
        return EnrichOutcome(
            error=f"DART API 호출 한도 또는 일시 제한: {msg}",
            fatal=True,
        )

    if not row or not prof:
        hint = (msg or "DART 매칭 실패").strip()
        if is_dart_quota_error(hint):
            return EnrichOutcome(error=hint, fatal=True)
        return EnrichOutcome(error=hint or "공시번호 미확정", fatal=False)

    cc = normalize_disclosure_corp_code(
        str(row.get("corp_code") or prof.get("corp_code") or "")
    )
    if len(cc) != 8:
        return EnrichOutcome(error="공시번호 8자리 미확정", fatal=False)

    corp_name = (row.get("corp_name") or prof.get("corp_name") or name or "").strip()
    stock = _normalize_stock_code(
        str(row.get("stock_code") or prof.get("stock_code") or "")
    )

    return EnrichOutcome(
        row={
            "business_number": biz,
            "company_name": corp_name,
            "disclosure_corp_code": cc,
            "stock_code": stock if stock and stock != "000000" else "",
        }
    )


def iter_disclosure_refresh_events(
    pending_rows: list[dict[str, Any]],
    *,
    max_items: int | None = None,
    sleep_sec: float | None = None,
    upsert_batch_size: int = 20,
) -> Iterator[dict[str, Any]]:
    """
    NDJSON 스트림용 이벤트 생성.
    type: info | progress | error | done
    """
    from services import supabase_customers

    if not supabase_customers.is_configured():
        yield {"type": "error", "message": "Supabase 가 설정되지 않았습니다."}
        return

    key = get_dart_api_key()
    if not key:
        yield {"type": "error", "message": "DART_API_KEY 가 설정되지 않았습니다."}
        return

    pending = list(pending_rows)
    if not pending:
        yield {
            "type": "error",
            "message": "공시번호가 없는 고객이 없습니다. 갱신할 항목이 없습니다.",
        }
        return

    cap = max_items if max_items is not None else len(pending)
    gap = _enrich_sleep_sec() if sleep_sec is None else sleep_sec
    todo = pending[:cap]
    n = len(todo)

    yield {
        "type": "info",
        "message": f"공시번호 없음 {len(pending)}건 중 이번 실행 {n}건 DART 조회",
    }

    t0 = time.perf_counter()
    buffer: list[dict[str, Any]] = []
    matched = 0
    failed_count = 0
    upserted_total = 0

    def flush_buffer() -> int:
        nonlocal upserted_total
        if not buffer:
            return 0
        chunk = list(buffer)
        buffer.clear()
        n_up = supabase_customers.upsert_customer_rows(chunk)
        upserted_total += n_up
        return n_up

    for i, raw in enumerate(todo):
        biz = normalize_biz_number(str(raw.get("business_number") or ""))
        if len(biz) != 10:
            failed_count += 1
            continue

        name = (str(raw.get("company_name") or "")).strip()
        outcome = enrich_customer_from_dart(biz, name, api_key=key)

        if outcome.fatal:
            flush_buffer()
            elapsed = time.perf_counter() - t0
            yield {
                "type": "progress",
                "current": i + 1,
                "total": n,
                "fraction": (i + 1) / n if n else 1.0,
                "pct": round(100.0 * (i + 1) / n, 1) if n else 100.0,
                "elapsed_sec": round(elapsed, 1),
                "eta_sec": 0.0,
                "matched": matched,
                "upserted": upserted_total,
            }
            yield {
                "type": "error",
                "message": outcome.error or "DART API 한도 또는 오류로 중단합니다.",
                "stop_reason": "dart_quota",
            }
            return

        if outcome.row:
            matched += 1
            buffer.append(outcome.row)
            display_row = {**raw, **outcome.row}
            yield {
                "type": "item",
                "business_number": biz,
                "ok": True,
                "row": display_row,
            }
            if len(buffer) >= upsert_batch_size:
                flush_buffer()
        else:
            failed_count += 1
            yield {
                "type": "item",
                "business_number": biz,
                "ok": False,
                "error": outcome.error or "DART 매칭 실패",
            }

        elapsed = time.perf_counter() - t0
        cur = i + 1
        rate = elapsed / cur if cur else 0.0
        eta_sec = max(0.0, rate * (n - cur))
        yield {
            "type": "progress",
            "current": cur,
            "total": n,
            "fraction": cur / n if n else 1.0,
            "pct": round(100.0 * cur / n, 1) if n else 100.0,
            "elapsed_sec": round(elapsed, 1),
            "eta_sec": round(eta_sec, 1),
            "matched": matched,
            "upserted": upserted_total,
            "failed": failed_count,
        }

        if cur < n and gap > 0:
            time.sleep(gap)

    flush_buffer()
    still = supabase_customers.count_missing_disclosure(force_refresh=True)

    yield {
        "type": "done",
        "matched": matched,
        "upserted": upserted_total,
        "failed_count": failed_count,
        "processed": n,
        "pending_total": len(pending),
        "remaining_missing": still,
        "remaining_after_cap": max(0, len(pending) - n),
    }


def run_disclosure_refresh_batch(
    pending_rows: list[dict[str, Any]],
    *,
    max_items: int | None = None,
    sleep_sec: float | None = None,
) -> dict[str, Any]:
    """Cron용 — 스트림 없이 일괄 실행."""
    from services import supabase_customers

    if not supabase_customers.is_configured():
        raise RuntimeError("Supabase 가 설정되지 않았습니다.")

    cap = max_items if max_items is not None else _enrich_max_per_run()
    stats: dict[str, Any] = {
        "pending_total": len(pending_rows),
        "processed": 0,
        "updated": 0,
        "matched": 0,
        "failed_count": 0,
        "failed_sample": [],
        "remaining_after_cap": max(0, len(pending_rows) - cap),
        "max_per_run": cap,
    }

    for ev in iter_disclosure_refresh_events(
        pending_rows[:cap],
        max_items=cap,
        sleep_sec=sleep_sec,
    ):
        if ev.get("type") == "error":
            stats["error"] = ev.get("message")
            stats["stop_reason"] = ev.get("stop_reason")
            break
        if ev.get("type") == "done":
            stats["processed"] = ev.get("processed", 0)
            stats["updated"] = ev.get("upserted", 0)
            stats["matched"] = ev.get("matched", 0)
            stats["failed_count"] = ev.get("failed_count", 0)
            stats["still_missing_disclosure"] = ev.get("remaining_missing")
            break

    return stats
