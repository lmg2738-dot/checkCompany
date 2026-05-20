# -*- coding: utf-8 -*-
"""
Vercel Serverless Functions 진입점.

Fluid 단일 Flask 외에도, 일부 프로젝트/설정에서 루트 경로가
함수에 연결되지 않아 NOT_FOUND 가 나는 경우가 있어
vercel.json 의 rewrites 가 모든 요청을 이 핸들러로 보냅니다.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# data/, templates/ 등 프로젝트 루트 기준 경로
try:
    os.chdir(_ROOT)
except OSError:
    pass

from app import app as flask_app

# Vercel Python 런타임이 찾는 WSGI 애플리케이션 이름
app = flask_app

__all__ = ["app"]
