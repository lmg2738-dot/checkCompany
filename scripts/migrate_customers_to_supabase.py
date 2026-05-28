# -*- coding: utf-8 -*-
"""data/dart_biz_master_list.csv → Supabase public.customers."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.env_config import bootstrap_env

bootstrap_env()

from services.dart_service import DART_BIZ_MASTER_CSV, parse_biz_master_csv_row  # noqa: E402
from services import supabase_customers  # noqa: E402


def main() -> None:
    if not supabase_customers.is_configured():
        raise SystemExit("SUPABASE_URL / SUPABASE_KEY 를 .env 에 설정하세요.")
    if not DART_BIZ_MASTER_CSV.is_file():
        raise SystemExit(f"없음: {DART_BIZ_MASTER_CSV}")

    import csv

    rows: list[dict] = []
    with DART_BIZ_MASTER_CSV.open(encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f):
            parsed = parse_biz_master_csv_row(raw)
            if not parsed:
                continue
            biz, entry = parsed
            rows.append(
                {
                    "business_number": biz,
                    "company_name": entry.get("corp_name") or "",
                    "disclosure_corp_code": entry.get("corp_code") or "",
                    "stock_code": entry.get("stock_code") or "",
                }
            )

    n = supabase_customers.upsert_customer_rows(rows)
    print(f"upsert {n} rows → customers")


if __name__ == "__main__":
    main()
