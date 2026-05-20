# -*- coding: utf-8 -*-
"""Google News RSS 기반 키워드 스캔 (API 키 불필요)."""

from __future__ import annotations

import re
import urllib.parse
from xml.etree import ElementTree

from services import ssl_setup

ssl_setup.init_truststore()

import requests

# 부정 이슈·채권 관련 키워드 (가중치 다름)
NEGATIVE_GENERAL = [
    "폐업", "파산", "회생", "워크아웃", "구조조정", "대규모", "적자", "자본잠식",
    "영업중단", "압류", "가압류", "기업회생", "법정관리", "상장폐지", "관리종목",
    "소송", "분쟁", "약정위반", "채무불이행", "연체", "부도",
]
NEGATIVE_FINANCIAL = [
    "감사", "의견거절", "감사보고서", "분식", "횡령", "배임", "수사",
    "신용등급", "등급하향", "부정적", "우려",
]

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; ChurnMonitor/1.0; +local)",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
)
SESSION.verify = ssl_setup.get_requests_verify()


def _fetch_rss(query: str, max_items: int = 15) -> list[dict]:
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    r = SESSION.get(url, timeout=15)
    r.raise_for_status()
    root = ElementTree.fromstring(r.content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        blob = f"{title} {desc}"
        items.append(
            {
                "title": title,
                "link": link,
                "pubDate": pub,
                "text": blob,
            }
        )
        if len(items) >= max_items:
            break
    return items


def _count_keywords(text: str, words: list[str]) -> int:
    t = text.lower()
    n = 0
    for w in words:
        if w.lower() in t:
            n += 1
    return n


def score_news_for_company(company_name: str) -> dict:
    """
    뉴스 RSS를 검색해 news_risk ∈ [0,1] 및 메타데이터 반환.
    """
    name = (company_name or "").strip()
    if not name:
        return {
            "news_risk": 0.25,
            "credit_from_news": 0.2,
            "article_count": 0,
            "negative_hits": 0,
            "financial_hits": 0,
            "sample_titles": [],
            "articles": [],
            "error": "회사명 없음",
        }

    try:
        items = _fetch_rss(name, max_items=12)
    except Exception as e:
        return {
            "news_risk": 0.35,
            "credit_from_news": 0.22,
            "article_count": 0,
            "negative_hits": 0,
            "financial_hits": 0,
            "sample_titles": [],
            "articles": [],
            "error": str(e),
        }

    neg = fin = 0
    titles = []
    for it in items:
        blob = it["text"]
        neg += _count_keywords(blob, NEGATIVE_GENERAL)
        fin += _count_keywords(blob, NEGATIVE_FINANCIAL)
        if it["title"]:
            titles.append(it["title"][:120])

    # 일반 이슈(운영·법적) vs 재무·신용 키워드 분리 → churn_model의 N / C 축
    news_risk = min(1.0, 0.11 * neg + 0.04 * fin)
    credit_from_news = min(1.0, 0.16 * fin + 0.05 * neg)
    if not items:
        news_risk = 0.2
        credit_from_news = 0.15

    return {
        "news_risk": round(news_risk, 4),
        "credit_from_news": round(credit_from_news, 4),
        "article_count": len(items),
        "negative_hits": neg,
        "financial_hits": fin,
        "sample_titles": titles[:5],
        "articles": items[:5],
        "error": None,
    }


def normalize_biz_number(biz: str) -> str:
    """숫자만 추출 (10자리 비교용)."""
    digits = re.sub(r"\D", "", biz or "")
    return digits[:10]
