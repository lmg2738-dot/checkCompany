# -*- coding: utf-8 -*-
"""고객 한 건에 대해 외부 공개 소스를 모아 이탈 위험 추정."""

from __future__ import annotations

from typing import Any

from churn_model import ChurnEstimate, estimate_churn
from services.dart_service import _normalize_stock_code, dart_enrich, registry_lookup_biz
from services.news_rss import normalize_biz_number, score_news_for_company
from services.stock_kr import market_stress_for_ticker


def analyze_customer(
    company_name: str,
    business_number: str,
    ticker_override: str | None = None,
    dart_corp_code: str | None = None,
) -> dict[str, Any]:
    name = (company_name or "").strip()
    biz = normalize_biz_number(business_number)
    hit = registry_lookup_biz(business_number) if len(biz) == 10 else None
    if not name and hit and hit.get("corp_name"):
        name = hit["corp_name"].strip()

    ticker = (ticker_override or "").strip() or None
    if not ticker and hit and hit.get("stock_code"):
        t6 = _normalize_stock_code(hit["stock_code"])
        if t6 and len(t6) == 6 and t6 != "000000":
            ticker = t6

    news = score_news_for_company(name)
    dart = dart_enrich(name, biz, stock_code=ticker, corp_code_override=dart_corp_code)

    if not name:
        cp = dart.get("company_profile") or {}
        name = (cp.get("corp_name") or "").strip()

    if not ticker and dart.get("listed") and dart.get("stock_code"):
        ticker = dart["stock_code"]
    if not ticker and hit and hit.get("stock_code"):
        t6 = _normalize_stock_code(hit["stock_code"])
        if t6 and len(t6) == 6 and t6 != "000000":
            ticker = t6

    mkt = market_stress_for_ticker(ticker)
    tc = _normalize_stock_code((ticker or "").strip())
    ticker_available = len(tc) == 6 and tc != "000000"
    market_observed = mkt.get("return_1m") is not None

    boost = float(dart.get("credit_risk_boost") or 0.0)
    credit_risk = min(
        1.0,
        float(news.get("credit_from_news") or 0.0) + boost + (0.05 if news.get("error") else 0.0),
    )

    row = {
        "company_name": name,
        "business_number": biz,
        "ticker": ticker,
        "news_risk": float(news.get("news_risk") or 0.0),
        "market_risk": float(mkt.get("market_risk") or 0.0),
        "credit_risk": credit_risk,
        "engagement_risk": None,
        "engagement_unknown": True,
    }
    est: ChurnEstimate = estimate_churn(
        engagement_risk=row["engagement_risk"],
        news_risk=row["news_risk"],
        market_risk=row["market_risk"],
        credit_risk=row["credit_risk"],
        engagement_is_unknown=True,
        market_in_composite=ticker_available,
    )

    return {
        "estimate": est,
        "company_name": name,
        "business_number": biz,
        "ticker": ticker,
        "ticker_available": ticker_available,
        "market_observed": market_observed,
        "listed": bool(dart.get("listed")),
        "dart_corp_code": dart.get("corp_code"),
        "news": news,
        "market": mkt,
        "dart": dart,
        "composite_score": est.composite_score,
        "estimated_churn_probability": est.estimated_churn_probability,
        "market_in_composite": est.market_in_composite,
    }
