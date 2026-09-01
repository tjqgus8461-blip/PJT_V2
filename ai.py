"""LLM 시황 요약.

키가 있는 쪽을 자동으로 고른다 (OPENAI 우선).
키가 하나도 없으면 예외를 던지지 않고 provider=None 을 돌려주며,
앱은 AI Summary 화면에만 안내 문구를 띄우고 나머지는 정상 동작한다.

요약은 '수집이 끝난 데이터'만 프롬프트에 넣는다.
모델이 없는 수치를 지어내지 않도록 시스템 프롬프트로 못을 박고,
같은 입력이면 DB 캐시를 재사용해 불필요한 호출을 막는다.
"""

from __future__ import annotations

import hashlib
import json
import os

from dotenv import load_dotenv

import database as db

load_dotenv()

DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

SYSTEM_PROMPT = (
    "너는 철강 원자재 시황을 정리하는 애널리스트다.\n"
    "규칙:\n"
    "1. 아래 제공된 가격 데이터와 뉴스 제목에만 근거해 작성한다.\n"
    "2. 제공되지 않은 수치, 날짜, 사건은 절대 언급하지 않는다. 추측하지 않는다.\n"
    "3. 가격 예측이나 매매 조언을 하지 않는다. 현재 상황만 기술한다.\n"
    "4. 데이터가 없는 품목은 '데이터 없음'이라고만 쓰고 넘어간다.\n"
    "5. 한국어로 3~5문장, 불릿 없이 줄글로 쓴다.\n"
    "6. 수치를 인용할 때는 단위와 기준일을 함께 쓴다."
)


def get_api_key(name: str) -> str | None:
    """API 키를 찾는다. 로컬은 .env, Streamlit Cloud 는 st.secrets 를 쓴다.

    찾은 값은 os.environ 에 넣어준다 - OpenAI()/Anthropic() 생성자가
    환경변수에서 키를 읽기 때문이다.
    """
    value = os.getenv(name)
    if value:
        return value
    try:
        import streamlit as st

        value = st.secrets.get(name)
    except Exception:  # noqa: BLE001 - streamlit 밖에서 실행될 수도 있다
        return None
    if value:
        os.environ[name] = str(value)
        return str(value)
    return None


def detect_provider() -> str | None:
    """사용 가능한 LLM 공급자를 고른다. 키와 패키지가 모두 있어야 한다."""
    import importlib.util

    if get_api_key("OPENAI_API_KEY") and importlib.util.find_spec("openai"):
        return "openai"
    if get_api_key("ANTHROPIC_API_KEY") and importlib.util.find_spec("anthropic"):
        return "anthropic"
    return None


def build_context() -> dict:
    """요약의 근거가 되는 데이터를 모은다. 화면에서 그대로 펼쳐 보여준다."""
    prices = []
    for item, name in db.ITEMS.items():
        latest = db.get_latest_price(item)
        if not latest:
            prices.append({"상품명": name, "상태": "데이터 없음"})
            continue
        previous = db.get_previous_price(item, latest["date"])
        entry = {
            "상품명": name,
            "가격": latest["price"],
            "단위": latest["unit"],
            "기준일": latest["date"],
            "규격": latest["spec"],
            "출처": latest["source"],
        }
        if previous:
            entry["직전"] = {
                "가격": previous["price"],
                "기준일": previous["date"],
                "변화": round(latest["price"] - previous["price"], 2),
            }
        prices.append(entry)

    news_df = db.get_news(limit=15)
    news = [
        {"제목": row["title"], "날짜": row["published_at"], "출처": row["source"]}
        for _, row in news_df.iterrows()
    ]
    return {"가격": prices, "뉴스": news}


def context_hash(context: dict) -> str:
    payload = json.dumps(context, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _call_openai(prompt: str, model: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=600,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def _call_anthropic(prompt: str, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


def summarize(force: bool = False) -> dict:
    """시황 요약을 돌려준다.

    반환: {status, text?, model?, generated_at?, cached?, context, reason?}
      status = 'ok' | 'no_provider' | 'no_data' | 'error'
    """
    context = build_context()

    if not any("가격" in entry for entry in context["가격"]) and not context["뉴스"]:
        return {"status": "no_data", "context": context}

    provider = detect_provider()
    if provider is None:
        return {"status": "no_provider", "context": context}

    digest = context_hash(context)
    if not force:
        cached = db.get_summary(digest)
        if cached:
            return {
                "status": "ok",
                "text": cached["text"],
                "model": cached["model"],
                "generated_at": cached["generated_at"],
                "cached": True,
                "context": context,
            }

    prompt = (
        "다음은 오늘 수집된 철강 원자재 데이터다. 이 데이터만 사용해 시황을 요약하라.\n\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
    )

    try:
        if provider == "openai":
            model = DEFAULT_OPENAI_MODEL
            text = _call_openai(prompt, model)
        else:
            model = DEFAULT_ANTHROPIC_MODEL
            text = _call_anthropic(prompt, model)
    except Exception as exc:  # noqa: BLE001 - 요약 실패가 앱 전체를 막지 않게 한다
        return {
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
            "context": context,
        }

    if not text:
        return {"status": "error", "reason": "모델이 빈 응답을 반환했습니다.", "context": context}

    from data_collector import now_iso

    generated_at = now_iso()
    db.save_summary(digest, text, model, generated_at)
    return {
        "status": "ok",
        "text": text,
        "model": model,
        "generated_at": generated_at,
        "cached": False,
        "context": context,
    }


if __name__ == "__main__":
    print("provider:", detect_provider())
    outcome = summarize()
    print("status:", outcome["status"])
    print(outcome.get("text") or outcome.get("reason") or "")
