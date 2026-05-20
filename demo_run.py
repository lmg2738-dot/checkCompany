# -*- coding: utf-8 -*-
"""
예시 CSV 또는 data/dart_bizno_input_list.txt 로 분석 파이프라인 스모크 테스트 (CLI).
사용: python demo_run.py
      python demo_run.py --dart-bizno-list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from app import parse_csv_text
from services.dart_service import dart_bizno_input_customer_rows
from customer_analyzer import analyze_customer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dart-bizno-list",
        action="store_true",
        help="data/dart_bizno_input_list.txt 의 사업자번호만 분석",
    )
    args = ap.parse_args()

    if args.dart_bizno_list:
        rows = dart_bizno_input_customer_rows()
        if not rows:
            print("dart_bizno_input_list.txt 에 유효한 사업자번호가 없습니다.")
            return 1
    else:
        sample = ROOT / "data" / "sample_customers.csv"
        if not sample.exists():
            print("없음:", sample)
            return 1
        raw = sample.read_text(encoding="utf-8-sig")
        rows = parse_csv_text(raw)
    if not rows:
        print("CSV 파싱 결과 행 없음")
        return 1
    print("=== 예시 고객", len(rows), "건 분석 시작 ===\n")
    for i, c in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {c['company_name']} ({c['business_number']}) …")
        r = analyze_customer(**c)
        est = r["estimate"]
        print(
            f"    복합점수={est.composite_score}  이탈추정P={est.estimated_churn_probability:.3f}  "
            f"N={est.news_risk:.2f} M={est.market_risk:.2f} C={est.credit_risk:.2f}"
        )
        if r.get("dart", {}).get("corp_code"):
            print(f"    DART corp_code={r['dart']['corp_code']} 종목={r.get('ticker')}")
        if r.get("dart", {}).get("message"):
            print(f"    DART 메시지: {r['dart']['message']}")
        print()
    print("=== 완료 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
