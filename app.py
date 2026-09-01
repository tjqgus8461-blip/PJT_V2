"""철강 원자재 대시보드 - Streamlit 앱.

실행:
    streamlit run app.py
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta

import plotly.express as px
import streamlit as st

import ai
import database as db
from data_collector import collect_all

st.set_page_config(
    page_title="철강 원자재 대시보드",
    page_icon="🔩",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 이 시간이 지나면 화면을 열 때 알아서 다시 수집한다.
REFRESH_AFTER_HOURS = 6

ACCENT = "#3B5BDB"
MUTED = "#6B7280"

DISCLAIMER = (
    "본 대시보드는 공개된 데이터를 수집해 참고용으로 제공합니다. "
    "각 수치의 정확성은 원 출처를 확인하시기 바라며, 투자·매매 판단의 근거로 사용하지 마십시오. "
    "AI 요약은 자동 생성된 것으로 오류가 있을 수 있습니다."
)

CSS = """
<style>
  .block-container { padding-top: 2.5rem; padding-bottom: 3rem; max-width: 1180px; }
  h1, h2, h3 { letter-spacing: -0.02em; }
  [data-testid="stMetricValue"] { font-size: 2.1rem; font-weight: 650; }
  [data-testid="stMetricLabel"] { color: #6B7280; font-weight: 500; }
  [data-testid="stMetricDelta"] { font-size: 0.85rem; }
  .page-title { font-size: 1.75rem; font-weight: 680; margin: 0 0 .15rem 0; }
  .page-sub { color: #6B7280; font-size: .9rem; margin: 0 0 1.6rem 0; }
  .news-row { padding: .85rem 0; border-bottom: 1px solid #EEF0F3; }
  .news-row:last-child { border-bottom: none; }
  .news-title { font-weight: 560; line-height: 1.45; }
  .news-title a { color: #1A1D23; text-decoration: none; }
  .news-title a:hover { color: #3B5BDB; text-decoration: underline; }
  .news-meta { color: #9AA1AC; font-size: .78rem; margin-top: .2rem; }
  .summary-box {
    background: #F7F8FA; border: 1px solid #E6E8EB; border-radius: .75rem;
    padding: 1.35rem 1.5rem; line-height: 1.75; font-size: 1rem;
  }
</style>
"""


def page_header(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="page-title">{title}</div><div class="page-sub">{subtitle}</div>',
        unsafe_allow_html=True,
    )


def format_price(price: float, currency: str) -> str:
    return f"{price:,.0f}" if currency == "KRW" else f"{price:,.2f}"


# ── 자동 수집 ───────────────────────────────────────────────────────────

def needs_refresh() -> bool:
    last = db.get_last_collected_at()
    if last is None:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last) > timedelta(
            hours=REFRESH_AFTER_HOURS
        )
    except ValueError:
        return True


@st.cache_resource(show_spinner=False)
def auto_collect(_bucket: str) -> dict:
    """버튼을 누르지 않아도 데이터가 채워지도록 알아서 수집한다.

    _bucket 은 시간대 문자열이다. cache_resource 가 같은 시간대 안에서는
    한 번만 실행하므로, 여러 명이 동시에 접속해도 수집이 중복되지 않는다.
    """
    return collect_all()


def ensure_data() -> None:
    if not needs_refresh():
        return
    with st.spinner("최신 데이터를 불러오는 중입니다. 처음 한 번은 30초 정도 걸립니다..."):
        outcome = auto_collect(datetime.now().strftime("%Y%m%d%H"))
    if outcome["errors"] and outcome["prices"] == 0 and outcome["news"] == 0:
        st.error("데이터를 불러오지 못했습니다.")
        for error in outcome["errors"]:
            st.caption(error)


# ── Dashboard ───────────────────────────────────────────────────────────

def render_price_card(item: str, name: str) -> None:
    latest = db.get_latest_price(item)

    with st.container(border=True):
        if latest is None:
            st.metric(name, "—")
            st.caption("데이터 소스 확보 중")
            return

        previous = db.get_previous_price(item, latest["date"])
        delta = None
        if previous:
            change = latest["price"] - previous["price"]
            percent = change / previous["price"] * 100 if previous["price"] else 0
            delta = f"{change:+,.2f} ({percent:+.1f}%)"

        # 단위를 값에 붙이면 열 폭에서 잘리므로 라벨로 뺀다.
        st.metric(
            f"{name} · {latest['unit']}",
            format_price(latest["price"], latest["currency"]),
            delta=delta,
            help=f"직전 시점: {previous['date']}" if previous else None,
        )
        st.caption(f"기준일 **{latest['date']}** · {latest['spec'] or '-'}")
        st.caption(f"출처 [{latest['source']}]({latest['source_url']})")


def render_dashboard() -> None:
    last = db.get_last_collected_at()
    page_header("Dashboard", f"마지막 갱신 {last}" if last else "데이터 없음")

    columns = st.columns(len(db.ITEMS), gap="medium")
    for column, (item, name) in zip(columns, db.ITEMS.items()):
        with column:
            render_price_card(item, name)

    available = {
        item: name
        for item, name in db.ITEMS.items()
        if not db.get_price_history(item).empty
    }
    if not available:
        st.info("표시할 시계열 데이터가 없습니다.")
        return

    st.markdown("<div style='height:1.6rem'></div>", unsafe_allow_html=True)
    st.subheader("가격 추이")

    selected_name = st.radio(
        "품목", list(available.values()), horizontal=True, label_visibility="collapsed"
    )
    selected_item = next(k for k, v in available.items() if v == selected_name)

    history = db.get_price_history(selected_item, limit=36)
    unit = history.iloc[-1]["unit"]

    low, high = history["price"].min(), history["price"].max()
    pad = (high - low) * 0.18 or max(high * 0.05, 1.0)

    figure = px.area(history, x="date", y="price")
    figure.update_traces(
        line=dict(color=ACCENT, width=2.4),
        fillcolor="rgba(59, 91, 219, 0.07)",
        hovertemplate="%{x|%Y-%m}<br><b>%{y:,.2f}</b> " + unit + "<extra></extra>",
    )
    figure.update_layout(
        height=340,
        margin=dict(l=0, r=0, t=10, b=0),
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(size=12, color=MUTED),
        xaxis=dict(title=None, showgrid=False, showline=True, linecolor="#E6E8EB"),
        # 0 부터 그리면 변동폭이 눌려 보인다. 데이터 범위에 맞춰 여백만 준다.
        yaxis=dict(
            title=unit,
            gridcolor="#F0F2F5",
            zeroline=False,
            range=[low - pad, high + pad],
        ),
    )
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})

    first, last_row = history.iloc[0], history.iloc[-1]
    st.caption(
        f"{first['date']:%Y-%m} ~ {last_row['date']:%Y-%m} · {len(history)}개 시점 · "
        f"출처 [{last_row['source']}]({last_row['source_url']})"
    )


# ── News ────────────────────────────────────────────────────────────────

def render_news() -> None:
    news = db.get_news(limit=60)
    page_header("News", "철강·원자재 관련 최신 기사")

    if news.empty:
        st.info("수집된 뉴스가 없습니다.")
        return

    sources = sorted(news["source"].dropna().unique())
    chosen = st.multiselect(
        "출처 필터", sources, default=[], placeholder="전체 출처", label_visibility="collapsed"
    )
    if chosen:
        news = news[news["source"].isin(chosen)]

    st.caption(f"{len(news)}건 · 제목·날짜·출처·원문 링크만 표시합니다 (본문 미수록)")

    # 제목·출처는 외부 RSS 에서 온 텍스트다. HTML 로 넣기 전에 반드시 이스케이프한다.
    def esc(value) -> str:
        return html.escape(str(value if value is not None else ""), quote=True)

    rows = "".join(
        f'<div class="news-row">'
        f'<div class="news-title"><a href="{esc(row["url"])}" target="_blank" '
        f'rel="noopener noreferrer">{esc(row["title"])}</a></div>'
        f'<div class="news-meta">{esc(row["published_at"] or "날짜 미상")} · '
        f'{esc(row["source"])}</div>'
        f"</div>"
        for _, row in news.iterrows()
    )
    st.markdown(rows, unsafe_allow_html=True)


# ── AI Summary ──────────────────────────────────────────────────────────

def render_summary() -> None:
    page_header("AI Summary", "수집된 가격·뉴스만 근거로 생성한 시황 요약")

    if ai.detect_provider() is None:
        st.warning(
            "LLM API 키가 설정되지 않아 요약을 생성할 수 없습니다. "
            "Dashboard 와 News 는 정상 동작합니다.\n\n"
            "- 로컬 실행: `.env.example` 을 `.env` 로 복사하고 `OPENAI_API_KEY` 입력\n"
            "- Streamlit Cloud: 앱 **Settings → Secrets** 에 "
            '`OPENAI_API_KEY = "sk-..."` 추가'
        )
        return

    force = st.button("요약 다시 생성", help="캐시를 무시하고 모델을 다시 호출합니다")
    with st.spinner("요약 생성 중..."):
        result = ai.summarize(force=force)

    if result["status"] == "no_data":
        st.info("요약할 데이터가 없습니다.")
        return
    if result["status"] == "error":
        st.error(f"요약 생성에 실패했습니다: {result['reason']}")
        return

    # 모델 출력도 그대로 HTML 에 넣지 않는다.
    st.markdown(
        f'<div class="summary-box">{html.escape(result["text"])}</div>',
        unsafe_allow_html=True,
    )
    badge = "캐시됨" if result.get("cached") else "새로 생성"
    st.caption(
        f"{result['model']} · {result['generated_at']} · {badge} · "
        "아래 근거 데이터만 사용해 생성되었습니다."
    )
    with st.expander("요약의 근거가 된 데이터 보기"):
        st.json(result["context"])


# ── 앱 ──────────────────────────────────────────────────────────────────

PAGES = {
    "Dashboard": render_dashboard,
    "News": render_news,
    "AI Summary": render_summary,
}


def main() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    db.init_db()
    ensure_data()

    with st.sidebar:
        st.markdown("### 🔩 철강 원자재")
        st.caption("철광석·석탄 시황")
        st.divider()
        page = st.radio("화면", list(PAGES), label_visibility="collapsed")
        st.divider()

        if st.button("지금 새로고침", width="stretch"):
            with st.spinner("수집 중..."):
                outcome = collect_all()
            st.cache_resource.clear()
            st.success(f"가격 {outcome['prices']}건 · 뉴스 {outcome['news']}건")
            for error in outcome["errors"]:
                st.error(error)

        last = db.get_last_collected_at()
        st.caption(
            f"마지막 갱신 {last}\n\n{REFRESH_AFTER_HOURS}시간마다 자동 갱신됩니다."
            if last
            else "아직 수집 이력이 없습니다."
        )

    PAGES[page]()

    st.divider()
    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()
