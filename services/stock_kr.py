# -*- coding: utf-8 -*-
"""상장 종목 시세 스트레스 (yfinance, 공개 데이터)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from services import ssl_setup

ssl_setup.init_truststore()

try:
    import yfinance as yf
except ImportError:
    yf = None


def market_stress_for_ticker(ticker: str | None) -> dict[str, Any]:
    """
    최근 약 30거래일 수익률·변동성 기반 market_risk ∈ [0,1].
    티커 없음 또는 오류 시 중립~약간 높은 불확실성.
    """
    code = (ticker or "").strip()
    if not code or yf is None:
        return {
            "market_risk": 0.42,
            "ticker": code or None,
            "return_1m": None,
            "note": "종목코드 없음 또는 yfinance 미설치",
        }

    # 한국 거래소: 6자리 숫자면 .KS / .KQ 시도
    sym = code
    if code.isdigit() and len(code) == 6:
        for suf in (".KS", ".KQ"):
            try:
                t = yf.Ticker(code + suf)
                end = datetime.utcnow()
                start = end - timedelta(days=45)
                hist = t.history(start=start, end=end)
                if hist is not None and len(hist) >= 5:
                    return _calc_from_hist(code + suf, hist)
            except Exception:
                continue
        sym = code + ".KS"

    try:
        t = yf.Ticker(sym)
        end = datetime.utcnow()
        start = end - timedelta(days=45)
        hist = t.history(start=start, end=end)
        if hist is None or len(hist) < 5:
            return {
                "market_risk": 0.45,
                "ticker": sym,
                "return_1m": None,
                "note": "시세 데이터 부족",
            }
        return _calc_from_hist(sym, hist)
    except Exception as e:
        return {
            "market_risk": 0.45,
            "ticker": sym,
            "return_1m": None,
            "note": str(e),
        }


def _calc_from_hist(sym: str, hist: Any) -> dict[str, Any]:
    closes = hist["Close"].dropna()
    if len(closes) < 5:
        return {
            "market_risk": 0.45,
            "ticker": sym,
            "return_1m": None,
            "note": "종가 부족",
        }
    last = float(closes.iloc[-1])
    first = float(closes.iloc[0])
    ret = (last - first) / first if first else 0.0
    # 일간 수익률 표준편차 (변동성)
    daily = closes.pct_change().dropna()
    vol = float(daily.std()) if len(daily) else 0.0

    # 낙폭·변동성을 0~1 스트레스로 매핑
    drawdown_stress = min(1.0, max(0.0, -ret * 3.0))  # -33% → ~1
    vol_stress = min(1.0, vol * 80.0)
    stress = min(1.0, 0.55 * drawdown_stress + 0.45 * vol_stress)

    return {
        "market_risk": round(stress, 4),
        "ticker": sym,
        "return_1m": round(ret, 4),
        "volatility_daily": round(vol, 6),
        "note": None,
    }
