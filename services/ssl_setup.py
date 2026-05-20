# -*- coding: utf-8 -*-
"""
외부 HTTPS(브라우저와 다른 Python 기본 CA) 호환.

- HTTP_USE_SYSTEM_CA=1(기본): truststore로 OS 신뢰 저장소 사용(Windows·회사 프록시 MITM과 동일 체인).
- HTTP_SSL_VERIFY=0: 검증 비활성화(개발·진단용만 권장).
- HTTP_SSL_VERIFY=/path/to.pem: 사용자 지정 CA 번들(회사 루트만 추출한 PEM).

환경 변수는 app/dotenv 로드 후에도 적용되도록, 첫 HTTP 요청 전에 init_truststore() 호출.
"""

from __future__ import annotations

import os
from pathlib import Path

_initialized = False


def get_requests_verify() -> bool | str:
    """requests Session.verify 에 넣을 값."""
    raw = (os.environ.get("HTTP_SSL_VERIFY") or "").strip()
    if not raw or raw in ("1", "true", "yes", "on"):
        return True
    low = raw.lower()
    if low in ("0", "false", "no", "off"):
        return False
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if p.is_file():
        return str(p.resolve())
    return True


def init_truststore() -> None:
    """SSL 검증을 OS CA와 맞추기. 여러 번 호출해도 한 번만 수행."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    v = get_requests_verify()
    if v is False:
        return
    if isinstance(v, str):
        return

    use_sys = (os.environ.get("HTTP_USE_SYSTEM_CA") or "1").strip().lower()
    if use_sys in ("0", "false", "no", "off"):
        return

    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass
