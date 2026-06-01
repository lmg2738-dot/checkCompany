# -*- coding: utf-8 -*-
"""Resend REST API — https://resend.com/"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

_SESSION = requests.Session()
_API_URL = "https://api.resend.com/emails"

# Resend `to` 는 ASCII 이메일 주소만 허용 (한글 표시명·주소 전체 문자열 불가)
_EMAIL_ADDR_RE = re.compile(
    r"([a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+)"
)


def get_resend_api_key() -> str:
    return (os.environ.get("RESEND_API_KEY") or "").strip()


def extract_email_address(raw: str) -> str:
    """
    '홍길동 <user@example.com>' 또는 'user@example.com' 에서 순수 이메일만 추출.
    Resend `to` 필드는 non-ASCII 가 포함되면 422.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("빈 주소입니다.")

    bracket = re.search(r"<([^<>]+)>", text)
    if bracket:
        text = bracket.group(1).strip()

    m = _EMAIL_ADDR_RE.search(text)
    if m:
        addr = m.group(1)
    elif "@" in text and text.isascii():
        addr = text.strip().strip('"').strip("'")
    else:
        raise ValueError(
            f"유효한 이메일 주소를 찾을 수 없습니다: {raw!r}. "
            "RESEND_TO 는 user@company.com 형식만 넣어 주세요 (한글 이름 없이)."
        )

    if not addr.isascii():
        raise ValueError(
            f"이메일 주소에 한글 등 non-ASCII 문자가 있습니다: {addr!r}. "
            "표시 이름은 넣지 말고 주소만 설정하세요."
        )
    if not _EMAIL_ADDR_RE.fullmatch(addr):
        raise ValueError(f"이메일 형식이 올바르지 않습니다: {addr!r}")

    return addr


def format_from_address(raw: str) -> str:
    """
    발신 `from` — 표시명이 ASCII 이면 'Name <email>' 유지, 아니면 email 만.
    """
    text = (raw or "").strip()
    if not text:
        return ""

    try:
        email = extract_email_address(text)
    except ValueError:
        return text

    if "<" in raw and ">" in raw:
        display = raw.split("<", 1)[0].strip().strip('"').strip("'")
        if display and display.isascii():
            return f"{display} <{email}>"
    return email


def parse_recipient_list(raw: str) -> list[str]:
    """쉼표·세미콜론 구분 수신자 → ASCII 이메일 목록."""
    if not (raw or "").strip():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        addr = extract_email_address(part)
        key = addr.lower()
        if key not in seen:
            seen.add(key)
            out.append(addr)
    return out


def _from_address() -> str:
    return format_from_address((os.environ.get("RESEND_FROM") or "").strip())


def _to_addresses() -> list[str]:
    return parse_recipient_list(os.environ.get("RESEND_TO") or "")


def resend_configured() -> bool:
    return bool(get_resend_api_key() and _from_address() and _to_addresses())


def _resend_signup_email() -> str:
    """Resend 가입 이메일(선택). onboarding@ 테스트 발신 시 수신 허용 여부 판단용."""
    raw = (
        os.environ.get("RESEND_SIGNUP_EMAIL")
        or os.environ.get("RESEND_ACCOUNT_EMAIL")
        or ""
    ).strip()
    if not raw:
        return ""
    try:
        return extract_email_address(raw)
    except ValueError:
        return ""


def resend_delivery_warning() -> str | None:
    """
    health/API 안내용. 발송을 막지 않음.

    Resend 규칙: 도메인 미인증 + onboarding@resend.dev 이면
    **가입 시 등록한 이메일**로만 수신 가능. mplace@cj.net 이 가입 주소면 정상 조합.
    """
    if not resend_configured():
        return None
    sender = _from_address().lower()
    recipients = _to_addresses()
    if "onboarding@resend.dev" not in sender:
        return None

    signup = _resend_signup_email()
    if signup:
        bad = [r for r in recipients if r.lower() != signup.lower()]
        if bad:
            return (
                f"테스트 발신(onboarding@resend.dev)은 Resend 가입 이메일({signup})로만 "
                f"보낼 수 있습니다. 현재 수신: {', '.join(recipients)}. "
                "다른 주소로내려면 cj.net 도메인 Verified 후 RESEND_FROM 변경."
            )
        return None

    # 가입 이메일 미설정: mplace@cj.net 이 가입 주소면 발송 가능 — 차단하지 않음
    return (
        "테스트 발신(onboarding@resend.dev) 사용 중. "
        f"RESEND_TO={', '.join(recipients)} 가 Resend 가입 이메일이면 발송 가능합니다. "
        "다른 수신자·403 오류 시 resend.com/domains 에서 cj.net 인증 후 "
        "RESEND_FROM=mplace@cj.net 등으로 변경하세요. "
        "(선택) RESEND_SIGNUP_EMAIL 에 가입 이메일을 넣으면 health 에서 수신 불일치만 경고합니다."
    )


def config_status() -> dict[str, Any]:
    raw_to = (os.environ.get("RESEND_TO") or "").strip()
    to_parse_error: str | None = None
    recipients: list[str] = []
    if raw_to:
        try:
            recipients = parse_recipient_list(raw_to)
        except ValueError as e:
            to_parse_error = str(e)

    warning = resend_delivery_warning() if resend_configured() else None
    # warning 이 있어도 가입 이메일과 일치하면 발송 가능 — API 로 최종 판단
    blocks_send = False
    if warning and _resend_signup_email():
        signup = _resend_signup_email()
        try:
            recips = parse_recipient_list(raw_to) if raw_to else []
            blocks_send = any(r.lower() != signup.lower() for r in recips)
        except ValueError:
            blocks_send = True

    return {
        "resend_api_key_set": bool(get_resend_api_key()),
        "resend_from_set": bool((os.environ.get("RESEND_FROM") or "").strip()),
        "resend_from_resolved": _from_address() or None,
        "resend_to_set": bool(raw_to),
        "resend_to_resolved": recipients,
        "resend_to_parse_error": to_parse_error,
        "resend_signup_email": _resend_signup_email() or None,
        "resend_ready": resend_configured(),
        "resend_delivery_warning": warning,
        "resend_blocks_send": blocks_send,
        "resend_can_deliver": resend_configured() and not blocks_send,
    }


def _friendly_resend_error(
    status_code: int,
    detail: str,
    *,
    recipients: list[str],
    sender: str,
) -> str:
    """Resend API 오류 → 화면에 보여줄 한글 안내."""
    msg = detail
    try:
        js = json.loads(detail)
        if isinstance(js, dict) and js.get("message"):
            msg = str(js["message"])
    except Exception:
        pass

    low = msg.lower()
    if status_code == 403 and (
        "domain is not verified" in low or "not verified" in low and "domain" in low
    ):
        domain_hint = "cj.net"
        m_dom = re.search(r"the\s+([a-z0-9.-]+)\s+domain\s+is\s+not\s+verified", low)
        if m_dom:
            domain_hint = m_dom.group(1)
        return (
            f"Resend: {domain_hint} 도메인이 아직 'Verified' 상태가 아닙니다. "
            "https://resend.com/domains 에서 도메인을 열고 DNS 레코드(SPF·DKIM 등)를 "
            "회사 DNS에 모두 추가한 뒤, 상태가 Verified 로 바뀔 때까지 기다려 주세요. "
            f"(현재 발신: {sender})"
        )

    if status_code == 403 and (
        "testing emails" in low or "verify a domain" in low
    ):
        allowed_hint = ""
        m = re.search(
            r"your own email address\s*\(([^)]+)\)",
            msg,
            re.IGNORECASE,
        )
        if m:
            allowed_hint = m.group(1).strip()
        lines = [
            "Resend 무료·테스트 모드 제한입니다. onboarding@resend.dev 등 기본 발신 주소로는 "
            "가입 시 등록한 이메일로만 보낼 수 있습니다.",
        ]
        if allowed_hint:
            lines.append(
                f"지금은 RESEND_TO 를 {allowed_hint} 로 두고 테스트하거나, "
            )
        else:
            lines.append("지금은 RESEND_TO 를 Resend 가입 이메일로 두고 테스트하거나, ")
        lines.append(
            "다른 회사 메일(예: @cj.net)로내려면 resend.com/domains 에서 도메인을 인증하고 "
            "RESEND_FROM 을 해당 도메인 주소(예: report@yourdomain.com)로 바꿔 주세요."
        )
        lines.append(f"(현재 발신: {sender}, 수신: {', '.join(recipients)})")
        return " ".join(lines)

    return f"Resend 발송 실패 HTTP {status_code}: {msg}"


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

    sender = format_from_address((from_addr or os.environ.get("RESEND_FROM") or "").strip())
    if not sender:
        raise RuntimeError("RESEND_FROM 이 설정되지 않았습니다.")

    if to is not None:
        recipients = []
        for item in to:
            recipients.append(extract_email_address(item))
    else:
        recipients = _to_addresses()

    if not recipients:
        raise RuntimeError(
            "RESEND_TO 가 설정되지 않았거나 파싱할 수 없습니다. "
            "예: user@company.com (한글 표시명 없이)"
        )

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
        raise RuntimeError(
            _friendly_resend_error(
                r.status_code,
                detail,
                recipients=recipients,
                sender=sender,
            )
        )

    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    return {"ok": True, "resend": body, "to": recipients}
