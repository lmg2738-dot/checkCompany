# -*- coding: utf-8 -*-
"""Google News RSS 기반 키워드 스캔 (API 키 불필요)."""

from __future__ import annotations

import calendar
import re
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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

NEWS_INCLUDE_MONTHS = 2
_MAX_RAW_FEED_ITEMS = 50


def _parse_pub_date_utc(pub: str) -> datetime | None:
    s = (pub or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _subtract_calendar_months(dt: datetime, months: int) -> datetime:
    """dt 기준으로 `months` 달 전의 같은(또는 말일까지 맞춘) 날짜."""
    year, month = dt.year, dt.month - months
    while month <= 0:
        month += 12
        year -= 1
    last_dom = calendar.monthrange(year, month)[1]
    day = min(dt.day, last_dom)
    return dt.astimezone(timezone.utc).replace(year=year, month=month, day=day)


def news_recency_cutoff_utc(months_back: int = NEWS_INCLUDE_MONTHS) -> datetime:
    """현재 시각 기준 과거 경계선(포함되면 반영 대상): `months_back`달 전 같은 날 시작."""
    return _subtract_calendar_months(datetime.now(timezone.utc), months_back)


def _fetch_recent_news_items(
    query: str,
    cutoff_utc: datetime,
    *,
    max_keep: int = 12,
    max_scan_entries: int = _MAX_RAW_FEED_ITEMS,
) -> list[dict]:
    """RSS에서 최대 `max_scan_entries`개 항목을 읽되, 발행 시각 기준 이내만 점수·표시 대상으로 유지한다."""
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    r = SESSION.get(url, timeout=15)
    r.raise_for_status()
    root = ElementTree.fromstring(r.content)
    out: list[dict] = []
    scanned = 0
    for item in root.findall(".//item"):
        if scanned >= max_scan_entries:
            break
        scanned += 1
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        blob = f"{title} {desc}"
        pu = _parse_pub_date_utc(pub)
        if pu is None or pu < cutoff_utc:
            continue
        out.append(
            {
                "title": title,
                "link": link,
                "pubDate": pub,
                "text": blob,
                "published_utc": pu.isoformat(),
            }
        )
        if len(out) >= max_keep:
            break
    return out


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
    반영 범위: 분석 실행 시점 기준 직전 2달(달력 월)·그 이후에 발행된 기사만.
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

    cutoff = news_recency_cutoff_utc()
    try:
        items = _fetch_recent_news_items(name, cutoff, max_keep=12, max_scan_entries=_MAX_RAW_FEED_ITEMS)
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
        "news_recent_months": NEWS_INCLUDE_MONTHS,
        "news_cutoff_utc_iso": cutoff.isoformat(),
        "error": None,
    }


def normalize_biz_number(biz: str) -> str:
    """숫자만 추출 (10자리 비교용)."""
    digits = re.sub(r"\D", "", biz or "")
    return digits[:10]
