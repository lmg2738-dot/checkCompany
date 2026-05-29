# -*- coding: utf-8 -*-
"""매일 위험 상위 10개 업체 리포트 이메일."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from churn_model import composite_weights_formula

KST = timezone(timedelta(hours=9))


def _report_date_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def email_subject() -> str:
    return f"고객 이탈·이슈 위험 리포트({_report_date_kst()})"


def _email_enabled() -> bool:
    raw = (os.environ.get("DAILY_EMAIL_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def dashboard_url() -> str:
    url = (os.environ.get("DASHBOARD_URL") or "https://check-company.vercel.app/").strip()
    if not url.endswith("/"):
        url += "/"
    return url


def build_email_html(
    sorted_results: list[dict[str, Any]],
    *,
    prep_meta: dict[str, Any] | None = None,
    report_date: str | None = None,
) -> str:
    from flask import render_template

    prep = prep_meta or {}
    top10 = sorted_results[:10]
    return render_template(
        "email/daily_risk_report.html",
        report_date=report_date or _report_date_kst(),
        top10=top10,
        analysis_prep=prep,
        composite_weights_with_m=composite_weights_formula(include_market=True),
        composite_weights_no_m=composite_weights_formula(include_market=False),
        dashboard_url=dashboard_url(),
        meter_bar_width_px=360,
    )


def send_daily_risk_report(
    sorted_results: list[dict[str, Any]],
    *,
    prep_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from services.resend_client import send_html_email

    if not _email_enabled():
        return {"ok": False, "skipped": True, "reason": "DAILY_EMAIL_ENABLED=0"}

    if not sorted_results:
        raise RuntimeError("분석 결과가 없어 이메일을 보낼 수 없습니다.")

    html = build_email_html(sorted_results, prep_meta=prep_meta)
    return send_html_email(subject=email_subject(), html=html)
