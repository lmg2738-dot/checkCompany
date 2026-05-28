# -*- coding: utf-8 -*-
"""웹 일괄 분석 fast path (DART company.json·스캔 생략 등)."""

from __future__ import annotations

import contextvars
import threading

_analyze_runtime: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "analyze_runtime",
    default=False,
)
_thread_bulk = threading.local()


def set_analyze_runtime(active: bool = True) -> contextvars.Token[bool]:
    return _analyze_runtime.set(active)


def reset_analyze_runtime(token: contextvars.Token[bool]) -> None:
    _analyze_runtime.reset(token)


def set_thread_bulk_analyze(active: bool = True) -> None:
    _thread_bulk.active = active


def in_analyze_runtime() -> bool:
    return bool(getattr(_thread_bulk, "active", False)) or _analyze_runtime.get()


class analyze_runtime:
    def __enter__(self) -> analyze_runtime:
        self._token = set_analyze_runtime(True)
        return self

    def __exit__(self, *exc: object) -> None:
        reset_analyze_runtime(self._token)
