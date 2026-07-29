"""
relevance_filter.py
관련성 필터 - 정규화(URL dedup + 키워드 태깅) 직후, 이슈 그룹핑
(임베딩 로드) 전에 실행한다.

배경: 키워드 매칭만으로는 걸러지지 않는 오매칭 유형이 다수 확인됨 -
동음이의어(질병명이 유튜버 닉네임인 경우), 검색어가 기관명/법안명 등
고유명사의 일부로만 등장하는 경우, 기사의 핵심 주제는 따로 있고 축산 관련
내용은 통계 나열 중 한 줄로만 등장하는 "각주성 언급" 등. 이런 유형은
키워드를 아무리 좁혀도(복합어, near() 등) 구조적으로 해결이 안 되는데,
LLM이 제목/요약 맥락을 읽으면 정확히 구분 가능한 문제라 필터 단계로 신설.

설계는 issue_grouper.py의 "3차 LLM 보조"와 동일한 패턴을 그대로 재사용한다
(provider 스위치, 배치 호출, 출력 개수/형식 검증, 실패 시 안전한 기본값
fallback) - 두 모듈이 서로 다른 판정을 하지만 LLM 호출 방식 자체는 같은
철학을 공유하므로 일관성을 위해 통일함.

기본값 방향이 issue_grouper와 반대라는 점에 주의: 그룹핑 3차는 "애매하면
false(안 묶음)"가 안전하지만, 이 필터는 "애매하면 true(통과)"가 안전하다 -
관련 있는 기사를 잘못 걸러내는 것보다, 무관한 기사가 몇 개 더 통과하는 편이
손실이 적다. 배치 호출 자체가 실패해도 마찬가지 이유로 그 배치는 전부
통과시킨다(안 걸러진 채로 이후 단계로 넘어감).

소스별로 LLM에 줄 수 있는 컨텍스트 양이 다르다는 한계가 있음 - 네이버는
description(짧은 요약), WATT는 body(본문 앞부분), GDELT는 제목뿐(GDELT는
스펙상 본문을 안 줌). GDELT 기사는 상대적으로 판정 근거가 부족하다.
"""

import json
import os

import requests

# ---------------------------------------------------------------------------
# LLM 프로바이더 설정 - issue_grouper.py와 완전히 동일한 스위치 방식 재사용
# (같은 프로젝트 안에서 프로바이더 전환 기준이 파일마다 다르면 혼란스러우므로
# 통일함). LLM_PROVIDER=anthropic(기본값) 또는 openrouter.
#
# os.environ.get(key, default) 대신 or를 쓰는 이유: GitHub Actions Variables에
# 빈 문자열로 설정된 경우 os.environ.get(key, default)는 default를 못 돌려주고
# 빈 문자열을 그대로 반환하는 버그가 이미 확인된 바 있음(issue_grouper.py
# 참고) - 같은 함정을 여기서도 피하기 위해 동일한 패턴 사용.
# ---------------------------------------------------------------------------
LLM_PROVIDER = os.environ.get("LLM_PROVIDER") or "anthropic"

LLM_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
LLM_API_URL_ANTHROPIC = "https://api.anthropic.com/v1/messages"

LLM_MODEL_OPENROUTER = os.environ.get("OPENROUTER_MODEL") or "openrouter/free"
LLM_API_URL_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

# --- 2단계 추가 폴백 모델 (issue_grouper.py와 동일한 방식, 아래 참고) ---
LLM_MODEL_OPENROUTER_2 = os.environ.get("OPENROUTER_MODEL_2") or ""
LLM_MODEL_OPENROUTER_3 = os.environ.get("OPENROUTER_MODEL_3") or ""

# (역할 라벨, 모델명) 쌍으로 관리 - issue_grouper.py와 동일한 이유
# (2026-07-28, 담당자 요청): 체인 길이가 OPENROUTER_MODEL_2/3 설정 여부에
# 따라 2~4로 들쭉날쭉해서, 리스트 인덱스만으로는 "2번째"가 항상 2순위
# 모델을 뜻하지 않게 됨 - 인덱스 대신 역할 자체를 라벨로 고정해서 로그에서
# 어떤 조합이든 정확히 어느 모델이 실패했는지 구분 가능하게 함
# (_request_llm_text 참고).
_LLM_MODEL_CHAIN_OPENROUTER_ROLES: list[tuple[str, str]] = []
if LLM_MODEL_OPENROUTER:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("1순위", LLM_MODEL_OPENROUTER))
if LLM_MODEL_OPENROUTER_2:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("2순위", LLM_MODEL_OPENROUTER_2))
if LLM_MODEL_OPENROUTER_3:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("3순위", LLM_MODEL_OPENROUTER_3))
if "openrouter/free" not in [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("최종 안전망", "openrouter/free"))

# 순위 라벨 -> 실패 시 찍을 오류 코드 (역할 고정)
_LLM_MODEL_ROLE_ERROR_CODE = {
    "1순위": "RF-07",
    "2순위": "RF-08",
    "3순위": "RF-09",
    "최종 안전망": "RF-10",
}

# 하위호환용 - 모델명만 뽑은 리스트(다른 곳에서 " -> ".join(...) 등으로 사용)
LLM_MODEL_CHAIN_OPENROUTER = [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]

# OpenRouter 권장 헤더(선택, ASCII 전용 - 한글 넣으면 UnicodeEncodeError남,
# issue_grouper.py에서 이미 실측 확인된 함정).
_OPENROUTER_X_TITLE = "feed-livestock-news-relevance-filter"

# 한 번의 API 호출에 몇 건까지 같이 물어볼지. issue_grouper의 LLM_BATCH_SIZE(20,
# 페어 단위)와 맞춤. 무료 라우터가 배치가 클 때 개수를 자주 못 맞추는
# 경향이 있어(예: 30건 배치에서 29건/31건 등) 20으로 유지 - id 기반 매칭
# (아래 _call_llm 참고)으로 어긋나도 배치 전체를 안 버리지만, 애초에
# 어긋나는 빈도 자체를 줄이는 것도 유효한 보강이라 같이 적용.
BATCH_SIZE = 20

# 기사 본문/요약 스니펫을 프롬프트에 넣을 때 자를 최대 길이. 너무 길면 배치당
# 토큰이 커져서 비용/실패 위험이 늘고, 판정에는 앞부분 몇 문장이면 충분하다고
# 판단.
SNIPPET_MAX_CHARS = 150


_SYSTEM_PROMPT = (
    # 영어로 작성 - 특히 작은 무료 모델일수록 학습 데이터가 영어 위주라
    # 형식 지시(JSON만 출력 등) 준수율이 더 안정적인 경향이 있고, 판단
    # 대상(기사 제목) 자체도 다국어라 지시문을 영어로 통일하는 게 더
    # 자연스럽다는 판단. 요약 생성 프롬프트(llm_summarizer.py)는 결과물이
    # 한국어여야 하므로 한국어로 유지.
    "You are a relevance classifier for a feed and livestock industry news "
    "curation system. For each article, decide whether it substantively "
    "covers the \"feed industry\" or \"livestock industry\" (animal "
    "husbandry, disease control, distribution, policy, etc.) as its actual "
    "topic.\n\n"
    "RELEVANT (true):\n"
    "- Articles whose core topic is livestock (cattle/pigs/chickens/ducks "
    "etc.) rearing, disease, disease control, livestock product "
    "(meat/eggs/milk etc.) production, pricing, trade, or livestock-related "
    "government policy/legislation\n"
    "- Articles whose core topic is feed ingredients (grains such as corn, "
    "soybean meal, wheat; international grain market conditions), feed "
    "additives (amino acids, probiotics, enzymes, antibiotic alternatives, "
    "etc.), or feed manufacturing/distribution (feed mills, premixes, "
    "compound feed, TMR, etc.)\n\n"
    "NOT RELEVANT (false) - the following are mismatch patterns that have "
    "actually been confirmed to repeat, so filter them out with particular "
    "care:\n"
    "1. Homonyms: the search term spells the same but refers to something "
    "different (e.g. a disease name that is actually a person's name/"
    "nickname, or \"feed\" referring to the pet food industry - pet food is "
    "out of scope for this project)\n"
    "2. Appears only as part of a proper noun: the search term is merely "
    "part of an institution name, bill name, etc. (e.g. embedded in the "
    "name of a National Assembly standing committee - the article's actual "
    "subject is unrelated political news)\n"
    "3. Footnote-level mention: the article's core topic is something else "
    "entirely (e.g. semiconductor exports, overall price index), and "
    "livestock-related content appears only briefly as one line among many "
    "statistics/items\n"
    "4. Grain not used for feed: articles about corn, wheat, soybeans, etc. "
    "where the grain is not for feed but for food, bioenergy (ethanol), or "
    "the general agricultural commodity market broadly, with no mention of "
    "feed or livestock at all (e.g. a corn price article that only "
    "discusses food-ingredient supply like popcorn/cereal or ethanol "
    "feedstock)\n\n"
    "Examples that MUST be kept as relevant (true) - these types have "
    "actually been mistakenly filtered out before, so pay special "
    "attention:\n"
    "- Articles about consumer prices/supply of livestock products "
    "(eggs/meat/milk etc.) are relevant even if the title doesn't contain "
    "words like \"livestock\" or \"animal husbandry\". This is true even "
    "for titles with a light tone using slang or buzzwords. For example, "
    "an article discussing the cause of an egg price surge using the "
    "buzzword \"rocket eggs\" is an article about egg (a livestock product) "
    "prices, so it is true - do not judge it false just because the tone "
    "is light or the word \"livestock\" doesn't appear.\n"
    "- Before marking such an article false, first confirm it doesn't "
    "actually match any of NOT RELEVANT criteria 1-4 (homonym / part of "
    "proper noun / footnote mention / grain not for feed) - if it clearly "
    "doesn't match any of them, it is automatically relevant.\n\n"
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
    기사에서 판정 근거로 쓸 스니펫을 뽑는다. 네이버는 description(짧은 요약),
    WATT는 body(본문 앞부분)를 쓰고, GDELT는 스펙상 본문이 아예 없어(body가
    항상 None) None을 반환한다 - 이 경우 프롬프트에 "요약 없음"으로 명시해
    모델이 없는 정보를 있는 척 채워 넣지 않게 한다(_build_user_prompt 참고).
    """
    text = article.get("description") or article.get("body")
    if not text:
        return None
    return text[:SNIPPET_MAX_CHARS]


def _build_user_prompt(batch: list[dict]) -> str:
    # 지시문은 영어로 작성(시스템 프롬프트와 같은 이유). "카테고리" 값
    # 자체(예: "기타", "질병명")는 keyword_tagger.py가 정하는 프로젝트
    # 전역 한글 라벨이라 번역 대상 아님 - 지시문 안에 한글 값이 섞여 들어가는
    # 건 정상(모델이 다국어 입력을 다루는 데는 문제없음).
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
    """
    LLM 원본 응답을 로그에 안전하게 남기기 위해 자른다. 파싱 실패/형식
    이상 로그에 "실제로 뭘 받았는지"가 없으면(예: 거부 메시지였는지, 빈
    문자열이었는지, JSON이 깨진 건지) 운영자가 원인을 구분할 방법이 없어서
    추가함. 너무 길면 로그가 지저분해지니 앞부분만 자르고, 줄바꿈은 한
    줄로 정리한다.
    """
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


def _request_anthropic(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session) -> str:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": LLM_MODEL_ANTHROPIC,
        "max_tokens": 1024,
        "temperature": 0,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    resp = session.post(LLM_API_URL_ANTHROPIC, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()


def _request_llm_text(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session,
                       label: str, validate=None):
    """
    LLM_PROVIDER에 맞는 경로로 실제 텍스트 응답을 받아온다.

    openrouter인 경우: _LLM_MODEL_CHAIN_OPENROUTER_ROLES(1순위 -> 2순위 ->
    3순위 -> 최종 안전망(openrouter/free))를 순서대로 시도한다. 앞 모델이
    실패하면(모델 이름 오타, 무료 티어에서 빠짐, 일시적 문제 등) 다음
    모델로 자동 재시도 - 특정 모델 하나에 고정한 설정이 그 모델만의 문제
    때문에 이 실행의 LLM 기능 전체(관련성 필터/재분류/그룹핑/요약 중 이
    함수를 부른 것)를 막는 걸 방지한다. 마지막 "최종 안전망"까지 실패하면
    더 폴백할 곳이 없으므로 그대로 예외를 올려보낸다 - 호출부가 기존처럼
    "이 배치 전체 안전 처리"로 흡수한다.

    ** 오류 코드를 역할(순위)별로 고정해서 분리한 이유(2026-07-28) **:
    issue_grouper.py와 동일 - "지정 모델 호출 실패"라는 로그 한 줄만으로는
    1순위/2순위/3순위 중 어느 자리가 실패한 건지 구분이 안 됐음.
    RF-07(1순위)/RF-08(2순위)/RF-09(3순위)/RF-10(최종 안전망)로 분리해서
    grep만으로 특정 순위의 실패 빈도를 바로 알 수 있게 함. 최종 안전망까지
    실패하는 건 완전 실패라 🔴 조치필요, 그 앞 순위 실패는 자동 복구되는
    경로라 🟡 주의.

    ** validate 콜백(2026-07-28 추가, issue_grouper.py와 동일한 방식) **:
    호출은 성공했는데 응답 형식이 이상한 경우도 호출 실패와 동일하게
    취급해 같은 재시도 체인에 태운다. validate(text, is_final)에서 예외를
    던지면 이번 모델 시도가 실패한 것으로 보고 다음 순위 모델로 재시도
    (위 RF-07~10 로그 경로 재사용). is_final=True는 최종 안전망까지 온
    시도라는 뜻 - 호출부가 여기서도 실패 시 예외를 던질지, 부분 복구로
    관대하게 처리할지 판단하는 데 씀.
    """
    if LLM_PROVIDER != "openrouter":
        text = _request_anthropic(system_prompt, user_prompt, api_key, session)
        return validate(text, True) if validate else text

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
    LLM API를 한 번 호출해서 batch 각각에 대한 relevant 판정을 받아온다.
    실패(호출 에러/JSON 파싱 실패/개수 불일치)하면 None을 반환한다 - 호출부
    (filter_articles)는 None이면 그 배치를 전부 통과시킨다.

    session: filter_articles가 배치마다 반복 호출하므로, 매번
    requests.post()로 새 연결을 맺는 대신 세션 하나를 재사용해 커넥션
    오버헤드를 줄인다.

    ** 형식 이상·id 누락 둘 다 모델 체인 재시도 대상 (2026-07-28) **:
    `_call_category_llm`에 적용한 것과 동일한 방식 - 호출은 성공했는데
    응답이 못 쓰는 수준(리스트가 아니거나 비어있음)이거나 id 일부가
    누락되면, "이 모델이 이번엔 못 쓰는 응답을 줬다"는 점에서 호출 실패와
    다를 게 없다고 보고 _request_llm_text의 validate 콜백으로 옮겨 다음
    순위 모델로 자동 재시도되게 함(RF-07~10 로그 경로 재사용). id 누락은
    최종 안전망(openrouter/free)까지 갔는데도 여전히 누락이면 더 물어볼
    곳이 없으니 예외 대신 있는 만큼만 살리고 나머지는 안전한 기본값
    (통과)으로 채워서 반환한다(RF-02로 로그) - 완전히 포기(RF-01)하는
    것보다 부분 성공이 낫다는 원칙 유지.
    """
    user_prompt = _build_user_prompt(batch)

    def _validate(text: str, is_final: bool) -> list[bool]:
        # 코드 펜스(```json ... ```)로 감싸서 올 때가 있어 방어적으로 벗겨낸다
        # (issue_grouper.py와 동일한 방어 로직 - 무료 모델은 이런 포맷 이탈이
        # Haiku보다 잦을 수 있어 특히 중요).
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())  # 실패하면 그대로 예외 -> 재시도 대상

        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")

        # "출력 개수 != 입력 개수"면 배치 전체를 버리는 대신, id를 명시적으로
        # 주고받게 해서 어긋나도 "일치하는 것만 살리고, 안 맞는 것만 개별적으로
        # 안전한 기본값(통과)으로 처리"한다 - 경미한 개수 어긋남에도 배치
        # 전체가 낭비되는 걸 방지.
        by_id: dict[int, bool] = {}
        for item in parsed:
            try:
                by_id[int(item["id"])] = bool(item["relevant"])
            except (KeyError, TypeError, ValueError):
                continue  # id/relevant 형식이 이상한 개별 항목만 무시하고 계속 진행

        missing = [idx for idx in range(1, len(batch) + 1) if idx not in by_id]
        if missing and not is_final:
            # 최종 안전망 전이면 물어볼 모델이 아직 남아있으니, 더 나은
            # 결과를 기대하고 다음 순위 모델로 재시도
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
    정규화+태깅이 끝난 articles를 받아, LLM이 "사료·축산업 뉴스가 아니다"라고
    확실히 판단한 것만 걸러내고 나머지를 반환한다.

    API 키가 없거나(LLM_PROVIDER에 맞는 키 환경변수 미설정) 모든 배치 호출이
    실패하면, 안전하게 원본 articles를 그대로 반환한다(필터를 그냥 안 거친
    것과 동일한 안전한 기본값).

    ** WATT 소스는 LLM 호출 없이 자동 통과 **
    WATT(WATTAgNet/Feed Strategy)는 그 자체가 사료·축산업 전문지라, 이
    필터가 잡으려는 오매칭 유형(동음이의어, 기관명 일부로만 등장, 각주성
    언급)은 "키워드 검색으로 긁어온" 네이버/GDELT에서만 발생하는 구조적
    문제고 WATT엔 애초에 해당 안 됨 - 그래서 LLM 호출 없이 안전하게
    통과시켜 호출 수를 아낀다. keyword_tagger.py가 site_category를 채울지
    판단할 때 쓰는 것과 같은 부정 조건(source not in ("네이버", "GDELT"))을
    재사용 - 같은 조건이라 같은 취약점도 그대로 적용됨: 나중에 새 소스가
    추가되면 그것도 "WATT 취급"돼 자동 통과될 위험이 있음(keyword_tagger.py
    tag_articles의 관련 주석 참고) - 현재 신규 소스 계획이 없어 그대로 둠.
    자동 통과된 기사는 반환 리스트 앞쪽에 모이므로, 이 함수를 거치면
    원래 수집 순서가 그대로 보존되지는 않는다 - 이후 단계(이슈 그룹핑/
    스코어링)는 순서에 의존하지 않으므로 문제 없음.
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

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER) if LLM_PROVIDER == "openrouter" else LLM_MODEL_ANTHROPIC
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
# keyword_tagger.py는 사전(CATEGORY_KEYWORDS) 단어가 제목에 있는지만 보고
# category를 정하는데, relevance_filter는 완전히 다른 기준(LLM이 "진짜
# 사료·축산 뉴스인가?")으로 관련성을 판단한다 - 그래서 사전 매칭엔 안 걸려
# category="기타"로 붙었는데 relevance_filter가 "관련 있음"으로 확정한
# 기사가 생길 수 있다. 이 기사는 필터를 통과해 살아남지만 category는
# 여전히 "기타"라서, "카테고리별 Top N"(기타 제외 설계)에는 영원히 못
# 들어가는 공백이 있었음 - 이 함수로 그 공백을 메운다.
#
# 관련성 판정(filter_articles)의 이진 true/false 스키마는 안 건드리고
# (더 복잡한 스키마를 물어보면 무료 모델의 JSON 파싱 실패율이 올라갈
# 위험이 있어서), 이미 "관련 있다"고 확정된 기사 중 category="기타"인
# 것만 별도 배치로 추려서 다시 LLM에 묻는 방식으로 분리했다 - 범위를
# 좁혀서 실패해도 영향이 제한적이게 함.

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
    _call_llm과 같은 호출/방어 패턴(코드펜스 벗기기, id 기반 부분 복구)을
    재분류용으로 그대로 재사용한 버전. 다른 점: 응답이 bool이 아니라
    category_choices 중 하나(또는 "기타")인 문자열이어야 하고, 그 목록에
    없는 값이 오면(모델이 카테고리명을 살짝 바꿔서 답하는 등) 안전하게
    무시한다(개별 항목만 "안 바뀜=기타 유지"로 처리 - id 자체가 아예
    누락된 경우와 동일하게 다룸).

    ** 형식 이상·id 누락 둘 다 모델 체인 재시도 대상 (2026-07-28) **:
    둘 다 "이 모델이 이번엔 못 쓰는 응답을 줬다"는 점에서 호출 실패와 다를
    게 없다고 보고, _request_llm_text의 validate 콜백으로 옮겨 실패 시
    다음 순위 모델로 자동 재시도되게 함(RF-07~10 로그 경로 재사용). 형식
    이상 전용 코드는 이걸로 완전히 은퇴했다. id 누락은 issue_grouper.py의
    id 누락(IG-03)과 달리 "완전 재시도 대상"으로 넣었는데(사용자 요청),
    대신 최종 안전망(openrouter/free)까지 갔는데도 여전히 누락이면 더
    물어볼 곳이 없으니 예외를 던지는 대신 있는 만큼만 살리고 나머지는
    안전한 기본값("기타")으로 채워서 반환한다(현재 RF-05로 로그 - 완전히
    포기(RF-04)하는 것보다 부분 성공이 낫다는 기존 원칙 유지).

    ** 코드 번호는 시간이 지나며 재배정됐다 **: 은퇴한 코드가 남긴 빈
    번호를 그때그때 당겨서 채우는 정책이라(담당자 요청), 이 파일의 오류
    코드 번호는 2026-07-28 하루에만 두 차례 바뀌었다 - 코드 안 주석에
    적힌 번호가 실제 print문과 다르게 보이면 이 파일이 진실이 아니라
    오류코드_전체목록.txt(항상 최신 기준으로 재추출)를 확인할 것.
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
            # 최종 안전망 전이면 물어볼 모델이 아직 남아있으니, 더 나은
            # 결과를 기대하고 다음 순위 모델로 재시도
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
    filter_articles()를 통과했지만 category="기타"로 남아있는 기사를 LLM으로
    재분류한다. main.py에서 filter_articles() 바로 다음 단계로 부른다.

    API 키가 없거나 전부 실패해도 안전하게 원본 그대로(재분류 없이) 반환.
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

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER) if LLM_PROVIDER == "openrouter" else LLM_MODEL_ANTHROPIC
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