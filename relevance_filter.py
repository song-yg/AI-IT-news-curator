"""
relevance_filter.py
관련성 필터 - 정규화(URL dedup + 키워드 태깅) 직후, 이슈 그룹핑(임베딩 로드) 전에 실행.

배경: 키워드 매칭만으로는 못 거르는 오매칭이 다수 확인됨 - 동음이의어(질병명이 유튜버
닉네임인 경우), 검색어가 기관명/법안명 등 고유명사의 일부로만 등장, 기사 핵심 주제는 따로
있고 관련 내용은 통계 나열 중 한 줄로만 등장하는 "각주성 언급" 등. 키워드를 아무리 좁혀도
구조적으로 해결이 안 되지만 LLM이 맥락을 읽으면 정확히 구분 가능해서 필터 단계로 신설.

설계는 issue_grouper.py의 3차 LLM 보조와 동일한 패턴 재사용 (provider 스위치, 배치 호출,
출력 검증, 실패 시 안전한 기본값 fallback) - 다른 판정이지만 호출 방식 철학은 통일.

기본값 방향이 issue_grouper와 반대: 그룹핑 3차는 "애매하면 false(안 묶음)"가 안전하지만,
이 필터는 "애매하면 true(통과)"가 안전 - 관련 기사를 잘못 거르는 것보다 무관한 기사가
몇 개 더 통과하는 편이 손실이 적음. 배치 호출 자체가 실패해도 그 배치는 전부 통과시킴.

소스별로 LLM에 줄 수 있는 컨텍스트 양이 다름 - 네이버는 description, WATT는 body, GDELT는
제목뿐(본문을 안 줌) - GDELT는 상대적으로 판정 근거가 부족함.
"""

import json
import os

import requests

# ---------------------------------------------------------------------------
# LLM 프로바이더 설정 - issue_grouper.py와 동일한 스위치 방식. LLM_PROVIDER=anthropic(기본) 또는 openrouter.
# os.environ.get(key, default) 대신 or를 쓰는 이유: GitHub Actions Variables가 빈 문자열로
# 설정되면 default를 못 돌려주는 버그가 확인됨(issue_grouper.py 참고) - 같은 함정 회피.
# ---------------------------------------------------------------------------
LLM_PROVIDER = os.environ.get("LLM_PROVIDER") or "anthropic"

LLM_MODEL_OPENROUTER = os.environ.get("OPENROUTER_MODEL") or "openrouter/free"
LLM_API_URL_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

# --- 2단계 추가 폴백 모델 (issue_grouper.py와 동일 방식) ---
LLM_MODEL_OPENROUTER_2 = os.environ.get("OPENROUTER_MODEL_2") or ""
LLM_MODEL_OPENROUTER_3 = os.environ.get("OPENROUTER_MODEL_3") or ""

# (역할 라벨, 모델명) 쌍으로 관리 - 체인 길이가 설정 여부에 따라 2~4로 들쭉날쭉해서 인덱스
# 대신 역할을 라벨로 고정, 어떤 조합이든 어느 모델이 실패했는지 로그에서 구분 가능(_request_llm_text 참고).
_LLM_MODEL_CHAIN_OPENROUTER_ROLES: list[tuple[str, str]] = []
if LLM_MODEL_OPENROUTER:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("1순위", LLM_MODEL_OPENROUTER))
if LLM_MODEL_OPENROUTER_2:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("2순위", LLM_MODEL_OPENROUTER_2))
if LLM_MODEL_OPENROUTER_3:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("3순위", LLM_MODEL_OPENROUTER_3))
if "openrouter/free" not in [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("최종 안전망", "openrouter/free"))

# 순위 라벨 -> 실패 시 찍을 오류 코드
_LLM_MODEL_ROLE_ERROR_CODE = {
    "1순위": "RF-07",
    "2순위": "RF-08",
    "3순위": "RF-09",
    "최종 안전망": "RF-10",
}

# 하위호환용 - 모델명만 뽑은 리스트
LLM_MODEL_CHAIN_OPENROUTER = [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]

# OpenRouter 권장 헤더 (ASCII 전용 - 한글 넣으면 UnicodeEncodeError, issue_grouper.py에서 확인됨)
_OPENROUTER_X_TITLE = "AI-IT-news-relevance-filter"

# 한 번의 API 호출에 몇 건까지 같이 물어볼지. issue_grouper의 LLM_BATCH_SIZE(20)와 맞춤
#  - 무료 라우터가 배치가 클 때 개수를 자주 못 맞추는 경향이 있어 20 유지.
BATCH_SIZE = 20

# 기사 본문/요약 스니펫을 프롬프트에 넣을 때 자를 최대 길이. 판정엔 앞부분 몇 문장이면 충분.
SNIPPET_MAX_CHARS = 150


_SYSTEM_PROMPT = (
    # 영어로 작성 - 작은 무료 모델일수록 형식 지시 준수율이 더 안정적.
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
    """
    판정 근거 스니펫 추출. 네이버는 description, WATT는 body, GDELT는 본문이 아예 없어
    None 반환 - 프롬프트에 "요약 없음"으로 명시해 모델이 없는 정보를 지어내지 않게 함.
    """
    text = article.get("description") or article.get("body")
    if not text:
        return None
    return text[:SNIPPET_MAX_CHARS]


def _build_user_prompt(batch: list[dict]) -> str:
    # 지시문은 영어. "category" 값(예: "기타")은 keyword_tagger.py가 정하는 한글 라벨이라 번역 대상 아님 - 지시문에 한글 값이 섞이는 건 정상.
    lines = ["Judge whether each of the following articles is relevant to feed/livestock industry news.\n"]
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
    """LLM 원본 응답을 로그용으로 자름 - 파싱 실패 시 실제로 뭘 받았는지 남겨서 원인 구분 가능하게."""
    if not text:
        return "(빈 응답)"
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _request_openrouter(system_prompt: str, user_prompt: str, api_key: str,
                         session: requests.Session, model_name: str) -> str:
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
    """
    LLM_PROVIDER에 맞는 경로로 텍스트 응답을 받아온다.

    openrouter면 _LLM_MODEL_CHAIN_OPENROUTER_ROLES(1순위->2순위->3순위->최종 안전망)를 순서대로 시도, 앞 모델이 실패하면 자동으로 다음 모델 재시도
      - 특정 모델 하나의 문제가 이 함수를 부른 기능(관련성 필터/재분류/그룹핑/요약) 전체를 막지 않게 함.
      - 최종 안전망까지 실패하면 예외를 올려보냄 - 호출부가 "배치 전체 안전 처리"로 흡수.

    오류 코드를 역할별로 고정(RF-07~10)해서 어느 순위가 실패했는지 grep으로 바로 확인 가능.
    최종 안전망 실패는 🔴 조치필요, 그 전 순위 실패는 자동 복구되는 경로라 🟡 주의.

    validate 콜백: 호출은 성공했는데 응답 형식이 이상한 경우도 호출 실패와 동일하게 취급해 같은 재시도 체인에 태운다.
    validate(text, is_final)가 예외를 던지면 다음 모델로 재시도.
    (is_final=True는 최종 안전망까지 온 시도라는 뜻 - 호출부가 예외를 던질지 부분 복구할지 판단)
    """

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
    """
    LLM API를 한 번 호출해 batch 각각의 relevant 판정을 받아온다. 실패 시 None.
    (filter_articles가 그 배치를 전부 통과시킴)

    session: filter_articles가 배치마다 반복 호출하므로 재사용해 커넥션 오버헤드 절감.

    형식 이상/id 누락 모두 모델 체인 재시도 대상(_request_llm_text의 validate로 처리)
      - 최종 안전망까지 갔는데도 id가 누락되면 예외 대신 있는 만큼만 살리고 나머지는 안전한 기본값(통과)으로 채운다(RF-02 로그).
      - 완전 포기(RF-01)보다 부분 성공이 낫다는 원칙.
    """
    user_prompt = _build_user_prompt(batch)

    def _validate(text: str, is_final: bool) -> list[bool]:
        # 코드펜스(```json ... ```)로 감싸서 올 때가 있어 방어적으로 벗겨냄
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())  # 실패하면 그대로 예외 -> 재시도 대상

        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")

        # 개수 불일치 시 배치 전체를 버리는 대신 id로 매칭해 일치하는 것만 살림
        by_id: dict[int, bool] = {}
        for item in parsed:
            try:
                by_id[int(item["id"])] = bool(item["relevant"])
            except (KeyError, TypeError, ValueError):
                continue

        missing = [idx for idx in range(1, len(batch) + 1) if idx not in by_id]
        if missing and not is_final:
            raise ValueError(f"id {missing} 누락(기대 {len(batch)}건 중 {len(missing)}건) - 다음 모델로 재시도")

        results = [by_id.get(idx, True) for idx in range(1, len(batch) + 1)]  # 안전한 기본값 - 통과
        if missing:
            print(f"[relevance_filter] 🟡 주의 [RF-02] - 관련성 필터 최종 안전망까지 갔지만 id {missing} "
                  f"여전히 누락(기대 {len(batch)}건 중 {len(missing)}건) - 그 항목들만 통과 처리, "
                  f"나머지 {len(batch) - len(missing)}건은 정상 판정 사용")
        return results

    try:
        return _request_llm_text(_SYSTEM_PROMPT, user_prompt, api_key, session, "관련성 필터", validate=_validate)
    except Exception as e:
        print(f"[relevance_filter] 🔴 조치필요 [RF-01] - LLM({LLM_PROVIDER}) 호출/파싱 실패 - 이 배치"
              f"({len(batch)}건) 전부 통과 처리: {type(e).__name__} - {e!r}")
        return None


def filter_articles(articles: list[dict]) -> list[dict]:
    """
    정규화+태깅이 끝난 articles를 받아 LLM이 "관련 없다"고 확실히 판단한 것만 걸러낸다.
    API 키 없음/모든 배치 실패 시 원본 그대로 반환 (필터 안 거친 것과 동일한 안전한 기본값).

    ** WATT 소스는 LLM 호출 없이 자동 통과 **
    WATT는 그 자체가 전문지라 이 필터가 잡으려는 오매칭 유형(동음이의어 등)이 구조적으로 해당 안 됨.
      - LLM 호출 없이 통과시켜 호출 수 절감.
      - keyword_tagger.py의 site_category 판단 조건(source not in ("네이버", "GDELT"))과 동일 조건 재사용.
      - 같은 취약점도 그대로 적용됨(새 소스 추가 시 "WATT 취급"돼 자동 통과될 위험, 현재 계획 없어 그대로 둠).
    자동 통과 기사가 반환 리스트 앞쪽에 모여 원래 수집 순서는 보존되지 않음 - 이후 단계는 순서 비의존이라 문제 없음.
    """
    if not articles:
        return articles

    watt_articles = [a for a in articles if a.get("source") not in ("네이버", "GDELT")]
    llm_target_articles = [a for a in articles if a.get("source") in ("네이버", "GDELT")]

    if watt_articles:
        print(f"[relevance_filter] WATT 소스 {len(watt_articles)}건은 업계 전문지 특성상 "
              f"LLM 호출 없이 자동 통과")

    if not llm_target_articles:
        return watt_articles

    key_env_var = "OPENROUTER_API_KEY" if LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[relevance_filter] 🔴 조치필요 [RF-03] - {key_env_var} 없음(LLM_PROVIDER={LLM_PROVIDER}) - "
              f"관련성 필터 생략, {len(llm_target_articles)}건(네이버/GDELT) 전부 통과")
        return articles

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER) if LLM_PROVIDER == "openrouter" else "Anthropic Claude"
    print(f"[relevance_filter] 관련성 필터 시작 - provider={LLM_PROVIDER}, "
          f"model={model_desc}, 대상 {len(llm_target_articles)}건(네이버/GDELT만, "
          f"WATT {len(watt_articles)}건 제외)")

    kept = list(watt_articles)
    dropped_samples = []
    total_batches = (len(llm_target_articles) + BATCH_SIZE - 1) // BATCH_SIZE
    with requests.Session() as session:
        for batch_num, i in enumerate(range(0, len(llm_target_articles), BATCH_SIZE), start=1):
            batch = llm_target_articles[i:i + BATCH_SIZE]
            print(f"[relevance_filter] 배치 {batch_num}/{total_batches} 처리 중 ({len(batch)}건)")
            results = _call_llm(batch, api_key, session)
            if results is None:
                kept.extend(batch)  # 이 배치는 전부 통과 (안전한 기본값)
                continue
            for article, relevant in zip(batch, results):
                if relevant:
                    kept.append(article)
                else:
                    dropped_samples.append(article.get("title", ""))

    dropped_count = len(articles) - len(kept)
    print(f"[relevance_filter] 관련성 필터 완료 - 전체 {len(articles)}건(WATT 자동통과 "
          f"{len(watt_articles)}건 포함) 중 {dropped_count}건 제외, {len(kept)}건 유지")
    if dropped_samples:
        sample_n = min(10, len(dropped_samples))
        print(f"[relevance_filter] 제외된 기사 샘플 (최대 {sample_n}건):")
        for title in dropped_samples[:sample_n]:
            print(f"   - {title}")

    return kept


# ---------------------------------------------------------------------------
# 카테고리 재분류
# ---------------------------------------------------------------------------
#
# keyword_tagger.py는 사전 단어가 제목에 있는지만 보고 category를 정하는데,
# relevance_filter는 완전히 다른 기준(LLM의 관련성 판단)으로 판단한다.
# 그래서 사전 매칭엔 안 걸려 category="기타"로 붙었는데 relevance_filter가 "관련 있음"으로 확정한 기사가 생길 수 있다.
# 이 기사는 필터를 통과하지만 category는 여전히 "기타"라, "기타" 제외 설계인 카테고리별 Top N엔 영원히 못 들어가는 공백이 있었음.
# 이 함수로 그 공백을 메운다.
#
# 관련성 판정의 이진 true/false 스키마는 안 건드리고(더 복잡한 스키마는 무료 모델 파싱 실패율을 높임), 이미 관련 있다고 확정된 기사 중 category="기타"인 것만 별도 배치로 다시 LLM에 묻는다.

CATEGORY_RECLASSIFY_SYSTEM_PROMPT_TEMPLATE = (
    "You are a categorization assistant for a feed and livestock industry "
    "news curation system. Each article below was not matched by "
    "dictionary-based keyword tagging and is currently labeled \"기타\" "
    "(uncategorized), but it has already been confirmed relevant to the "
    "feed/livestock industry by a separate relevance check. Assign the "
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
    """
    _call_llm과 같은 호출/방어 패턴을 재분류용으로 재사용.
    다른 점: 응답이 bool이 아니라 category_choices 중 하나(또는 "기타") 문자열이어야 하고, 목록에 없는 값은 개별 항목만 무시(안 바뀜="기타" 유지, id 누락과 동일하게 처리).

    형식 이상/id 누락 모두 모델 체인 재시도 대상 - 최종 안전망까지 갔는데도 누락이면 있는 만큼만
    살리고 나머지는 "기타"로 채워 반환(RF-05 로그) - 완전 포기(RF-04)보다 부분 성공이 낫다는 원칙.
    """

    user_prompt = _build_category_user_prompt(batch)
    valid_choices = set(category_choices) | {"기타"}

    def _validate(text: str, is_final: bool) -> list[str]:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())  # 실패하면 그대로 예외 -> 재시도 대상

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
                continue  # 목록에 없는 값 - 이 항목만 무시(기타 유지)
            by_id[idx] = category

        missing = [idx for idx in range(1, len(batch) + 1) if idx not in by_id]
        if missing and not is_final:
            raise ValueError(f"id {missing} 누락/형식 이상(기대 {len(batch)}건 중 {len(missing)}건) - 다음 모델로 재시도")

        results = [by_id.get(idx, "기타") for idx in range(1, len(batch) + 1)]
        if missing:
            print(f"[relevance_filter] 🟡 주의 [RF-05] - 카테고리 재분류 최종 안전망까지 갔지만 id {missing} "
                  f"여전히 누락(기대 {len(batch)}건 중 {len(missing)}건) - 그 항목들만 "
                  f"'기타' 유지, 나머지 {len(batch) - len(missing)}건은 정상 판정 사용")
        return results

    try:
        return _request_llm_text(system_prompt, user_prompt, api_key, session, "카테고리 재분류", validate=_validate)
    except Exception as e:
        print(f"[relevance_filter] 🔴 조치필요 [RF-04] - 카테고리 재분류 LLM({LLM_PROVIDER}) 호출/파싱 실패 - "
              f"이 배치({len(batch)}건) 전부 '기타' 유지: {type(e).__name__} - {e!r}")
        return None


def recategorize_uncategorized(articles: list[dict]) -> list[dict]:
    """
    filter_articles()를 통과했지만 category="기타"로 남은 기사를 LLM으로 재분류.
    main.py에서 filter_articles() 바로 다음 단계로 호출. API 키 없음/전부 실패해도 안전하게 원본 그대로 반환.
    """
    targets = [a for a in articles if a.get("category") == "기타"]
    if not targets:
        return articles

    key_env_var = "OPENROUTER_API_KEY" if LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[relevance_filter] 🔴 조치필요 [RF-06] - {key_env_var} 없음(LLM_PROVIDER={LLM_PROVIDER}) - "
              f"카테고리 재분류 생략, {len(targets)}건 '기타' 그대로 유지")
        return articles

    import keyword_tagger
    category_choices = list(keyword_tagger.CATEGORY_KEYWORDS.keys())
    category_list_text = "\n".join(f"- {c}" for c in category_choices)
    system_prompt = CATEGORY_RECLASSIFY_SYSTEM_PROMPT_TEMPLATE.format(category_list=category_list_text)

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER) if LLM_PROVIDER == "openrouter" else "Anthropic Claude"
    print(f"[relevance_filter] 카테고리 재분류 시작 - provider={LLM_PROVIDER}, "
          f"model={model_desc}, 대상 {len(targets)}건('기타'로 남았지만 관련성 확인된 기사)")

    reclassified_count = 0
    total_batches = (len(targets) + BATCH_SIZE - 1) // BATCH_SIZE
    with requests.Session() as session:
        for batch_num, i in enumerate(range(0, len(targets), BATCH_SIZE), start=1):
            batch = targets[i:i + BATCH_SIZE]
            print(f"[relevance_filter] 카테고리 재분류 배치 {batch_num}/{total_batches} "
                  f"처리 중 ({len(batch)}건)")
            results = _call_category_llm(batch, api_key, session, category_choices, system_prompt)
            if results is None:
                continue  # 이 배치는 전부 '기타' 유지 (원본 이미 '기타'라 손댈 것 없음)
            for article, new_category in zip(batch, results):
                if new_category != "기타":
                    article["category"] = new_category
                    reclassified_count += 1

    print(f"[relevance_filter] 카테고리 재분류 완료 - {len(targets)}건 중 "
          f"{reclassified_count}건 재분류됨, {len(targets) - reclassified_count}건 '기타' 유지")

    return articles