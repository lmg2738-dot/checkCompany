# -*- coding: utf-8 -*-
"""
Vercel Fluid/백엔드용 기본 진입점.

공식 예제는 main.py 에 Flask 인스턴스 `app` 을 두는 패턴입니다.
https://github.com/vercel/vercel/tree/main/examples/flask
"""
from __future__ import annotations

from app import app, main as run_local_server

__all__ = ["app"]


if __name__ == "__main__":
    run_local_server()
