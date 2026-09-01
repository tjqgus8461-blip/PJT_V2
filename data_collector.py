"""데이터 수집기.

수집 우선순위와 실제 확인 결과 (2026-09-01 직접 검증):

  뉴스   1순위 - 스틸데일리 공식 RSS (/rss/allArticle.xml)
                robots.txt 는 /admin/ 만 차단하며, RSS 는 배포 목적의 공식 피드다.
                제목/발행시각/링크만 저장하고 본문은 복제하지 않는다.
         보강  - Google News RSS (철광석/원료탄 등 원자재 키워드)

  철광석 3순위 - World Bank Pink Sheet 월간 엑셀
                'Iron ore, cfr spot' ($/dmtu), 62% Fe, CFR China
  석탄   3순위 - World Bank Pink Sheet 월간 엑셀
                'Coal, Australian' ($/mt), FOB Newcastle 6,000kcal/kg

  스크랩  --   국내 철스크랩은 무료·공개 시계열 소스를 확보하지 못해 제외했다.
                지어낸 값을 넣지 않기 위한 결정이다. 소스를 확보하면
                collect_* 함수를 하나 추가하고 database.ITEMS 에
                "scrap" 을 되살리면 된다.
"""

from __future__ import annotations

import io
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from database import ITEMS

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"

USER_AGENT = "Mozilla/5.0 (compatible; SteelDashboard/1.0; personal MVP)"
HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT = 60
POLITE_DELAY = 1.0  # 연속 요청 사이 최소 간격(초)

WORLDBANK_XLSX_URL = os.getenv(
    "WORLDBANK_XLSX_URL",
    "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026"
    "/related/CMO-Historical-Data-Monthly.xlsx",
)
WORLDBANK_SOURCE = "World Bank Commodity Price Data (Pink Sheet)"

# World Bank 'Monthly Prices' 시트에서 가져올 열.
#   엑셀 4행 = 품목명, 5행 = 단위, 6행부터 = 데이터(A열이 '2026M07' 형식)
WORLDBANK_SERIES = {
    "iron_ore": {
        "column": "Iron ore, cfr spot",
        "spec": "any origin fines, spot, CFR China, 62% Fe",
    },
    "coal": {
        "column": "Coal, Australian",
        "spec": "thermal, FOB Newcastle, 6,000 kcal/kg",
    },
}

STEELDAILY_RSS = "https://www.steeldaily.co.kr/rss/allArticle.xml"
STEELDAILY_NAME = "스틸데일리"

GOOGLE_NEWS_QUERIES = ["철광석 가격", "원료탄 가격", "철스크랩 시황"]
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


# ── 가격: World Bank Pink Sheet ─────────────────────────────────────────

def _fetch_worldbank_sheet() -> pd.DataFrame:
    response = requests.get(WORLDBANK_XLSX_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return pd.read_excel(
        io.BytesIO(response.content), sheet_name="Monthly Prices", header=None
    )


def _parse_unit(raw: str) -> tuple[str, str]:
    """'($/mt)' -> ('USD', 'USD/mt')"""
    match = re.search(r"\(\s*\$\s*/\s*([A-Za-z]+)\s*\)", str(raw))
    if not match:
        return "USD", "USD"
    return "USD", f"USD/{match.group(1)}"


def _parse_period(raw: str) -> str | None:
    """'2026M07' -> '2026-07-01'"""
    match = re.fullmatch(r"(\d{4})M(\d{2})", str(raw).strip())
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-01"


def collect_worldbank(months: int = 36) -> list[dict]:
    """철광석/석탄 월간 가격을 최근 months 개월치 수집한다."""
    raw = _fetch_worldbank_sheet()
    names = raw.iloc[4]
    units = raw.iloc[5]
    collected_at = now_iso()
    rows: list[dict] = []

    for item, config in WORLDBANK_SERIES.items():
        matches = [i for i, name in names.items() if str(name).strip() == config["column"]]
        if not matches:
            raise ValueError(
                f"World Bank 엑셀에서 '{config['column']}' 열을 찾지 못했습니다. "
                "엑셀 양식이 바뀌었을 수 있습니다."
            )
        col = matches[0]
        currency, unit = _parse_unit(units[col])

        series = raw.iloc[6:, [0, col]].dropna()
        for _, (period, value) in series.iterrows():
            date = _parse_period(period)
            if date is None:
                continue
            # 결측치는 '..' 문자열로 들어온다. 숫자가 아니면 건너뛴다.
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "item": item,
                    "item_name": ITEMS[item],
                    "price": round(price, 2),
                    "currency": currency,
                    "unit": unit,
                    "spec": config["spec"],
                    "date": date,
                    "source": WORLDBANK_SOURCE,
                    "source_url": WORLDBANK_XLSX_URL,
                    "source_type": "auto",
                    "collected_at": collected_at,
                }
            )

    # 품목별로 최근 months 개만 남긴다.
    trimmed: list[dict] = []
    for item in WORLDBANK_SERIES:
        item_rows = sorted(
            (r for r in rows if r["item"] == item), key=lambda r: r["date"]
        )
        trimmed.extend(item_rows[-months:])
    return trimmed


# ── 뉴스 ────────────────────────────────────────────────────────────────

def _entry_date(entry) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")


def _clean_title(title: str) -> str:
    """RSS 제목에 섞여 오는 HTML 태그/엔티티를 벗겨내고 공백을 정리한다."""
    text = BeautifulSoup(title or "", "html.parser").get_text()
    return re.sub(r"\s+", " ", text).strip()


def _fetch_feed(url: str):
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return feedparser.parse(response.content)


def collect_news(limit: int = 40) -> list[dict]:
    """스틸데일리 RSS(1순위) + Google News RSS(보강)."""
    collected_at = now_iso()
    rows: list[dict] = []
    seen_urls: set[str] = set()

    def add(title: str, link: str, source: str, published: str | None) -> None:
        title = _clean_title(title)
        if not title or not link or link in seen_urls:
            return
        seen_urls.add(link)
        rows.append(
            {
                "title": title,
                "published_at": published,
                "source": source,
                "url": link,
                "collected_at": collected_at,
            }
        )

    # 1순위: 스틸데일리 공식 RSS
    feed = _fetch_feed(STEELDAILY_RSS)
    for entry in feed.entries:
        add(entry.get("title", ""), entry.get("link", ""), STEELDAILY_NAME, _entry_date(entry))

    # 보강: Google News RSS (원자재 키워드)
    for query in GOOGLE_NEWS_QUERIES:
        time.sleep(POLITE_DELAY)
        feed = _fetch_feed(GOOGLE_NEWS_RSS.format(query=requests.utils.quote(query)))
        for entry in feed.entries:
            title = entry.get("title", "")
            source = (entry.get("source") or {}).get("title")
            if not source and " - " in title:
                title, source = title.rsplit(" - ", 1)
            add(title, entry.get("link", ""), source or "Google News", _entry_date(entry))

    rows.sort(key=lambda r: r["published_at"] or "", reverse=True)
    return rows[:limit]


# ── 전체 수집 ───────────────────────────────────────────────────────────

def collect_all() -> dict:
    """모든 수집기를 실행한다. 하나가 실패해도 나머지는 진행한다."""
    import database as db

    db.init_db()
    result = {"prices": 0, "news": 0, "errors": []}

    try:
        result["prices"] += db.save_prices(collect_worldbank())
    except Exception as exc:  # noqa: BLE001 - 개별 소스 실패를 격리한다
        result["errors"].append(f"World Bank (철광석/석탄): {type(exc).__name__} - {exc}")

    try:
        result["news"] += db.save_news(collect_news())
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"뉴스: {type(exc).__name__} - {exc}")

    return result


if __name__ == "__main__":
    outcome = collect_all()
    print(f"가격 {outcome['prices']}건, 뉴스 {outcome['news']}건 저장")
    for error in outcome["errors"]:
        print("  [실패]", error)
