# -*- coding: utf-8 -*-
"""공시번호 미보유 customers → DART 조회 → Supabase upsert (로컬·수동 실행)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.env_config import bootstrap_env

bootstrap_env()

from services import supabase_customers  # noqa: E402
from services.customer_enrichment import run_disclosure_refresh_batch  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="DART 공시번호·종목 보강 배치")
    p.add_argument("--max", type=int, default=None, help="이번 실행 최대 건수")
    p.add_argument("--dry-run", action="store_true", help="대상만 출력, DART/DB 미반영")
    args = p.parse_args()

    if not supabase_customers.is_configured():
        raise SystemExit("SUPABASE_URL / SUPABASE_KEY 를 설정하세요.")

    pending = supabase_customers.fetch_rows_missing_disclosure(force_refresh=True)
    print(f"공시번호 없음: {len(pending)}건")
    if args.dry_run:
        for r in pending[:20]:
            print(r.get("business_number"), r.get("company_name"))
        if len(pending) > 20:
            print(f"... 외 {len(pending) - 20}건")
        return

    stats = run_disclosure_refresh_batch(pending, max_items=args.max)
    still = supabase_customers.count_missing_disclosure(force_refresh=True)
    print(json.dumps({**stats, "still_missing_disclosure": still}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
