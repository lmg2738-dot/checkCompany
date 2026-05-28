# -*- coding: utf-8 -*-
"""Resend REST API — https://resend.com/"""

from __future__ import annotations

import os
from typing import Any

import requests

_SESSION = requests.Session()
_API_URL = "https://api.resend.com/emails"


def get_resend_api_key() -> str:
    return (os.environ.get("RESEND_API_KEY") or "").strip()


def resend_configured() -> bool:
    return bool(get_resend_api_key() and _from_address() and _to_addresses())


def _from_address() -> str:
    return (os.environ.get("RESEND_FROM") or "").strip()


def _to_addresses() -> list[str]:
    raw = (os.environ.get("RESEND_TO") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


def config_status() -> dict[str, Any]:
    return {
        "resend_api_key_set": bool(get_resend_api_key()),
        "resend_from_set": bool(_from_address()),
        "resend_to_set": bool(_to_addresses()),
        "resend_ready": resend_configured(),
    }


def send_html_email(
    *,
    subject: str,
    html: str,
    to: list[str] | None = None,
    from_addr: str | None = None,
) -> dict[str, Any]:
    key = get_resend_api_key()
    if not key:
        raise RuntimeError("RESEND_API_KEY 가 설정되지 않았습니다.")

    sender = (from_addr or _from_address()).strip()
    recipients = to if to is not None else _to_addresses()
    if not sender:
        raise RuntimeError("RESEND_FROM 이 설정되지 않았습니다.")
    if not recipients:
        raise RuntimeError("RESEND_TO 가 설정되지 않았습니다.")

    r = _SESSION.post(
        _API_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "from": sender,
            "to": recipients,
            "subject": subject,
            "html": html,
        },
        timeout=60,
    )
    if r.status_code >= 400:
        detail = (r.text or "")[:800]
        raise RuntimeError(f"Resend 발송 실패 HTTP {r.status_code}: {detail}")

    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    return {"ok": True, "resend": body, "to": recipients}
