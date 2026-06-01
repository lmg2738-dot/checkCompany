# -*- coding: utf-8 -*-
"""
Vercel Cron 전용 진입점 (/api/cron/daily-risk-email).

catch-all rewrite(api/index)만 쓰면 Cron 호출 시 Authorization·x-vercel-cron 이
빠지고 127.0.0.1 내부 프록시만 남는 경우가 있어, 이 파일로 직접 라우팅합니다.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    os.chdir(_ROOT)
except OSError:
    pass

from services.env_config import bootstrap_env

bootstrap_env()

from app import app  # noqa: E402

__all__ = ["app"]
