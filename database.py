"""SQLite 저장소.

테이블 3개만 사용한다.
  prices    - 가격 시계열 (상품명/가격/통화/단위/날짜/출처/출처URL 포함)
  news      - 수집한 뉴스 목록
  summaries - AI 요약 캐시 (같은 입력이면 재호출하지 않는다)

Streamlit 은 요청마다 다른 스레드에서 코드를 실행하므로
커넥션을 전역으로 들고 있지 않고 호출마다 열고 닫는다.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "steel.db"

# Dashboard 에서 다루는 품목. 화면 표시 순서이기도 하다.
#
# 철스크랩은 무료·공개 시계열 소스를 확보하지 못해 제외했다.
# 소스를 찾으면 여기에 "scrap": "철스크랩" 을 되살리고
# data_collector 에 수집 함수를 추가하면 나머지 코드는 그대로 동작한다.
ITEMS = {
    "iron_ore": "철광석",
    "coal": "석탄",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item        TEXT NOT NULL,          -- scrap / iron_ore / coal
    item_name   TEXT NOT NULL,          -- 상품명 (철스크랩 ...)
    price       REAL NOT NULL,          -- 가격
    currency    TEXT NOT NULL,          -- 통화 (USD / KRW)
    unit        TEXT NOT NULL,          -- 단위 (USD/mt ...)
    spec        TEXT,                   -- 규격 (62% Fe, CFR China ...)
    date        TEXT NOT NULL,          -- 가격 기준일 YYYY-MM-DD
    source      TEXT NOT NULL,          -- 출처명
    source_url  TEXT NOT NULL,          -- 출처 URL
    source_type TEXT NOT NULL DEFAULT 'auto',  -- auto(자동수집) / manual(수기입력)
    collected_at TEXT NOT NULL,         -- 수집 시각 ISO8601
    UNIQUE (item, date, source)
);

CREATE INDEX IF NOT EXISTS idx_prices_item_date ON prices (item, date);

CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    published_at TEXT,                  -- YYYY-MM-DD (없으면 NULL)
    source       TEXT NOT NULL,         -- 출처 언론사
    url          TEXT NOT NULL UNIQUE,  -- 원문 링크
    collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    input_hash   TEXT NOT NULL UNIQUE,  -- 입력 데이터 해시
    text         TEXT NOT NULL,
    model        TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
"""

PRICE_COLUMNS = (
    "item", "item_name", "price", "currency", "unit", "spec",
    "date", "source", "source_url", "source_type", "collected_at",
)
NEWS_COLUMNS = ("title", "published_at", "source", "url", "collected_at")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ── 가격 ────────────────────────────────────────────────────────────────

def save_prices(rows: Iterable[dict]) -> int:
    """가격 행들을 저장한다. (item, date, source) 가 같으면 덮어쓴다.

    출처나 출처 URL 이 없는 행은 저장을 거부한다 - 근거 없는 숫자를
    화면에 띄우지 않기 위한 최소한의 방어선이다.
    """
    rows = list(rows)
    if not rows:
        return 0

    clean = []
    for row in rows:
        missing = [k for k in PRICE_COLUMNS if k != "spec" and not row.get(k)]
        if missing:
            raise ValueError(f"가격 행에 필수 항목이 없습니다: {missing} / {row}")
        clean.append(tuple(row.get(col) for col in PRICE_COLUMNS))

    placeholders = ", ".join("?" * len(PRICE_COLUMNS))
    sql = (
        f"INSERT OR REPLACE INTO prices ({', '.join(PRICE_COLUMNS)}) "
        f"VALUES ({placeholders})"
    )
    with get_conn() as conn:
        conn.executemany(sql, clean)
    return len(clean)


def get_price_history(item: str, limit: int = 24) -> pd.DataFrame:
    """한 품목의 최근 시계열을 오래된 순으로 돌려준다."""
    sql = """
        SELECT * FROM (
            SELECT * FROM prices WHERE item = ? ORDER BY date DESC LIMIT ?
        ) ORDER BY date ASC
    """
    with get_conn() as conn:
        df = pd.read_sql_query(sql, conn, params=(item, limit))
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def get_latest_price(item: str) -> dict | None:
    """가장 최근 가격 1건. 데이터가 없으면 None (0 이나 임의값을 만들지 않는다)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM prices WHERE item = ? ORDER BY date DESC LIMIT 1",
            (item,),
        ).fetchone()
    return dict(row) if row else None


def get_previous_price(item: str, before_date: str) -> dict | None:
    """직전 시점 가격 1건. 증감 계산용."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM prices WHERE item = ? AND date < ? ORDER BY date DESC LIMIT 1",
            (item, before_date),
        ).fetchone()
    return dict(row) if row else None


# ── 뉴스 ────────────────────────────────────────────────────────────────

def save_news(rows: Iterable[dict]) -> int:
    """뉴스를 저장한다. URL 이 같으면 무시한다(중복 제거)."""
    rows = list(rows)
    if not rows:
        return 0

    values = [tuple(row.get(col) for col in NEWS_COLUMNS) for row in rows]
    placeholders = ", ".join("?" * len(NEWS_COLUMNS))
    sql = (
        f"INSERT OR IGNORE INTO news ({', '.join(NEWS_COLUMNS)}) "
        f"VALUES ({placeholders})"
    )
    with get_conn() as conn:
        cur = conn.executemany(sql, values)
        return cur.rowcount


def get_news(limit: int = 30) -> pd.DataFrame:
    sql = """
        SELECT title, published_at, source, url, collected_at
        FROM news
        ORDER BY COALESCE(published_at, '') DESC, id DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return pd.read_sql_query(sql, conn, params=(limit,))


# ── AI 요약 캐시 ────────────────────────────────────────────────────────

def get_summary(input_hash: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM summaries WHERE input_hash = ?", (input_hash,)
        ).fetchone()
    return dict(row) if row else None


def save_summary(input_hash: str, text: str, model: str, generated_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO summaries (input_hash, text, model, generated_at) "
            "VALUES (?, ?, ?, ?)",
            (input_hash, text, model, generated_at),
        )


def get_last_collected_at() -> str | None:
    """가격/뉴스를 통틀어 가장 최근 수집 시각. 헤더 표시용."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(t) AS t FROM ("
            "  SELECT MAX(collected_at) AS t FROM prices"
            "  UNION ALL"
            "  SELECT MAX(collected_at) AS t FROM news"
            ")"
        ).fetchone()
    return row["t"] if row and row["t"] else None
