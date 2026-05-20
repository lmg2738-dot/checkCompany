# -*- coding: utf-8 -*-
"""Open DART 마스터 빌드(`build_dart_registry.py`) 실행 이력 — 내일 이어 실행·한도 관리용."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HISTORY_PATH = DATA_DIR / "dart_build_history.json"


def default_daily_call_cap() -> int:
    raw = (os.environ.get("DART_BUILD_DAILY_CALL_CAP") or os.environ.get("DART_BUILD_MAX_SCAN") or "40000").strip()
    try:
        v = int(raw)
        return max(1000, min(v, 500_000))
    except ValueError:
        return 40_000


def default_stage2_max_scan() -> int:
    raw = (os.environ.get("DART_BUILD_MAX_SCAN") or "").strip()
    if raw.isdigit():
        return max(100, min(int(raw), 500_000))
    return default_daily_call_cap()


def load_history() -> dict[str, Any]:
    if not HISTORY_PATH.is_file():
        return {}
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_history(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    prev = load_history()
    at = datetime.now(timezone.utc).isoformat()
    runs = list(prev.get("runs") or [])
    runs.append({"at": at, **payload})
    prev["runs"] = runs[-50:]
    prev["last"] = {**payload, "ended_at": at}
    HISTORY_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")


def _console_safe_str(s: str) -> str:
    """Windows cp949 콘솔 등에서 인쇄 불가한 대시 등 보정."""
    return s.replace("\u2014", "-").replace("\u2013", "-")


def print_last_run_summary() -> None:
    h = load_history()
    last = h.get("last")
    if not last:
        print("=== dart_build_history: 이전 실행 기록 없음 ===", flush=True)
        return
    print("=== 직전 빌드 요약 (data/dart_build_history.json) ===", flush=True)
    for k in (
        "ended_at",
        "matched_rows",
        "input_valid",
        "still_unmatched",
        "stage1_api_calls",
        "stage2_api_calls",
        "daily_cap",
        "stage2_max_calls_effective",
        "next_corp_scan_index",
        "exit_note",
    ):
        if k in last:
            val = last[k]
            if isinstance(val, str):
                val = _console_safe_str(val)
            print(f"  {k}: {val}", flush=True)


# 웹/문서용: 종목코드가 비는 대표 원인 (API 한도 + 데이터)
STOCK_CODE_GAP_CAUSES_KO = (
    "종목코드(표에서 흔히 '품목코드'로 부르는 6자리 상장코드)가 중간부터 비는 주요 원인:\n"
    "  1) DART 일일 호출 한도(예: 4만 회) 도중 - 이후 company.json 이 실패/빈 응답 -> 매칭/종목 미기록.\n"
    "  2) 마스터 CSV에 아직 해당 사업자 행이 없거나, 빌드가 중간에 멈춤 -> 0단계에서 복원되지 않음.\n"
    "  3) 엑셀에서 공시번호 앞자리 0이 숫자로 깨짐 -> 과거에는 행이 레지스트리에서 빠졌을 수 있음(현재는 8자리 0패딩 보정).\n"
    "  4) 비상장/미공시 법인 - DART에 종목코드 자체가 없음.\n"
    "이어 실행: python build_dart_registry.py --resume-scan (또는 data/.dart_build_scan_next_index 기준)\n"
    "미완료만 목록: python build_dart_registry.py --list-incomplete"
)
