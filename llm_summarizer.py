"""
llm_summarizer.py
(A) 자체 요약 생성 + (A-1) 얇은 재료 fallback 담당. ((B) 그룹핑 보조는 issue_grouper.stage3_llm_assist)

** 프로바이더 설정을 issue_grouper.py에서 재사용 **
LLM_PROVIDER/모델명/API URL/X-Title 상수를 새로 정의하지 않고 issue_grouper.py에서 import.
이 값들에서 실제로 버그를 겪었던 적이 있어서(① GitHub Actions 미등록 Variable이 빈 문자열로
넘어와 os.environ.get(key, default)가 기본값을 못 씀, ② X-Title에 한글 넣었다가 HTTP 헤더는
latin-1만 허용돼 UnicodeEncodeError) 이미 검증된 값을 그대로 재사용해 같은 버그 재현을 막는다.

** (A)/(A-1) 안전장치 **
API 키 없음/LLM 호출·응답 실패 시 요약을 생략하고 원문 제목만 노출하는 쪽으로 fallback한다.
"""

import os

import requests
import trafilatura
from trafilatura.settings import use_config as _trafilatura_use_config

import issue_grouper as _ig  # LLM_PROVIDER, 모델명, API URL, X-Title 상수 재사용


_SYSTEM_PROMPT = (
    "너는 AI·IT 뉴스 큐레이션 서비스의 요약 작성자다. 주어진 이슈(같은 사건을 다루는 기사 제목들과 참고 정보)를 보고 한국어로 2~3문장의 자체 요약을 작성하라. 원문을 그대로 옮기지 말고 핵심 내용만 새로 요약한다. 확실하지 않은 수치나 사실은 임의로 만들어내지 말고, 주어진 제목/참고 정보에 있는 내용만 사용한다. 확실하지 않으면 요약 대신 애매함을 그대로 표현하라. 전문용어·정부기관명·제도명 등 고유명사는 임의로 한글화하지 말고 영문 원어가 있으면 괄호로 병기한다. 다른 설명 없이 요약 문장만 출력한다."
)

# 그룹 하나에 기사가 아주 많을 때 제목을 전부 프롬프트에 넣으면 비용/속도 낭비가 커서 상한을 둔다.
_MAX_TITLES_IN_PROMPT = 10
# 참고 컨텍스트(본문 발췌/description)도 몇 건까지만 볼지 상한
_MAX_CONTEXT_ARTICLES = 5
_MAX_BODY_EXCERPT_CHARS = 300

# --- 단독 기사 본문 추가 수집 ---
# 네이버/GDELT는 본문을 못 가져와서 "재료 부족"으로 요약이 생략되는 경우가 많았다. 대신
# trafilatura(범용 본문 추출)로 시도해본다 - 사이트별 맞춤 셀렉터가 없어 성공률은 사이트마다
# 다르고(광고를 본문으로 착각, JS 렌더링 사이트는 못 뽑음, WATT처럼 403 걸리는 사이트도 있음),
# 실패해도 기존처럼 "재료 부족 -> 요약 생략" fallback이라 지금보다 나빠지지 않는다.
# 비용 통제를 위해 "재료 부족 판정이 실제로 난 시점", 그것도 이미 Top N으로 추려진 기사에만 시도.
_BODY_FETCH_TIMEOUT_SECONDS = 10
_BODY_FETCH_MIN_LENGTH = 200  # has_substantial_material의 본문 기준과 동일

# trafilatura 기본 타임아웃(30초)은 Top N 몇 건이라도 사이트가 매달리면 실행이 늘어지므로 짧게 조정
_TRAFILATURA_CONFIG = _trafilatura_use_config()
_TRAFILATURA_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(_BODY_FETCH_TIMEOUT_SECONDS))


def _build_user_prompt(item: dict) -> str:
    """
    이슈 하나(scorer.score_group() 결과 dict)를 LLM 프롬프트로 만든다.
    제목 + (있으면) 본문 핵심 문장 + (네이버 소스면) description을 참고 컨텍스트로 추가
    (그대로 인용하지 않고 참고용으로만).
    """
    titles = item.get("titles", [])
    lines = ["다음은 같은 이슈를 다룬 기사 제목들이다:"]
    for title in titles[:_MAX_TITLES_IN_PROMPT]:
        lines.append(f"- {title}")
    if len(titles) > _MAX_TITLES_IN_PROMPT:
        lines.append(f"(외 {len(titles) - _MAX_TITLES_IN_PROMPT}건 제목 생략)")

    context_lines = []
    for article in item.get("articles", [])[:_MAX_CONTEXT_ARTICLES]:
        source = article.get("source", "?")
        body = article.get("body")
        if body:
            context_lines.append(f"[{source}] 본문 일부: {body[:_MAX_BODY_EXCERPT_CHARS]}")
        description = article.get("description")
        if description:
            context_lines.append(f"[{source}] 설명: {description}")

    if context_lines:
        lines.append("\n참고 정보(그대로 인용하지 말고 참고만 할 것):")
        lines.extend(context_lines)

    lines.append("\n위 내용을 바탕으로 한국어 2~3문장 자체 요약을 작성하라.")
    return "\n".join(lines)


def _request_openrouter(system_prompt: str, user_prompt: str, api_key: str,
                         session: requests.Session, model_name: str) -> tuple[str, dict]:
    """반환값: (응답 텍스트, 원본 응답 dict) - dict는 실패 시 로그용."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        # HTTP 헤더는 latin-1만 허용돼 한글이 섞이면 UnicodeEncodeError - issue_grouper의 검증된 상수 재사용
        "X-Title": _ig._OPENROUTER_X_TITLE,
    }
    body = {
        "model": model_name,
        "temperature": 0.3,  # 자연어 생성이라 3차(판정, temperature=0)보다 살짝 여유
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = session.post(_ig.LLM_API_URL_OPENROUTER, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip(), data


def _request_anthropic(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session) -> tuple[str, dict]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": _ig.LLM_MODEL_ANTHROPIC,
        "max_tokens": 300,
        "temperature": 0.3,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    resp = session.post(_ig.LLM_API_URL_ANTHROPIC, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()
    return text, data


def _call_llm(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session) -> str | None:
    """
    issue_grouper.py의 프로바이더 설정으로 LLM을 호출하고 응답 텍스트 반환. 실패 시 None
    (호출부가 "요약 생략, 원문 제목만"으로 fallback).

    issue_grouper의 3차 호출과 요청 형식은 비슷하지만 파싱 방식(자연어 vs JSON 배열)이 달라
    별도 함수로 둠. temperature/max_tokens도 요약용으로 다르게 줘서 헬퍼를 그대로 재사용하지 않음.

    session: summarize_top_issues가 이슈마다 반복 호출하므로 재사용해 커넥션 오버헤드 절감.

    ** 지정 모델 실패 시 다음 후보 모델로 자동 재시도 **
    openrouter면 _ig._LLM_MODEL_CHAIN_OPENROUTER_ROLES(1순위 -> 2순위 -> 3순위 -> 최종 안전망
    openrouter/free)를 순서대로 시도 - 특정 모델 하나의 문제(오타, 무료 티어 이탈 등)가
    이번 실행의 요약 기능 전체를 막지 않게 함. 최종 안전망까지 실패해야 최종 실패.
    """
    data = None  # 응답 자체를 못 받았을 수도 있어 미리 초기화
    try:
        if _ig.LLM_PROVIDER == "openrouter":
            chain = _ig._LLM_MODEL_CHAIN_OPENROUTER_ROLES
            role_codes = {"1순위": "LS-02", "2순위": "LS-03", "3순위": "LS-04", "최종 안전망": "LS-05"}
            last_error: Exception | None = None
            for idx, (role, model_name) in enumerate(chain):
                try:
                    if idx > 0:
                        print(f"[llm_summarizer] 🟡 주의 - 요약 생성 {role} 모델('{model_name}')로 재시도 "
                              f"({idx + 1}/{len(chain)})")
                    text, data = _request_openrouter(system_prompt, user_prompt, api_key, session, model_name)
                    return text
                except Exception as e:
                    last_error = e
                    code = role_codes[role]
                    is_final = idx == len(chain) - 1
                    level = "🔴 조치필요" if is_final else "🟡 주의"
                    next_note = "더 시도할 모델 없음" if is_final else "다음 후보 모델로 재시도"
                    print(f"[llm_summarizer] {level} [{code}] - 요약 생성 {role} 모델('{model_name}') "
                          f"호출 실패 - {next_note}: {type(e).__name__} - {e!r}")
            raise last_error
        else:
            text, data = _request_anthropic(system_prompt, user_prompt, api_key, session)
            return text
    except Exception as e:
        # data가 있으면(HTTP는 성공했는데 예상 필드를 못 찾은 경우) 실제 응답을 잘라서 같이 남김
        snippet = (" ".join(str(data).split())[:200] + "...") if data is not None else "(응답을 아예 못 받음 - 요청/인증 단계에서 실패)"
        print(f"[llm_summarizer] 🔴 조치필요 [LS-01] - LLM({_ig.LLM_PROVIDER}) 호출 실패: {type(e).__name__} - {e!r} "
              f"| 실제 응답: {snippet}")
        return None


def _is_suspicious_summary(text: str) -> bool:
    """
    OpenRouter 무료 라우터(openrouter/free)가 정상 요약 대신 콘텐츠 안전성 판정 텍스트
    ("User Safety: safe")를 그대로 반환하는 경우가 있다 (원인 미확인 - 무료 라우터가 요청마다
    다른 실제 모델로 라우팅되는 것으로 추정). 실제로 관측된 좁은 패턴만 걸러낸다 - 넓히면
    정상 요약도 걸러질 위험이 있어서, 낮은 품질/짧은 요약까지 거르는 건 범위 밖.
    """
    return "user safety" in text.lower()


def _fetch_body_via_trafilatura(url: str) -> str | None:
    """
    URL에서 범용으로 본문 추출 시도. 사이트별 셀렉터가 없어 성공률은 들쭉날쭉.
    실패(다운로드/추출/타임아웃/예외)하면 조용히 None 반환 - 호출부가 "재료 부족" 경로로 흡수.
    """
    try:
        downloaded = trafilatura.fetch_url(url, no_ssl=True, config=_TRAFILATURA_CONFIG)
    except Exception:
        return None
    if not downloaded:
        return None
    try:
        extracted = trafilatura.extract(downloaded, favor_precision=True, include_comments=False,
                                         include_tables=False)
    except Exception:
        return None
    return extracted or None


def summarize_issue(item: dict, session: requests.Session | None = None) -> dict:
    """
    이슈 하나에 (A)/(A-1) 로직을 적용해 요약을 붙인다. 원본 item은 변경하지 않고 얕은 복사본을
    반환한다 (호출부가 리스트를 여러 번 다룰 수 있어 부작용 없는 편이 안전).

    session: 안 넘기면 호출 하나짜리 임시 세션 사용 (summarize_top_issues처럼 여러 건을 처리할
    때만 세션을 만들어 넘기면 재사용 이득이 있음).

    반환값에 추가되는 필드:
      summary: LLM이 생성한 2~3문장 요약, 또는 None(생략된 경우)
      summary_skipped_reason: 생략 이유. 정상 요약됐으면 None.
    """
    result = dict(item)
    titles = item.get("titles", [])
    item_for_prompt = item  # 기본은 원본 그대로 - 본문 추가 수집 성공 시에만 아래서 교체

    if len(titles) == 1:
        article = item.get("articles", [{}])[0] if item.get("articles") else {}
        body = article.get("body") or ""
        description = article.get("description") or ""
        # 잠정값 - 이 정도는 돼야 "제목을 그대로 풀어쓰는 것"을 넘어서는 실질적 요약이 가능하다고 봄
        has_substantial_material = len(body) >= 200 or len(description) >= 50

        if not has_substantial_material:
            url = article.get("url") or (item.get("urls", [None])[0] if item.get("urls") else None)
            if url:
                print(f"[llm_summarizer] 🟡 주의 [LS-06] - 단독 기사 재료 부족 - 본문 추가 수집 시도: {url}")
                fetched_body = _fetch_body_via_trafilatura(url)
                if fetched_body and len(fetched_body) >= _BODY_FETCH_MIN_LENGTH:
                    print(f"[llm_summarizer] 본문 추가 수집 성공({len(fetched_body)}자) - 요약 진행: {url}")
                    has_substantial_material = True
                    # item은 원본이라 직접 안 건드리고 프롬프트 생성용 얕은 복사본만 따로 만듦
                    enriched_article = dict(article)
                    enriched_article["body"] = fetched_body
                    other_articles = item.get("articles", [])[1:]
                    item_for_prompt = dict(item)
                    item_for_prompt["articles"] = [enriched_article] + other_articles

        if not has_substantial_material:
            result["summary"] = None
            result["summary_skipped_reason"] = (
                "단독 기사(이슈 그룹핑 안 됨) - 본문/설명 재료가 얇아 요약 생략, "
                "원문 제목만 노출 (범용 본문 추가 수집도 실패/미시도)"
            )
            return result
        # 재료가 충분하면 단독 기사여도 아래 정상 요약 경로로 진행

    key_env_var = "OPENROUTER_API_KEY" if _ig.LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        result["summary"] = None
        result["summary_skipped_reason"] = (
            f"{key_env_var} 없음(LLM_PROVIDER={_ig.LLM_PROVIDER}) - 요약 생략, 원문 제목만 노출"
        )
        return result

    user_prompt = _build_user_prompt(item_for_prompt)
    if session is not None:
        summary_text = _call_llm(_SYSTEM_PROMPT, user_prompt, api_key, session)
    else:
        with requests.Session() as temp_session:
            summary_text = _call_llm(_SYSTEM_PROMPT, user_prompt, api_key, temp_session)

    if not summary_text:
        result["summary"] = None
        result["summary_skipped_reason"] = "LLM 호출/응답 실패 - 요약 생략, 원문 제목만 노출"
        return result

    if _is_suspicious_summary(summary_text):
        result["summary"] = None
        result["summary_skipped_reason"] = (
            "LLM 응답이 요약이 아닌 것으로 추정됨(안전성 필터 오작동 등, 원인 미확인) - "
            "본문 요약 생성 실패, 원문 제목만 노출"
        )
        return result

    result["summary"] = summary_text
    result["summary_skipped_reason"] = None
    return result


def summarize_top_issues(ranked_items: list[dict], label: str = "") -> list[dict]:
    """
    scorer.score_and_rank() 결과 전체에 summarize_issue를 적용 (main.py의 4단계 호출부에서
    국내/해외 각각 부름).

    이슈 하나당 LLM 호출이 몇 초~몇십 초 걸릴 수 있어(무료 모델은 특히), 항목 하나 처리할
    때마다 바로 로그를 찍어 실행 상태를 실시간으로 볼 수 있게 한다.
    """
    results = []
    total = len(ranked_items)
    with requests.Session() as session:
        for i, item in enumerate(ranked_items, start=1):
            titles = item.get("titles", [])
            rep_title = titles[0] if titles else "(제목 없음)"
            prefix = f"[llm_summarizer] {label} " if label else "[llm_summarizer] "
            print(f"{prefix}({i}/{total}) '{rep_title}' (그룹 {len(titles)}건) - 처리 중...")

            result = summarize_issue(item, session)

            if result.get("summary"):
                print(f"{prefix}({i}/{total}) 요약 완료")
            else:
                print(f"{prefix}({i}/{total}) 요약 생략 - {result.get('summary_skipped_reason', '사유 불명')}")

            results.append(result)
    return results


def print_summaries(label: str, summarized: list[dict]) -> None:
    """결과를 콘솔에 출력 (요약 유무와 무관하게 원문 링크는 항상 같이 표시)."""
    print(f"\n=== {label} - LLM 요약 ===")
    for i, item in enumerate(summarized, start=1):
        titles = item.get("titles", [])
        rep_title = titles[0] if titles else "(제목 없음)"
        print(f"{i}. {rep_title}")
        if item.get("summary"):
            print(f"   요약: {item['summary']}")
        else:
            print(f"   (요약 생략 - {item.get('summary_skipped_reason', '사유 불명')})")
        urls = item.get("urls", [])
        shown_urls = ", ".join(urls[:3])
        more_note = f" 외 {len(urls) - 3}건" if len(urls) > 3 else ""
        print(f"   원문 링크: {shown_urls}{more_note}")