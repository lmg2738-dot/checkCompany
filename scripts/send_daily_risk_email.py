# -*- coding: utf-8 -*-
"""로컬에서 일일 위험 리포트 이메일 수동 발송."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.env_config import bootstrap_env

bootstrap_env()

from app import app, prepare_customers_for_analysis, sorted_results_for_template  # noqa: E402
from services.bulk_analyze import run_bulk_customer_analysis  # noqa: E402
from services.daily_risk_email import send_daily_risk_report  # noqa: E402


def main() -> int:
    with app.app_context():
        customers, prep = prepare_customers_for_analysis("")
        if not customers:
            print("분석 가능한 고객이 없습니다.", file=sys.stderr)
            return 1
        print(f"분석 시작: {len(customers)}건 …")
        results = run_bulk_customer_analysis(customers)
        sorted_rows = sorted_results_for_template(results)
        out = send_daily_risk_report(sorted_rows, prep_meta=prep)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
