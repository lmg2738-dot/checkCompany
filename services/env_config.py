# -*- coding: utf-8 -*-
"""환경 변수: 로컬 .env / Vercel 대시보드 시크릿."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_bootstrapped = False


def bootstrap_env() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True
    if (os.environ.get("VERCEL") or "").strip():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def is_vercel() -> bool:
    return bool((os.environ.get("VERCEL") or "").strip())
