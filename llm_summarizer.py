"""
llm_summarizer.py - (A) 자체 요약 생성 + (A-1) 얇은 재료 fallback.
프로바이더/모델명/API URL/X-Title 상수는 issue_grouper.py에서 재사용.
API 키 없음/LLM 호출 실패 시 요약 생략하고 원문 제목만 노출.
"""

import json
import os
import time

import requests
import trafilatura
from trafilatura.settings import use_config as _trafilatura_use_config

import llm_rate_limiter

import issue_grouper as _ig


_SYSTEM_PROMPT = (
    "너는 AI·IT 뉴스 큐레이션 서비스의 요약 작성자다. 주어진 이슈(같은 사건을 다루는 기사 제목들과 참고 정보)를 보고 다음 두 가지를 작성하라. "
    "(1) title: 이슈를 대표하는 한국어 제목. 원문 제목이 이미 한국어면 자연스럽게 다듬어 그대로 쓰고, 영어 등 외국어면 한국어로 번역한다 - 원문의 사실관계를 유지하고 과도하게 의역하지 않는다. "
    "(2) summary: 한국어 2~3문장 자체 요약. 원문을 그대로 옮기지 말고 핵심 내용만 새로 요약한다. 확실하지 않은 수치나 사실은 임의로 만들어내지 말고, 주어진 제목/참고 정보에 있는 내용만 사용한다. 확실하지 않으면 요약 대신 애매함을 그대로 표현하라. "
    "title/summary 둘 다 전문용어·정부기관명·제도명 등 고유명사는 임의로 한글화하지 말고 영문 원어가 있으면 괄호로 병기한다. "
    "다른 설명이나 마크다운 코드펜스 없이, 반드시 다음 JSON 형식으로만 응답하라: {\"title\": \"...\", \"summary\": \"...\"}"
)

_MAX_TITLES_IN_PROMPT = 10
_MAX_CONTEXT_ARTICLES = 5
_MAX_BODY_EXCERPT_CHARS = 300

# 단독 기사 본문 추가 수집(trafilatura, 범용 추출 - 사이트별 셀렉터 없음, 실패해도 기존 fallback으로 흡수)
_BODY_FETCH_TIMEOUT_SECONDS = 10
_BODY_FETCH_MIN_LENGTH = 200

_TRAFILATURA_CONFIG = _trafilatura_use_config()
_TRAFILATURA_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(_BODY_FETCH_TIMEOUT_SECONDS))


def _build_user_prompt(item: dict) -> str:
    """이슈 하나(scorer.score_group() 결과)를 프롬프트로 변환."""
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
    """반환값: (응답 텍스트, 원본 응답 dict)."""
    llm_rate_limiter.wait_for_openrouter_slot()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "X-Title": _ig._OPENROUTER_X_TITLE,
    }
    body = {
        "model": model_name,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = session.post(_ig.LLM_API_URL_OPENROUTER, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip(), data


def _call_llm(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session) -> str | None:
    """issue_grouper의 OpenRouter 모델 체인으로 호출, 응답 텍스트 반환. 실패 시 None."""
    data = None
    try:
        chain = _ig._LLM_MODEL_CHAIN_OPENROUTER_ROLES
        role_codes = {"1순위": "LS-01", "2순위": "LS-02", "3순위": "LS-03", "최종 안전망": "LS-04"}
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
    except Exception as e:
        snippet = (" ".join(str(data).split())[:200] + "...") if data is not None else "(응답을 아예 못 받음 - 요청/인증 단계에서 실패)"
        print(f"[llm_summarizer] 🔴 조치필요 [LS-05] - LLM 호출 실패: {type(e).__name__} - {e!r} "
              f"| 실제 응답: {snippet}")
        return None


def _is_suspicious_summary(text: str) -> bool:
    """OpenRouter 무료 라우터가 요약 대신 "User Safety: safe" 같은 안전성 판정 텍스트를 반환하는 경우 감지."""
    return "user safety" in text.lower()


def _parse_llm_response(text: str) -> dict | None:
    """{"title": ..., "summary": ...} JSON 파싱. 실패하면 None(원문 텍스트를 그대로 요약으로 fallback)."""
    cleaned = text
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
    try:
        parsed = json.loads(cleaned.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    title = parsed.get("title")
    return {
        "title": title.strip() if isinstance(title, str) and title.strip() else None,
        "summary": summary.strip(),
    }


def _fetch_body_via_trafilatura(url: str) -> str | None:
    """URL에서 범용 본문 추출. 실패하면 조용히 None."""
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
    이슈 하나에 요약을 붙여 반환(원본은 안 건드림, 얕은 복사본 반환).
    추가 필드: title_ko(한국어 제목, 실패 시 None), summary, summary_skipped_reason.
    """
    result = dict(item)

    if not llm_rate_limiter.LLM_ENABLED:
        result["summary"] = None
        result["title_ko"] = None
        result["summary_skipped_reason"] = "LLM_ENABLED=off - 요약 생략, 원문 제목만 노출"
        return result

    titles = item.get("titles", [])
    item_for_prompt = item

    if len(titles) == 1:
        article = item.get("articles", [{}])[0] if item.get("articles") else {}
        body = article.get("body") or ""
        description = article.get("description") or ""
        has_substantial_material = len(body) >= 200 or len(description) >= 50

        if not has_substantial_material:
            url = article.get("url") or (item.get("urls", [None])[0] if item.get("urls") else None)
            if url:
                print(f"[llm_summarizer] 🟡 주의 [LS-06] - 단독 기사 재료 부족 - 본문 추가 수집 시도: {url}")
                fetched_body = _fetch_body_via_trafilatura(url)
                if fetched_body and len(fetched_body) >= _BODY_FETCH_MIN_LENGTH:
                    print(f"[llm_summarizer] 본문 추가 수집 성공({len(fetched_body)}자) - 요약 진행: {url}")
                    has_substantial_material = True
                    enriched_article = dict(article)
                    enriched_article["body"] = fetched_body
                    other_articles = item.get("articles", [])[1:]
                    item_for_prompt = dict(item)
                    item_for_prompt["articles"] = [enriched_article] + other_articles

        if not has_substantial_material:
            result["summary"] = None
            result["title_ko"] = None
            result["summary_skipped_reason"] = (
                "단독 기사(이슈 그룹핑 안 됨) - 본문/설명 재료가 얇아 요약 생략, "
                "원문 제목만 노출 (범용 본문 추가 수집도 실패/미시도 - 제목 번역도 이 경로에선 "
                "같이 생략됨)"
            )
            return result

    key_env_var = "OPENROUTER_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        result["summary"] = None
        result["title_ko"] = None
        result["summary_skipped_reason"] = (
            f"{key_env_var} 없음 - 요약 생략, 원문 제목만 노출"
        )
        return result

    user_prompt = _build_user_prompt(item_for_prompt)
    if session is not None:
        raw_response = _call_llm(_SYSTEM_PROMPT, user_prompt, api_key, session)
    else:
        with requests.Session() as temp_session:
            raw_response = _call_llm(_SYSTEM_PROMPT, user_prompt, api_key, temp_session)

    if not raw_response:
        result["summary"] = None
        result["title_ko"] = None
        result["summary_skipped_reason"] = "LLM 호출/응답 실패 - 요약 생략, 원문 제목만 노출"
        return result

    parsed = _parse_llm_response(raw_response)
    if parsed is None:
        summary_text = raw_response
        title_ko = None
    else:
        summary_text = parsed["summary"]
        title_ko = parsed["title"]

    if _is_suspicious_summary(summary_text):
        result["summary"] = None
        result["title_ko"] = None
        result["summary_skipped_reason"] = (
            "LLM 응답이 요약이 아닌 것으로 추정됨(안전성 필터 오작동 등, 원인 미확인) - "
            "본문 요약 생성 실패, 원문 제목만 노출"
        )
        return result

    result["summary"] = summary_text
    result["title_ko"] = title_ko
    result["summary_skipped_reason"] = None
    return result


def summarize_top_issues(ranked_items: list[dict], label: str = "",
                          deadline: float | None = None) -> list[dict]:
    """scorer.score_and_rank() 결과 전체에 summarize_issue 적용(국내/해외/카테고리별 각각 호출됨)."""
    results = []
    total = len(ranked_items)
    with requests.Session() as session:
        for i, item in enumerate(ranked_items, start=1):
            titles = item.get("titles", [])
            rep_title = titles[0] if titles else "(제목 없음)"
            prefix = f"[llm_summarizer] {label} " if label else "[llm_summarizer] "

            if deadline is not None and time.monotonic() >= deadline:
                print(f"{prefix}({i}/{total}) 🟡 주의 - 요약 시간 예산 소진 - "
                      f"남은 {total - i + 1}건 전부 요약 생략, 원문 제목만 노출")
                for remaining_item in ranked_items[i - 1:]:
                    remaining_result = dict(remaining_item)
                    remaining_result["summary"] = None
                    remaining_result["summary_skipped_reason"] = "시간 예산 소진 - 요약 생략, 원문 제목만 노출"
                    results.append(remaining_result)
                break

            print(f"{prefix}({i}/{total}) '{rep_title}' (그룹 {len(titles)}건) - 처리 중...")

            result = summarize_issue(item, session)

            if result.get("summary"):
                print(f"{prefix}({i}/{total}) 요약 완료")
            else:
                print(f"{prefix}({i}/{total}) 요약 생략 - {result.get('summary_skipped_reason', '사유 불명')}")

            results.append(result)
    return results


def print_summaries(label: str, summarized: list[dict]) -> None:
    """결과를 콘솔에 출력."""
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