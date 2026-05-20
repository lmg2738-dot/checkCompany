# -*- coding: utf-8 -*-
"""
Vercel Fluid/로컬 공용: 로컬 실행은 여기서 하고,
원격(Vercel)은 api/index.py + vercel.json rewrites 를 진입으로 씁니다.
"""
from __future__ import annotations

from app import app, main as run_local_server

__all__ = ["app"]


if __name__ == "__main__":
    run_local_server()
