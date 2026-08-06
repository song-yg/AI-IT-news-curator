"""
relevance_filter.py - 관련성 필터.
키워드 매칭만으론 못 거르는 오매칭(동음이의어, 각주성 언급 등)을 LLM으로 판단.
issue_grouper.py와 동일한 프로바이더/재시도 패턴 재사용.
기본값 방향은 issue_grouper와 반대 - 애매하면 true(통과)가 안전.
"""

import json
import os
import time

import requests

import llm_rate_limiter

LLM_MODEL_OPENROUTER = os.environ.get("OPENROUTER_MODEL") or "openrouter/free"
LLM_API_URL_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

LLM_MODEL_OPENROUTER_2 = os.environ.get("OPENROUTER_MODEL_2") or ""
LLM_MODEL_OPENROUTER_3 = os.environ.get("OPENROUTER_MODEL_3") or ""

_LLM_MODEL_CHAIN_OPENROUTER_ROLES: list[tuple[str, str]] = []
if LLM_MODEL_OPENROUTER:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("1순위", LLM_MODEL_OPENROUTER))
if LLM_MODEL_OPENROUTER_2:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("2순위", LLM_MODEL_OPENROUTER_2))
if LLM_MODEL_OPENROUTER_3:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("3순위", LLM_MODEL_OPENROUTER_3))
if "openrouter/free" not in [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("최종 안전망", "openrouter/free"))

_LLM_MODEL_ROLE_ERROR_CODE = {
    "1순위": "RF-01",
    "2순위": "RF-02",
    "3순위": "RF-03",
    "최종 안전망": "RF-04",
}

LLM_MODEL_CHAIN_OPENROUTER = [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]

_OPENROUTER_X_TITLE = "AI-IT-news-relevance-filter"  # ASCII 필수

BATCH_SIZE = 20
SNIPPET_MAX_CHARS = 150

TIME_BUDGET_SECONDS = 120 * 60  # standalone 기본값 - 정상 실행은 호출부의 deadline이 우선
CATEGORY_RECLASSIFY_TIME_BUDGET_SECONDS = 45 * 60  # standalone 기본값


_SYSTEM_PROMPT = (
    "You are a relevance classifier for an AI/IT industry news curation "
    "system. For each article, decide whether it substantively covers the "
    "\"AI industry\" or \"IT industry\" (artificial intelligence, specific "
    "AI/tech companies, robotics, or other IT technologies) as its actual "
    "topic.\n\n"
    "RELEVANT (true):\n"
    "- Articles whose core topic is artificial intelligence: models/"
    "services (LLMs, generative AI, chatbots, machine learning, deep "
    "learning, AI chips/hardware), AI-related corporate moves (funding, "
    "product launches, partnerships, regulation, earnings) for companies "
    "such as Nvidia, or AI policy/legislation\n"
    "- Articles whose core topic is robotics (autonomous driving, robots, "
    "drones) or other IT technologies covered by this project (metaverse, "
    "VR/AR, blockchain, NFT, cloud computing, cybersecurity, quantum "
    "computing)\n\n"
    "NOT RELEVANT (false) - the following are mismatch patterns that have "
    "actually been confirmed to repeat, so filter them out with particular "
    "care:\n"
    "1. Homonyms: the search term spells the same but refers to something "
    "different (e.g. \"클라우드\"/\"cloud\" as a person's name or fictional "
    "character, \"메타버스\" as an unrelated brand/nickname, or \"AI\" used "
    "as an abbreviation unrelated to artificial intelligence)\n"
    "2. Appears only as part of a proper noun: the search term is merely "
    "part of an event name, sponsorship name, product line name, etc. "
    "(e.g. \"Nvidia Cup\" esports tournament results, where the article's "
    "actual subject is the match outcome, not Nvidia or AI)\n"
    "3. Footnote-level mention: the article's core topic is something else "
    "entirely (e.g. a general stock market wrap-up, an overall corporate "
    "earnings roundup), and AI/IT-related content appears only briefly as "
    "one line among many companies/items\n"
    "4. Generic/marketing buzzword with no real substance: the article "
    "merely slaps on \"AI\" or \"스마트\" as a marketing adjective for an "
    "otherwise unrelated product (e.g. a home appliance review that "
    "mentions \"AI 기능 탑재\" only in passing, with no real discussion of "
    "the AI technology itself)\n\n"
    "Examples that MUST be kept as relevant (true) - these types have "
    "actually been mistakenly filtered out before, so pay special "
    "attention:\n"
    "- Articles about AI/IT company stock moves, earnings, or product news "
    "are relevant even if the title doesn't contain words like "
    "\"인공지능\" or \"IT\". This is true even for titles with a light tone "
    "using slang or buzzwords. For example, an article discussing Nvidia's "
    "stock surge using a slang nickname for the stock is an article about "
    "an AI-industry company, so it is true - do not judge it false just "
    "because the tone is light or the word \"인공지능\" doesn't appear.\n"
    "- Before marking such an article false, first confirm it doesn't "
    "actually match any of NOT RELEVANT criteria 1-4 (homonym / part of "
    "proper noun / footnote mention / generic buzzword with no substance) "
    "- if it clearly doesn't match any of them, it is automatically "
    "relevant.\n\n"
    "The \"category\" value provided alongside each article is just a "
    "reference hint from automatic dictionary-keyword matching, not a "
    "confirmed answer - make your final judgment based on the actual "
    "title/summary content.\n\n"
    "If the judgment is ambiguous, answer true (conservative default - it "
    "is safer to let a few irrelevant articles through than to wrongly "
    "filter out a relevant one).\n\n"
    "Titles may be in different languages (Korean/English/other languages "
    "mixed) - judge by the same criteria regardless of language.\n\n"
    "Output only a JSON array with no other explanation. Each element must "
    "be in the form {\"id\": number, \"relevant\": true|false}, and id must "
    "exactly match the number of the input article."
)

def _snippet(article: dict) -> str | None:
    """판정 근거 스니펫. 네이버는 description, GDELT는 본문 없음(항상 None) -> None 반환."""
    text = article.get("description") or article.get("body")
    if not text:
        return None
    return text[:SNIPPET_MAX_CHARS]


def _build_user_prompt(batch: list[dict]) -> str:
    lines = ["Judge whether each of the following articles is relevant to AI/IT industry news.\n"]
    for idx, article in enumerate(batch, start=1):
        title = article.get("title", "")
        category = article.get("category", "기타")
        snippet = _snippet(article)
        snippet_part = f'"{snippet}"' if snippet else "(none - judge from title only)"
        lines.append(
            f'{idx}. Title: "{title}" / Category: {category} / Summary: {snippet_part}'
        )
    lines.append(
        f'\nThere are {len(batch)} articles total. Include the number above as "id" in each '
        f'element and answer with a JSON array only (e.g. [{{"id": 1, "relevant": true}}, '
        f'{{"id": 2, "relevant": false}}, ...]). Do not omit any id or change the order.'
    )
    return "\n".join(lines)


def _snippet_for_log(text: str, limit: int = 200) -> str:
    if not text:
        return "(빈 응답)"
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _request_openrouter(system_prompt: str, user_prompt: str, api_key: str,
                         session: requests.Session, model_name: str) -> str:
    llm_rate_limiter.wait_for_openrouter_slot()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "X-Title": _OPENROUTER_X_TITLE,
    }
    body = {
        "model": model_name,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = session.post(LLM_API_URL_OPENROUTER, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()

def _request_llm_text(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session,
                       label: str, validate=None):
    """모델 체인(1순위->2순위->3순위->최종 안전망)을 순서대로 시도, 실패하면 다음 모델로."""
    chain = _LLM_MODEL_CHAIN_OPENROUTER_ROLES
    last_error: Exception | None = None
    for idx, (role, model_name) in enumerate(chain):
        is_final = idx == len(chain) - 1
        try:
            if idx > 0:
                print(f"[relevance_filter] 🟡 주의 - {label} {role} 모델('{model_name}')로 재시도 "
                      f"({idx + 1}/{len(chain)})")
            text = _request_openrouter(system_prompt, user_prompt, api_key, session, model_name)
            return validate(text, is_final) if validate else text
        except Exception as e:
            last_error = e
            code = _LLM_MODEL_ROLE_ERROR_CODE[role]
            level = "🔴 조치필요" if is_final else "🟡 주의"
            next_note = "더 시도할 모델 없음 - 이 배치 전체 안전 처리" if is_final else "다음 후보 모델로 재시도"
            print(f"[relevance_filter] {level} [{code}] - {label} {role} 모델('{model_name}') "
                  f"호출/응답 검증 실패 - {next_note}: {type(e).__name__} - {e!r}")
    raise last_error


def _call_llm(batch: list[dict], api_key: str, session: requests.Session) -> list[bool] | None:
    """batch 각각의 relevant 판정. 실패 시 None(호출부가 배치 전체 통과 처리)."""
    user_prompt = _build_user_prompt(batch)

    def _validate(text: str, is_final: bool) -> list[bool]:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())

        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")

        by_id: dict[int, bool] = {}
        for item in parsed:
            try:
                by_id[int(item["id"])] = bool(item["relevant"])
            except (KeyError, TypeError, ValueError):
                continue

        missing = [idx for idx in range(1, len(batch) + 1) if idx not in by_id]
        if missing and not is_final:
            raise ValueError(f"id {missing} 누락(기대 {len(batch)}건 중 {len(missing)}건) - 다음 모델로 재시도")

        results = [by_id.get(idx, True) for idx in range(1, len(batch) + 1)]
        if missing:
            print(f"[relevance_filter] 🟡 주의 [RF-05] - 관련성 필터 최종 안전망까지 갔지만 id {missing} "
                  f"여전히 누락(기대 {len(batch)}건 중 {len(missing)}건) - 그 항목들만 통과 처리, "
                  f"나머지 {len(batch) - len(missing)}건은 정상 판정 사용")
        return results

    try:
        return _request_llm_text(_SYSTEM_PROMPT, user_prompt, api_key, session, "관련성 필터", validate=_validate)
    except Exception as e:
        print(f"[relevance_filter] 🔴 조치필요 [RF-06] - LLM 호출/파싱 실패 - 이 배치"
              f"({len(batch)}건) 전부 통과 처리: {type(e).__name__} - {e!r}")
        return None


def filter_articles(articles: list[dict], deadline: float | None = None) -> list[dict]:
    """
    기사 단위 관련성 필터. 지금은 파이프라인 본선에서 안 쓰임(legacy)
    그룹핑을 관련성 필터보다 먼저 실행하는 순서로 바뀌면서 filter_groups()(아래, 그룹 단위)를 씀.
    그룹핑 없이 기사 단위로만 테스트하고 싶을 때 쓸 수 있어 남겨둠.
    """
    if not articles:
        return articles

    other_source_articles = [a for a in articles if a.get("source") not in ("네이버", "GDELT")]
    llm_target_articles = [a for a in articles if a.get("source") in ("네이버", "GDELT")]

    if other_source_articles:
        print(f"[relevance_filter] 네이버/GDELT 외 소스 {len(other_source_articles)}건은 LLM 호출 없이 자동 통과")

    if not llm_target_articles:
        return other_source_articles

    if not llm_rate_limiter.LLM_ENABLED:
        print(f"[relevance_filter] LLM_ENABLED=off - 관련성 필터 스킵, {len(articles)}건 전부 통과")
        return articles

    key_env_var = "OPENROUTER_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[relevance_filter] 🔴 조치필요 [RF-07] - {key_env_var} 없음 - "
              f"관련성 필터 생략, {len(llm_target_articles)}건(네이버/GDELT) 전부 통과")
        return articles

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER)
    print(f"[relevance_filter] 관련성 필터 시작 - model={model_desc}, 대상 {len(llm_target_articles)}건(네이버/GDELT만, "
          f"그 외 소스 {len(other_source_articles)}건 제외)")

    kept = list(other_source_articles)
    dropped_samples = []
    total_batches = (len(llm_target_articles) + BATCH_SIZE - 1) // BATCH_SIZE
    effective_deadline = deadline if deadline is not None else time.monotonic() + TIME_BUDGET_SECONDS
    skipped_count = 0

    def _over_budget() -> bool:
        return time.monotonic() >= effective_deadline

    with requests.Session() as session:
        for batch_num, i in enumerate(range(0, len(llm_target_articles), BATCH_SIZE), start=1):
            if _over_budget():
                remaining = llm_target_articles[i:]
                skipped_count = len(remaining)
                kept.extend(remaining)
                print(f"[relevance_filter] 🟡 주의 - 관련성 필터 시간 예산 소진 - "
                      f"남은 {skipped_count}건은 건너뛰고 전부 통과 처리")
                break

            batch = llm_target_articles[i:i + BATCH_SIZE]
            print(f"[relevance_filter] 배치 {batch_num}/{total_batches} 처리 중 ({len(batch)}건)")
            results = _call_llm(batch, api_key, session)
            if results is None:
                kept.extend(batch)
                continue
            for article, relevant in zip(batch, results):
                if relevant:
                    kept.append(article)
                else:
                    dropped_samples.append(article.get("title", ""))

    dropped_count = len(articles) - len(kept)
    print(f"[relevance_filter] 관련성 필터 완료 - 전체 {len(articles)}건(그 외 소스 자동통과 "
          f"{len(other_source_articles)}건 포함) 중 {dropped_count}건 제외, {len(kept)}건 유지"
          + (f" (시간 예산 소진으로 {skipped_count}건 미검증 통과 포함)" if skipped_count else ""))
    if dropped_samples:
        sample_n = min(10, len(dropped_samples))
        print(f"[relevance_filter] 제외된 기사 샘플 (최대 {sample_n}건):")
        for title in dropped_samples[:sample_n]:
            print(f"   - {title}")

    return kept


# --- 카테고리 재분류: 사전 매칭엔 안 걸려 "기타"인데 관련성은 확인된 기사를 LLM으로 재분류 ---
CATEGORY_RECLASSIFY_SYSTEM_PROMPT_TEMPLATE = (
    "You are a categorization assistant for an AI/IT industry "
    "news curation system. Each article below was not matched by "
    "dictionary-based keyword tagging and is currently labeled \"기타\" "
    "(uncategorized), but it has already been confirmed relevant to the "
    "AI/IT industry by a separate relevance check. Assign the "
    "single best-fitting category for each article from the following "
    "exact list. Respond with the Korean label written exactly as shown "
    "below, character for character - do not translate, abbreviate, or "
    "modify it in any way:\n\n"
    "{category_list}\n\n"
    "If none of the categories are a good fit, respond with \"기타\" "
    "(i.e. leave it uncategorized) rather than forcing a poor match.\n\n"
    "Output only a JSON array with no other explanation. Each element must "
    "be in the form {{\"id\": number, \"category\": \"<one label from the "
    "list above, or 기타>\"}}, and id must exactly match the number of the "
    "input article."
)


def _build_category_user_prompt(batch: list[dict]) -> str:
    lines = ["Assign the best-fitting category to each of the following articles.\n"]
    for idx, article in enumerate(batch, start=1):
        title = article.get("title", "")
        snippet = _snippet(article)
        snippet_part = f'"{snippet}"' if snippet else "(none - judge from title only)"
        lines.append(f'{idx}. Title: "{title}" / Summary: {snippet_part}')
    lines.append(
        f'\nThere are {len(batch)} articles total. Include the number above as "id" in each '
        f'element and answer with a JSON array only. Do not omit any id or change the order.'
    )
    return "\n".join(lines)


def _call_category_llm(batch: list[dict], api_key: str, session: requests.Session,
                        category_choices: list[str], system_prompt: str) -> list[str] | None:
    """_call_llm과 동일 패턴. 응답은 category_choices 중 하나(또는 "기타")."""
    user_prompt = _build_category_user_prompt(batch)
    valid_choices = set(category_choices) | {"기타"}

    def _validate(text: str, is_final: bool) -> list[str]:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())

        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")

        by_id: dict[int, str] = {}
        for item in parsed:
            try:
                category = str(item["category"])
                idx = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if category not in valid_choices:
                continue
            by_id[idx] = category

        missing = [idx for idx in range(1, len(batch) + 1) if idx not in by_id]
        if missing and not is_final:
            raise ValueError(f"id {missing} 누락/형식 이상(기대 {len(batch)}건 중 {len(missing)}건) - 다음 모델로 재시도")

        results = [by_id.get(idx, "기타") for idx in range(1, len(batch) + 1)]
        if missing:
            print(f"[relevance_filter] 🟡 주의 [RF-08] - 카테고리 재분류 최종 안전망까지 갔지만 id {missing} "
                  f"여전히 누락(기대 {len(batch)}건 중 {len(missing)}건) - 그 항목들만 "
                  f"'기타' 유지, 나머지 {len(batch) - len(missing)}건은 정상 판정 사용")
        return results

    try:
        return _request_llm_text(system_prompt, user_prompt, api_key, session, "카테고리 재분류", validate=_validate)
    except Exception as e:
        print(f"[relevance_filter] 🔴 조치필요 [RF-09] - 카테고리 재분류 LLM 호출/파싱 실패 - "
              f"이 배치({len(batch)}건) 전부 '기타' 유지: {type(e).__name__} - {e!r}")
        return None


def recategorize_uncategorized(articles: list[dict], deadline: float | None = None) -> list[dict]:
    """filter_articles() 통과했지만 category="기타"인 기사를 LLM으로 재분류(legacy, 기사 단위)."""
    targets = [a for a in articles if a.get("category") == "기타"]
    if not targets:
        return articles

    if not llm_rate_limiter.LLM_ENABLED:
        print(f"[relevance_filter] LLM_ENABLED=off - 카테고리 재분류 스킵, {len(targets)}건 '기타' 유지")
        return articles

    key_env_var = "OPENROUTER_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[relevance_filter] 🔴 조치필요 [RF-10] - {key_env_var} 없음 - "
              f"카테고리 재분류 생략, {len(targets)}건 '기타' 그대로 유지")
        return articles

    import keyword_tagger
    category_choices = list(keyword_tagger.CATEGORY_KEYWORDS.keys())
    category_list_text = "\n".join(f"- {c}" for c in category_choices)
    system_prompt = CATEGORY_RECLASSIFY_SYSTEM_PROMPT_TEMPLATE.format(category_list=category_list_text)

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER)
    print(f"[relevance_filter] 카테고리 재분류 시작 - model={model_desc}, 대상 {len(targets)}건('기타'로 남았지만 관련성 확인된 기사)")

    reclassified_count = 0
    skipped_count = 0
    total_batches = (len(targets) + BATCH_SIZE - 1) // BATCH_SIZE
    effective_deadline = (deadline if deadline is not None
                           else time.monotonic() + CATEGORY_RECLASSIFY_TIME_BUDGET_SECONDS)

    def _over_budget() -> bool:
        return time.monotonic() >= effective_deadline

    with requests.Session() as session:
        for batch_num, i in enumerate(range(0, len(targets), BATCH_SIZE), start=1):
            if _over_budget():
                skipped_count = len(targets) - i
                print(f"[relevance_filter] 🟡 주의 - 카테고리 재분류 시간 예산 소진 - "
                      f"남은 {skipped_count}건은 건너뛰고 '기타' 유지")
                break

            batch = targets[i:i + BATCH_SIZE]
            print(f"[relevance_filter] 카테고리 재분류 배치 {batch_num}/{total_batches} "
                  f"처리 중 ({len(batch)}건)")
            results = _call_category_llm(batch, api_key, session, category_choices, system_prompt)
            if results is None:
                continue
            for article, new_category in zip(batch, results):
                if new_category != "기타":
                    article["category"] = new_category
                    reclassified_count += 1

    print(f"[relevance_filter] 카테고리 재분류 완료 - {len(targets)}건 중 "
          f"{reclassified_count}건 재분류됨, {len(targets) - reclassified_count}건 '기타' 유지"
          + (f" (그중 {skipped_count}건은 시간 예산 소진으로 미시도)" if skipped_count else ""))

    return articles


# --- 그룹 단위 관련성 필터 / 카테고리 재분류(파이프라인 본선, 대표 기사 1건씩만 LLM에 물음) ---


def filter_groups(groups: list[list[dict]], deadline: float | None = None) -> list[list[dict]]:
    """그룹 대표(index 0)가 "관련 없다"고 확정된 그룹만 통째로 제외. 실패 시 안전하게 전부 통과."""
    if not groups:
        return groups

    if not llm_rate_limiter.LLM_ENABLED:
        print(f"[relevance_filter] LLM_ENABLED=off - 관련성 필터 스킵, {len(groups)}개 그룹 전부 통과")
        return groups

    key_env_var = "OPENROUTER_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[relevance_filter] 🔴 조치필요 [RF-11] - {key_env_var} 없음 - "
              f"관련성 필터 생략, {len(groups)}개 그룹 전부 통과")
        return groups

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER)
    print(f"[relevance_filter] 관련성 필터(그룹 단위) 시작 - model={model_desc}, 대상 {len(groups)}개 그룹(대표 기사 1건씩)")

    kept = []
    dropped_samples = []
    total_batches = (len(groups) + BATCH_SIZE - 1) // BATCH_SIZE
    with requests.Session() as session:
        for batch_num, i in enumerate(range(0, len(groups), BATCH_SIZE), start=1):
            if deadline is not None and time.monotonic() >= deadline:
                remaining = groups[i:]
                kept.extend(remaining)
                print(f"[relevance_filter] 🟡 주의 - 관련성 필터(그룹 단위) 시간 예산 소진 - "
                      f"남은 {len(remaining)}개 그룹은 필터링 없이 전부 통과 처리")
                break

            batch_groups = groups[i:i + BATCH_SIZE]
            batch_representatives = [g[0] for g in batch_groups]
            print(f"[relevance_filter] 그룹 배치 {batch_num}/{total_batches} 처리 중 ({len(batch_groups)}개 그룹)")
            results = _call_llm(batch_representatives, api_key, session)
            if results is None:
                kept.extend(batch_groups)
                continue
            for group, relevant in zip(batch_groups, results):
                if relevant:
                    kept.append(group)
                else:
                    dropped_samples.append(group[0].get("title", ""))

    dropped_count = len(groups) - len(kept)
    print(f"[relevance_filter] 관련성 필터(그룹 단위) 완료 - 전체 {len(groups)}개 그룹 중 "
          f"{dropped_count}개 제외, {len(kept)}개 유지")
    if dropped_samples:
        sample_n = min(10, len(dropped_samples))
        print(f"[relevance_filter] 제외된 그룹 대표 제목 샘플 (최대 {sample_n}건):")
        for title in dropped_samples[:sample_n]:
            print(f"   - {title}")

    return kept


def recategorize_uncategorized_groups(groups: list[list[dict]], deadline: float | None = None) -> list[list[dict]]:
    """그룹 대표가 "기타"인 그룹만 재분류. 재분류되면 그룹 내 "기타" 멤버 전원에게 같은 카테고리 적용."""
    targets = [g for g in groups if g[0].get("category") == "기타"]
    if not targets:
        return groups

    if not llm_rate_limiter.LLM_ENABLED:
        print(f"[relevance_filter] LLM_ENABLED=off - 카테고리 재분류 스킵, {len(targets)}개 그룹 '기타' 유지")
        return groups

    key_env_var = "OPENROUTER_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[relevance_filter] 🔴 조치필요 [RF-12] - {key_env_var} 없음 - "
              f"카테고리 재분류 생략, {len(targets)}개 그룹 '기타' 그대로 유지")
        return groups

    import keyword_tagger
    category_choices = list(keyword_tagger.CATEGORY_KEYWORDS.keys())
    category_list_text = "\n".join(f"- {c}" for c in category_choices)
    system_prompt = CATEGORY_RECLASSIFY_SYSTEM_PROMPT_TEMPLATE.format(category_list=category_list_text)

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER)
    print(f"[relevance_filter] 카테고리 재분류(그룹 단위) 시작 - model={model_desc}, 대상 {len(targets)}개 그룹(대표 기사가 '기타')")

    reclassified_count = 0
    skipped_count = 0
    total_batches = (len(targets) + BATCH_SIZE - 1) // BATCH_SIZE
    with requests.Session() as session:
        for batch_num, i in enumerate(range(0, len(targets), BATCH_SIZE), start=1):
            if deadline is not None and time.monotonic() >= deadline:
                skipped_count = len(targets) - i
                print(f"[relevance_filter] 🟡 주의 - 카테고리 재분류(그룹 단위) 시간 예산 소진 - "
                      f"남은 {skipped_count}개 그룹은 재분류 없이 '기타' 유지")
                break

            batch_groups = targets[i:i + BATCH_SIZE]
            batch_representatives = [g[0] for g in batch_groups]
            print(f"[relevance_filter] 카테고리 재분류(그룹) 배치 {batch_num}/{total_batches} "
                  f"처리 중 ({len(batch_groups)}개 그룹)")
            results = _call_category_llm(batch_representatives, api_key, session, category_choices, system_prompt)
            if results is None:
                continue
            for group, new_category in zip(batch_groups, results):
                if new_category != "기타":
                    for article in group:
                        if article.get("category") == "기타":
                            article["category"] = new_category
                    reclassified_count += 1

    print(f"[relevance_filter] 카테고리 재분류(그룹 단위) 완료 - {len(targets)}개 그룹 중 "
          f"{reclassified_count}개 재분류됨, {len(targets) - reclassified_count}개 '기타' 유지"
          + (f" (그중 {skipped_count}개는 시간 예산 소진으로 미시도)" if skipped_count else ""))

    return groups