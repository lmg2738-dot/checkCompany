# -*- coding: utf-8 -*-
"""
고객 이탈 위험 추정 모델.

복합 위험 점수 S ∈ [0,1]를 아래 가중합으로 정의하고,
이를 이탈 확률 추정에 로지스틱 링크를 적용합니다.

    S = w_E·E + w_N·N + w_M·M + w_C·C

    - E: 발송·이용 저하 지표 (0=정상, 1=심각). 발송 데이터 미제공 시 불확실성 반영 중립값.
    - N: 뉴스·언론 기반 부정 이슈 강도 (정규화 0~1)
    - M: 시장·주가 스트레스 (상장·시세 조회 성공 시만 복합에 반영; 종목 없으면 복합 제외·N·C 재정규화)
    - C: 신용·재무 스트레스 프록시 (공시 키워드 + 부정 재무 뉴스; 유료 신용 API 없음)

가중치는 합이 1이 되도록 하며, 발송 데이터 연동 시 w_E를 상향 조정할 수 있습니다.

추정 이탈 확률 (보조 지표):

    P_churn = σ( β₀ + β₁·S ),  σ(x) = 1/(1+e^{-x})

기본값: β₀=-2.0, β₁=4.0 → S=0.5 부근에서 P≈0.5 근처가 되도록 스케일링.
실제 캘리브레이션은 사내 이탈 실적과 회귀·보정이 필요합니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp
from typing import Any

# 가중치 (합 = 1.0)
W_ENGAGEMENT = 0.20
W_NEWS = 0.30
W_MARKET = 0.25
W_CREDIT = 0.25

# 발송 데이터 없을 때 E에 쓰는 불확실성 중립값 (약한 리스크로 두지 않음: 0.5)
DEFAULT_ENGAGEMENT_RISK = 0.50

# 로지스틱 이탈 확률 파라미터
LOGIT_INTERCEPT = -2.0
LOGIT_SLOPE = 4.0

# M 제외 시 E+N+C 가중치 재정규화 (합 1)
_W_NO_MARKET_SUM = W_ENGAGEMENT + W_NEWS + W_CREDIT
W_ENGAGEMENT_NO_M = W_ENGAGEMENT / _W_NO_MARKET_SUM
W_NEWS_NO_M = W_NEWS / _W_NO_MARKET_SUM
W_CREDIT_NO_M = W_CREDIT / _W_NO_MARKET_SUM


def composite_weights_formula(*, include_market: bool = True) -> str:
    if include_market:
        return "S = 0.20·E + 0.30·N + 0.25·M + 0.25·C"
    return (
        f"S = {W_ENGAGEMENT_NO_M:.2f}·E + {W_NEWS_NO_M:.2f}·N + {W_CREDIT_NO_M:.2f}·C "
        "(종목 없음 → M 복합 제외)"
    )


@dataclass(frozen=True)
class ChurnEstimate:
    """단일 고객사에 대한 이탈 위험 산출 결과."""

    composite_score: float  # 0~100
    engagement_risk: float
    news_risk: float
    market_risk: float
    credit_risk: float
    estimated_churn_probability: float  # 0~1
    notes: str
    market_in_composite: bool = True


def logistic(x: float) -> float:
    try:
        return 1.0 / (1.0 + exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def compute_composite(
    engagement_risk: float,
    news_risk: float,
    market_risk: float,
    credit_risk: float,
    *,
    include_market: bool = True,
) -> float:
    """가중합 S (0~1). include_market=False면 M 제외 후 E·N·C만 재정규화."""
    e = max(0.0, min(1.0, engagement_risk))
    n = max(0.0, min(1.0, news_risk))
    m = max(0.0, min(1.0, market_risk))
    c = max(0.0, min(1.0, credit_risk))
    if include_market:
        return W_ENGAGEMENT * e + W_NEWS * n + W_MARKET * m + W_CREDIT * c
    return W_ENGAGEMENT_NO_M * e + W_NEWS_NO_M * n + W_CREDIT_NO_M * c


def composite_to_churn_probability(s: float) -> float:
    """S ∈ [0,1] → 추정 이탈 확률."""
    s = max(0.0, min(1.0, s))
    return logistic(LOGIT_INTERCEPT + LOGIT_SLOPE * s)


def estimate_churn(
    *,
    engagement_risk: float | None = None,
    news_risk: float = 0.0,
    market_risk: float = 0.0,
    credit_risk: float = 0.0,
    engagement_is_unknown: bool = True,
    market_in_composite: bool = True,
) -> ChurnEstimate:
    """
    engagement_risk: 발송 추이 기반 0~1. None이면 기본 불확실성 값 사용.
    engagement_is_unknown: True면 발송 데이터 미연동으로 간주하고 notes에 표시.
    """
    e = DEFAULT_ENGAGEMENT_RISK if engagement_risk is None else engagement_risk
    s = compute_composite(
        e,
        news_risk,
        market_risk,
        credit_risk,
        include_market=market_in_composite,
    )
    score_100 = round(s * 100.0, 2)
    p = composite_to_churn_probability(s)
    parts = []
    if engagement_is_unknown or engagement_risk is None:
        parts.append("발송데이터 미반영(E=중립 불확실성)")
    if not market_in_composite:
        parts.append(
            f"종목 없음·M 복합 제외(N·C 가중: {W_NEWS_NO_M:.0%}/{W_CREDIT_NO_M:.0%})"
        )
    return ChurnEstimate(
        composite_score=score_100,
        engagement_risk=e,
        news_risk=news_risk,
        market_risk=market_risk,
        credit_risk=credit_risk,
        estimated_churn_probability=round(p, 4),
        notes="; ".join(parts) if parts else "발송데이터 연동 시 E 가중 재조정 권장",
        market_in_composite=market_in_composite,
    )


def row_to_estimate(row: dict[str, Any]) -> ChurnEstimate:
    """서비스 레이어에서 채운 dict를 ChurnEstimate로 변환."""
    return estimate_churn(
        engagement_risk=row.get("engagement_risk"),
        news_risk=float(row.get("news_risk") or 0),
        market_risk=float(row.get("market_risk") or 0),
        credit_risk=float(row.get("credit_risk") or 0),
        engagement_is_unknown=row.get("engagement_unknown", True),
        market_in_composite=bool(row.get("ticker_available", True)),
    )
