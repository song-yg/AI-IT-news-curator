"""
issue_grouper.py
이슈 그룹핑 담당 모듈.
  1차 - KR<->EN 키워드 사전 매칭
  2차 - BGE-M3 임베딩 코사인 유사도
  3차 - LLM 그룹핑 보조 (임계값 애매 구간)
  4차 - Top N 사후 재검토(stage4_dedupe_and_promote) - 최종 순위권 후보끼리만
        LLM으로 한 번 더 같은 사건인지 확인해 병합하고, 빈 자리는 다음 순위로 승격

group_issues()가 1~3차를 순서대로 실행해 최종 그룹 리스트를 만든다.
4차는 그룹핑이 아니라 스코어링 이후 단계라 group_issues()에는 안 들어있고, main.py가
score() 안에서 국내/해외 Top N과 카테고리별 Top N 각각에 stage4_dedupe_and_promote를
직접 호출한다.
"""

import csv
import json
import os
from itertools import combinations

import requests

import scorer  # 4차 병합 그룹 재스코어링용

from keyword_tagger import EXCLUDED_TERMS


# ---------------------------------------------------------------------------
# 1차: KR<->EN 키워드 사전 매칭
# ---------------------------------------------------------------------------
#
# 문서 예시: "조류독감"/"AI" <-> "avian flu"/"HPAI" 처럼, 같은 이슈를 가리키는 한글/영어 표현들을 하나의 묶음(set)으로 등록해둔다.
# 제목 안에 같은 묶음의 단어가 들어있는 두 기사는 "1차에서 바로 매칭"된다.
#
# 주의 - keyword_tagger.py의 CATEGORY_KEYWORDS와는 목적이 다르다:
#   - keyword_tagger: "이 기사가 질병명/시장가격/제도 중 어떤 카테고리인가"
#     (넓은 카테고리 분류용, 이미 구현됨)
#   - 여기(ISSUE_SYNONYM_GROUPS): "이 두 기사가 완전히 같은 사건을 다루는가"
#     (개별 이슈 매칭용, 카테고리보다 훨씬 좁고 구체적인 단위)
# 예를 들어 keyword_tagger는 "조류독감"과 "구제역"을 둘 다 "질병명" 카테고리
# 하나로 묶지만, 여기서는 서로 다른 이슈이므로 별개 그룹이어야 한다.
#
# ** 이슈 그룹핑의 정의: "동일 사건만" **
# 이유: 세계적으로 큰 이슈면 각 발생건이 각자 랭킹에 자연스럽게 올라올 테니 억지로 합칠 필요 없음.
#
# 아래 함수(_stage1_match_keys, stage1_group)는 인프라 자체는 남겨둔다.
# - 나중에 "완전 동일 사건"을 표현할 수 있는 더 구체적인 키(예: 특정 지명+ 특정 발생 시점 조합)로 다시 채울 가능성이 있을 때 재사용 가능. 지금은 빈 리스트라 이 함수들이 항상 매칭 없음(빈 set)을 반환.
# - 즉 모든 기사가 2차(임베딩)로 그대로 넘어감.
ISSUE_SYNONYM_GROUPS: list[set[str]] = []


def _stage1_match_keys(title: str) -> set[int]:
    """
    제목 하나가 ISSUE_SYNONYM_GROUPS의 몇 번 묶음(들)에 걸리는지 반환한다.
    (대소문자 무시 부분 문자열 매칭 - keyword_tagger.py의 tag_title과 같은 방식)

    반환값은 "걸린 묶음의 인덱스 집합" - 한 제목이 여러 묶음에 걸릴 수도 있어서 인덱스 하나가 아니라 set으로 돌려준다.
    """
    title_lower = title.lower()
    matched = set()
    for idx, synonyms in enumerate(ISSUE_SYNONYM_GROUPS):
        usable_synonyms = synonyms - EXCLUDED_TERMS
        if any(syn.lower() in title_lower for syn in usable_synonyms):
            matched.add(idx)
    return matched


# ---------------------------------------------------------------------------
# 그룹 병합 로직 (Union-Find, 일명 "합집합-찾기" 구조)
# ---------------------------------------------------------------------------
#
# 왜 필요한가: "A와 B가 매칭", "B와 C가 매칭"이라는 개별 판정 결과가 나왔을 때,
# 최종적으로는 "A, B, C가 전부 한 그룹"이어야 한다 (B를 통해 A와 C도 간접 연결됨).
# 이렇게 "여러 개의 쌍(pair) 매칭 결과를 하나의 그룹으로 합쳐주는" 표준 자료구조가 Union-Find다.
# 코드 자체는 짧지만 동작 원리가 직관적이지 않을 수 있어서 아래에 최대한 풀어서 주석을 달았다.
class UnionFind:
    """
    각 기사(인덱스)를 하나의 "그룹 대표"에 연결해두는 구조.
    - find(i): i가 속한 그룹의 "대표"가 누구인지 찾는다
    - union(i, j): i와 j를 같은 그룹으로 합친다
    """

    def __init__(self, n: int):
        # 처음엔 모두가 "자기 자신"을 대표로 하는 독립된 그룹
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        # i의 대표를 찾아 올라간다. 대표가 자기 자신이 아니면 계속 위로.
        while self.parent[i] != i:
            # 경로 압축: 다음에 더 빨리 찾도록 중간 노드를 대표에 바로 연결
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        root_i, root_j = self.find(i), self.find(j)
        if root_i != root_j:
            self.parent[root_j] = root_i

    def groups(self) -> list[list[int]]:
        """최종 그룹들을 [[기사 인덱스, ...], ...] 형태로 뽑아낸다."""
        buckets: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            root = self.find(i)
            buckets.setdefault(root, []).append(i)
        return list(buckets.values())


# ---------------------------------------------------------------------------
# 1차 매칭만으로 그룹 만들기 (2차 임베딩은 다음 스텝에서 추가)
# ---------------------------------------------------------------------------

def stage1_group(articles: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """
    1차 키워드 사전 매칭만으로 그룹을 만든다.

    반환값:
      grouped: 1차에서 이미 묶인 그룹들 (list[list[기사]])
                - 묶였다는 건 "같은 묶음(ISSUE_SYNONYM_GROUPS)에 걸린 기사가 2건 이상"이라는 뜻.
                  딱 1건만 걸린 경우는 "그룹"이 아니라 그냥 매칭 안 된 기사와 동일하게 취급해 unmatched로 보낸다(그룹은 최소 2건부터 의미가 있음).
      unmatched: 1차에서 못 잡은 기사들 -> 다음 스텝(2차 임베딩)으로 넘길 대상
    """
    n = len(articles)
    uf = UnionFind(n)

    # 각 묶음(synonym group) 인덱스에 걸린 기사들끼리 전부 union
    key_to_article_indices: dict[int, list[int]] = {}
    for i, article in enumerate(articles):
        keys = _stage1_match_keys(article.get("title", ""))
        for key in keys:
            key_to_article_indices.setdefault(key, []).append(i)

    for indices in key_to_article_indices.values():
        for a, b in combinations(indices, 2):
            uf.union(a, b)

    grouped = []
    unmatched = []
    for indices in uf.groups():
        if len(indices) >= 2:
            grouped.append([articles[i] for i in indices])
        else:
            unmatched.append(articles[indices[0]])

    return grouped, unmatched


# ---------------------------------------------------------------------------
# 2차: BGE-M3 임베딩 코사인 유사도 매칭
# ---------------------------------------------------------------------------
#
# 1차에서 못 잡은 기사만 대상으로, 전체 기사끼리(국내x해외 뿐 아니라 국내-국내/해외-해외도 포함) 코사인 유사도를 계산해서 threshold 이상이면 그룹핑한다.
#
# 임계값(THRESHOLD)은 아직 미확정 값이다. scorer.py의 PRESS_DEDUP_CAP과 같은 성격의 "잠정 상수"
# - issue_grouper.export_similarity_scores()로 뽑은 유사도 디버그 CSV를 직접 보고 조정한다.
THRESHOLD = 0.7

# threshold 근처 "애매한 구간"의 폭. 예를 들어 THRESHOLD=0.7, BORDERLINE_MARGIN=0.06이면 0.64~0.70 사이가 애매 구간 -> 3차(LLM 보조)대상.
# 3차(stage3_llm_assist)가 실제로 이 borderline_pairs를 입력받아 처리한다 - LLM이 "같은 사건"으로 확정한 쌍만 병합되고, 그 외(API 키 없음/호출 실패 등)는 여전히 "안 묶는" 보수적 기본값으로 fallback.
BORDERLINE_MARGIN = 0.06

# --- 유사도 디버그 CSV 스위치 ---
SIMILARITY_DEBUG_CSV = (os.environ.get("SIMILARITY_DEBUG_CSV") or "").strip().lower() in ("1", "true", "yes", "on")


def _embedding_text(article: dict) -> str:
    """
    임베딩에 넣을 텍스트를 만든다 - "제목 + 있으면 본문 요약 일부".
    본문(body)이 있는 소스(WATT)는 앞부분 200자만 덧붙인다 - 본문 전체를
    넣으면 계산량만 늘고, 어차피 "같은 이슈인지" 판단엔 도입부만으로 충분한
    경우가 대부분이라 짧게 자른다.
    """
    title = article.get("title", "")
    body = article.get("body")
    if body:
        return f"{title} {body[:200]}"
    return title


def _cosine_similarity_matrix(vectors):
    """
    벡터 리스트 전체에 대한 N x N 코사인 유사도 행렬을 계산한다.
    numpy만 있으면 되고, BGE-M3가 이미 정규화된 벡터를 주는 편이지만 혹시 몰라 직접 정규화도 한 번 해준다 (이중으로 해도 결과는 동일 - 이미 정규화된 벡터를 다시 정규화해도 크기가 안 변하므로 안전).
    """
    import numpy as np

    vectors = np.array(vectors)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    normalized = vectors / norms
    return normalized @ normalized.T


def export_similarity_scores(articles: list[dict], sim_matrix, threshold: float,
                              borderline_margin: float, path: str = "similarity_debug/similarity_scores.csv",
                              min_score: float = 0.4) -> str | None:
    """
    THRESHOLD/BORDERLINE_MARGIN을 눈으로 보고 직접 튜닝할 수 있도록, stage2_group이 계산한 유사도 행렬 전체를 CSV로 내보낸다.
    threshold-margin ~ threshold 구간(borderline)만이 아니라 그 좁은 구간 밖의 값들도 봐야 "임계값을 어디로 옮기면 어떤 쌍들이 추가/제외되는지"를 판단할 수 있어서 훨씬 넓게 뽑는다.

    min_score: 이 값 미만인 쌍은 아예 제외한다.
     - 기사 수가 n이면 쌍이 n*(n-1)/2개라 전부 다 내보내면(대부분 0에 가까운 무관한 쌍) 파일이 쓸데없이 커지고, 정작 튜닝에 필요한 "임계값 근처" 구간은 찾기 어려워짐.
     - 0.4는 잠정값으로, 필요하면 나중에 조정.

    status 컬럼은 "지금 설정(THRESHOLD/BORDERLINE_MARGIN)으로는 이 쌍이 어떻게 처리됐는지" 참고용으로 같이 넣음(merged/borderline/none)
     - 다만 이건 어디까지나 현재 값 기준 참고이고, 실제 튜닝 판단은 similarity 값과 제목 쌍을 직접 보고 하면 됨.

    실패해도(디스크 문제 등) 예외를 던지지 않고 로그만 남긴다.
     - 이건 진단/튜닝용 부가 기능이라, 실패했다고 그룹핑 자체가 죽으면 안 됨(storage.py의 파일 쓰기 실패 흡수 패턴과 같은 방향).
    """
    n = len(articles)
    rows = []
    for i, j in combinations(range(n), 2):
        sim = float(sim_matrix[i][j])
        if sim < min_score:
            continue
        if sim >= threshold:
            status = "merged"
        elif threshold - borderline_margin <= sim < threshold:
            status = "borderline"
        else:
            status = "none"
        rows.append((sim, articles[i], articles[j], status))

    rows.sort(key=lambda r: r[0], reverse=True)  # 유사도 높은 순 - 경계선을 위아래로 훑기 편하게

    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            # utf-8-sig: 엑셀에서 한글 CSV 열 때 깨지는 문제 방지(BOM 포함)
            writer = csv.writer(f)
            writer.writerow(["similarity", "status", "title_a", "title_b",
                              "category_a", "category_b", "source_a", "source_b"])
            for sim, a, b, status in rows:
                writer.writerow([
                    f"{sim:.4f}", status,
                    a.get("title", ""), b.get("title", ""),
                    a.get("category", ""), b.get("category", ""),
                    a.get("source", ""), b.get("source", ""),
                ])
    except OSError as e:
        print(f"[issue_grouper] 🟡 주의 [IG-01] - 유사도 디버그 CSV 저장 실패(그룹핑 결과에는 영향 없음): "
              f"{path} - {type(e).__name__}: {e}")
        return None

    print(f"[issue_grouper] 유사도 디버그 CSV 저장 완료 ({len(rows)}쌍, "
          f"min_score={min_score} 이상만) -> {path}")
    return path


def stage2_group(
    articles: list[dict],
    model=None,
    threshold: float = THRESHOLD,
    borderline_margin: float = BORDERLINE_MARGIN,
) -> tuple[list[list[dict]], list[dict], list[tuple[dict, dict, float]]]:
    """
    1차에서 못 잡은 기사들을 BGE-M3 임베딩으로 매칭한다.

    model: sentence_transformers.SentenceTransformer 인스턴스를 주입받는다
           (함수 안에서 직접 모델을 로드하지 않는 이유: 모델 로드 자체가 무거운 작업이라, 기사 배치마다 매번 새로 로드하면 안 되기 때문.
            - 호출하는 쪽(main.py)에서 한 번만 로드해서 넘겨주는 구조로 설계)

    반환값:
      grouped: 2차에서 새로 묶인 그룹들
      still_unmatched: 2차에서도 못 잡은 기사들 (혼자 남는 "단독 기사" - llm_summarizer의 (A-1) fallback 대상이 됨)
      borderline_pairs: threshold 근처 애매 구간에 걸린 (기사A, 기사B, 유사도) 쌍 목록
                        - group_issues()가 이 목록을 3차 LLM 보조(stage3_llm_assist)에 넘겨 최종 병합 여부를 판단한다
    """
    if not articles:
        return [], [], []

    texts = [_embedding_text(a) for a in articles]
    vectors = model.encode(texts, normalize_embeddings=True)
    sim_matrix = _cosine_similarity_matrix(vectors)

    # 임계값 튜닝용 디버그 CSV - 기본은 꺼져있음(위 SIMILARITY_DEBUG_CSV 선언부 참고).
    # 켜져 있을 때만 계산·저장하고, 실패해도 안전하게 로그만 남기고 계속 진행함(그룹핑 로직 자체와는 무관).
    if SIMILARITY_DEBUG_CSV:
        export_similarity_scores(articles, sim_matrix, threshold, borderline_margin)

    n = len(articles)
    uf = UnionFind(n)

    for i, j in combinations(range(n), 2):
        sim = float(sim_matrix[i][j])
        if sim >= threshold:
            uf.union(i, j)

    borderline_pairs = []
    for i, j in combinations(range(n), 2):
        if uf.find(i) == uf.find(j):
            # 이미 같은 그룹으로 확정됨 (직접이든 다른 기사를 거친 간접 연결이든) - 이 쌍의 판정은 최종 결과에 영향이 없으므로 스킵
            continue
        sim = float(sim_matrix[i][j])
        if threshold - borderline_margin <= sim < threshold:
            borderline_pairs.append((articles[i], articles[j], sim))

    grouped = []
    still_unmatched = []
    for indices in uf.groups():
        if len(indices) >= 2:
            grouped.append([articles[i] for i in indices])
        else:
            still_unmatched.append(articles[indices[0]])

    return grouped, still_unmatched, borderline_pairs


# ---------------------------------------------------------------------------
# 3차: LLM 그룹핑 보조 (임계값 애매 구간)
# ---------------------------------------------------------------------------
#
# 2차(임베딩) 유사도가 threshold 근처 애매 구간에 걸린 쌍만 LLM에 물어봐서 "같은 사건인지 아닌지" 최종 판단한다.
# (전수 호출 아님 - stage2_group에서 이미 borderline_pairs로 걸러진 소수만 대상)
#
# 판정 기준은 이슈 그룹핑의 정의를 그대로 따른다: "같은 사건"
# 이란 같은 주제여도 국가·장소·시점이 다르면 별개 이슈다. 이 기준을 LLM에게도 시스템 프롬프트로 명시한다.

LLM_PROVIDER = os.environ.get("LLM_PROVIDER")

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

# 순위 라벨 -> 실패 시 찍을 오류 코드 (역할 고정 - 체인 길이와 무관하게 항상 동일한 역할은 동일한 코드로 찍힘)
_LLM_MODEL_ROLE_ERROR_CODE = {
    "1순위": "IG-07",
    "2순위": "IG-08",
    "3순위": "IG-09",
    "최종 안전망": "IG-10",
}

# 기존 코드(다른 곳에서 로그 표시용으로 참조하는 " -> ".join(...) 등)와의 하위호환을 위해 모델명만 뽑은 리스트도 그대로 유지.
LLM_MODEL_CHAIN_OPENROUTER = [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]

LLM_BATCH_SIZE = 20  # 한 번의 API 호출에 몇 쌍까지 같이 물어볼지 (호출 수 절약, OpenRouter 무료 티어 분당/일 요청 한도 감안해도 안전한 크기)

# OpenRouter 요청에 붙이는 선택적 식별 헤더. HTTP 헤더 값은 latin-1(ASCII 계열) 인코딩만 허용되므로 반드시 ASCII 문자열이어야 한다.
# - 한글을 넣으면 매 배치가 UnicodeEncodeError로 죽는다(아래 자체 테스트에 이 상수의 ASCII 여부를 검증하는 assert가 있음).
_OPENROUTER_X_TITLE = "AI-IT-news-issue-grouping-stage3"

_LLM_SYSTEM_PROMPT = (
    # 영어로 작성 - relevance_filter.py와 같은 이유(무료 소형 모델의 형식 지시 준수율, 다국어 입력과의 일관성).
    "You are a judge that assists news issue grouping. Given two article titles, decide whether the two articles cover \"exactly the same "
    "event\". "
    "Even if they cover the same technology/topic, judge them as separate events if the company, product, or timing differs (e.g. an article about OpenAI's GPT model release and one about Google's Gemini model release are separate events even though the topic name (LLM release) is the same). "
    "This applies not only at the company level but also to different products or divisions within the same company (e.g. an article about a security vulnerability in AWS and one in Azure OpenAI Service are both cloud AI security issues but are separate events if the affected product differs). "
    "Also judge them as separate events when one article covers a single issue (A) alone and the other article covers several issues including that one (A, B, C, D, etc.) together - overlapping on one issue does not make them the same event, because the scope and focus covered are different (e.g. a standalone article on \"OpenAI's new safety policy announcement\" and an article on \"OpenAI's safety policy, funding round, and leadership changes combined\" are not the same event even though the safety policy overlaps, because the latter is a separate roundup-style article covering multiple developments together). "
    "Titles may be in different languages (Korean/English/other languages mixed) - judge them as the same event if they refer to the same event, regardless of language. "
    "If you are not certain, you must answer false (conservative default - it is safer not to group than to group incorrectly)."
    "Output only a JSON array with no other explanation. Each element must be in the form {\"id\": number, \"same_event\": true|false}, and id must exactly match the number of the input pair."
)


def _build_llm_user_prompt(pairs: list[tuple[dict, dict, float]]) -> str:
    # 지시문은 영어로 작성(시스템 프롬프트와 같은 이유).
    lines = ["Judge whether each of the following pairs of article titles covers exactly the same event.\n"]
    for idx, (a, b, _sim) in enumerate(pairs, start=1):
        lines.append(f"{idx}. A: \"{a.get('title', '')}\" / B: \"{b.get('title', '')}\"")
    lines.append(
        f'\nThere are {len(pairs)} pairs total. Include the number above as "id" in each '
        f'element and answer with a JSON array only (e.g. [{{"id": 1, "same_event": true}}, '
        f'{{"id": 2, "same_event": false}}, ...]). Do not omit any id or change the order.'
    )
    return "\n".join(lines)


def _snippet_for_log(text: str, limit: int = 200) -> str:
    """
    LLM 원본 응답을 로그에 안전하게 남기기 위해 자른다(relevance_filter.py의 동일 함수와 같은 목적)
     - 파싱 실패/형식 이상 로그에 "실제로 뭘 받았는지"가 없으면 운영자가 원인을 구분할 방법이 없어서 추가함.
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
        # OpenRouter 권장 헤더(선택) - 프로젝트 식별용, 없어도 동작함.
        # HTTP 헤더 값은 latin-1 인코딩만 허용되므로 ASCII 전용 문자열이어야 함(한글이 섞이면 UnicodeEncodeError로 매 배치가 실패함).
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
                       validate=None):
    """
    LLM_PROVIDER에 맞는 경로로 실제 텍스트 응답을 받아온다.

    openrouter인 경우: _LLM_MODEL_CHAIN_OPENROUTER_ROLES(1순위 -> 2순위 -> 3순위 -> 최종 안전망(openrouter/free), 위 상수 선언부 참고)를 순서대로 시도한다.
    앞 모델이 실패하면(모델 이름 오타, 무료 티어에서 빠짐, 일시적 문제 등) 다음 모델로 자동 재시도
     - 특정 모델 하나에 고정한 설정이 그 모델만의 문제 때문에 3차 그룹핑 보조 전체를 막는 걸 방지한다.
       마지막 "최종 안전망"까지 실패하면 더 폴백할 곳이 없으므로 예외를 그대로 올려보낸다.
     - 호출부가 기존처럼 "이 배치 전체 안 묶음"으로 흡수한다.
    """
    chain = _LLM_MODEL_CHAIN_OPENROUTER_ROLES
    last_error: Exception | None = None
    for idx, (role, model_name) in enumerate(chain):
        is_final = idx == len(chain) - 1
        try:
            if idx > 0:
                print(f"[issue_grouper] 🟡 주의 - 3차 그룹핑 {role} 모델('{model_name}')로 재시도 "
                      f"({idx + 1}/{len(chain)})")
            text = _request_openrouter(system_prompt, user_prompt, api_key, session, model_name)
            return validate(text, is_final) if validate else text
        except Exception as e:
            last_error = e
            code = _LLM_MODEL_ROLE_ERROR_CODE[role]
            level = "🔴 조치필요" if is_final else "🟡 주의"
            next_note = "더 시도할 모델 없음 - 이 배치 전체 '안 묶음' fallback" if is_final else "다음 후보 모델로 재시도"
            print(f"[issue_grouper] {level} [{code}] - 3차 그룹핑 {role} 모델('{model_name}') "
                  f"호출/응답 검증 실패 - {next_note}: {type(e).__name__} - {e!r}")
    raise last_error


def _call_llm(pairs: list[tuple[dict, dict, float]], api_key: str, session: requests.Session) -> list[bool] | None:
    """
    LLM API를 한 번 호출해서 pairs 각각에 대한 same_event 판정을 받아온다.
    LLM_PROVIDER 값에 따라 Anthropic(claude-haiku-4-5-20251001) 또는 OpenRouter(무료 라우터/모델)로 분기한다.
    - 요청 형식은 프로바이더마다 다르지만(Anthropic: content 블록 리스트, OpenRouter: OpenAI 호환 choices[0].message.content), 이후 파싱/검증 로직은 공통이다.

    session: stage3_llm_assist가 배치마다 반복 호출하므로, 세션을 재사용해 커넥션 오버헤드를 줄인다(relevance_filter.py와 동일한 방식).

    입력/출력 개수 불일치, JSON 파싱 실패, API 에러, 항목 형식 이상 등 신뢰할 수 없는 응답이면 None을 반환한다.
    - 출력 형식을 코드로 자동 검증해서 어긋나면 fallback하는 원칙을 여기서도 그대로 적용 (fallback은 이 함수를 부르는 stage3_llm_assist 쪽에서 "안 묶음"으로 처리).
    """
    user_prompt = _build_llm_user_prompt(pairs)

    def _validate(text: str, is_final: bool) -> list:
        # 코드 펜스(```json ... ```)로 감싸서 올 때가 있어 방어적으로 벗겨낸다.
        # (무료 모델은 이런 포맷 이탈이 Haiku보다 잦을 수 있어 특히 중요)
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())  # 실패하면 그대로 예외 -> 호출부가 재시도 대상으로 취급

        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")
        return parsed

    try:
        parsed = _request_llm_text(_LLM_SYSTEM_PROMPT, user_prompt, api_key, session, validate=_validate)
    except Exception as e:
        print(f"[issue_grouper] 🔴 조치필요 [IG-02] - 3차 LLM({LLM_PROVIDER}) 호출/파싱 실패 - 이 배치({len(pairs)}쌍)는 "
              f"전부 '안 묶음' fallback: {type(e).__name__} - {e!r}")
        return None

    # id 기반 부분 복구 - "출력 개수 != 입력 개수"면 배치 전체를 버리는 대신, id를 명시적으로 주고받게 해서 어긋나도 "일치하는 것만 살리고, 안 맞는 것만 개별적으로 안전한 기본값(안 묶음)으로 처리"한다.
    # relevance_filter.py와는 기본값 방향이 다름에 주의.
    # - 거기는 "애매하면 통과(true)"가 안전하지만, 여기는 "애매하면 안 묶음(false)"이 안전하다(잘못 묶는 것보다 안 묶는 게 안전).
    by_id: dict[int, bool] = {}
    for item in parsed:
        try:
            by_id[int(item["id"])] = bool(item["same_event"])
        except (KeyError, TypeError, ValueError):
            continue  # id/same_event 형식이 이상한 개별 항목만 무시하고 계속 진행

    results = []
    missing = []
    for idx in range(1, len(pairs) + 1):
        if idx in by_id:
            results.append(by_id[idx])
        else:
            missing.append(idx)
            results.append(False)  # 안전한 기본값 - 안 묶음

    if missing:
        print(f"[issue_grouper] 🟡 주의 [IG-03] - 3차 LLM({LLM_PROVIDER}) 출력에서 id {missing} 누락"
              f"(기대 {len(pairs)}쌍 중 {len(missing)}쌍) - 그 쌍들만 '안 묶음' 기본값 처리, "
              f"나머지 {len(pairs) - len(missing)}쌍은 정상 판정 사용")

    return results


def stage3_llm_assist(borderline_pairs: list[tuple[dict, dict, float]]) -> list[tuple[dict, dict, float]]:
    """
    2차에서 애매 구간에 걸린 쌍들을 LLM에 물어봐서, "같은 사건"으로 확정된 쌍만 골라 반환한다 (그룹을 지우는 게 아니라 묶을지 말지만 판단).

    LLM_PROVIDER(기본 anthropic)에 따라 필요한 API 키 환경변수가 다르다:
      anthropic  -> ANTHROPIC_API_KEY
      openrouter -> OPENROUTER_API_KEY (임시 로컬 검증용 - 위 프로바이더
                    스위치 주석 참조, 운영 환경에서는 쓰지 않을 것)

    해당 키가 없거나 모든 배치 호출이 실패하면, "안 묶음" 보수적 기본값으로 안전하게 fallback한다.
     - 이 경우 group_issues의 최종 결과는 3차가 아예 없던 이전 동작과 동일해지므로 전체 파이프라인이 죽지 않는다.
    """
    if not borderline_pairs:
        return []

    key_env_var = "OPENROUTER_API_KEY" if LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[issue_grouper] 🟡 주의 [IG-04] - {key_env_var} 없음(LLM_PROVIDER={LLM_PROVIDER}) - 3차 LLM 보조 생략, "
              f"애매 구간 {len(borderline_pairs)}쌍 전부 '안 묶음' 기본값 유지")
        return []

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER) if LLM_PROVIDER == "openrouter" else "claude-haiku-4-5-20251001"
    print(f"[issue_grouper] 3차 LLM 보조 시작 - provider={LLM_PROVIDER}, model={model_desc}, "
          f"대상 {len(borderline_pairs)}쌍")

    confirmed = []
    with requests.Session() as session:
        for i in range(0, len(borderline_pairs), LLM_BATCH_SIZE):
            batch = borderline_pairs[i:i + LLM_BATCH_SIZE]
            results = _call_llm(batch, api_key, session)
            if results is None:
                continue  # 이 배치만 안 묶음 유지, 다음 배치는 계속 시도
            for (a, b, sim), same_event in zip(batch, results):
                if same_event:
                    confirmed.append((a, b, sim))

    print(f"[issue_grouper] 3차 LLM 보조 완료 - 애매 구간 {len(borderline_pairs)}쌍 중 "
          f"{len(confirmed)}쌍 '같은 사건'으로 최종 병합")
    return confirmed


# ---------------------------------------------------------------------------
# 최종 진입점: 1차 + 2차 + 3차를 합쳐서 scorer.py에 바로 넘길 수 있는 형태로 반환
# ---------------------------------------------------------------------------

def group_issues(articles: list[dict], model=None) -> list[list[dict]]:
    """
    1차(사전) + 2차(임베딩) + 3차(LLM 보조)를 순서대로 실행해 최종 이슈 그룹 리스트를 만든다.

    이 함수의 반환값은 scorer.score_and_rank()가 받는 입력과 형태가 동일하다.
    (list[list[dict]]) - main.py의 score()가 이 함수를 호출해서 씀.

    ** 3차 병합 방식 **
    stage2_group의 결과(stage2_grouped 각 그룹 + still_unmatched 각 기사)를 "구성요소(component)"로 보고, stage3_llm_assist가 "같은 사건"으로 확정한 쌍만 이 구성요소들끼리 추가로 union한다 (Union-Find를 구성요소 단위로 한 번 더 적용.
     - 그룹 안에 이미 묶인 기사와 아직 단독인 기사가 한 쌍으로 확정될 수도 있으므로, "기사 단위"가 아니라 "구성요소 단위"로 합쳐야 한다).
       article의 url을 구성요소 식별에 쓴다 - 이 시스템의 공통 스키마상 url은 항상 존재하고 고유하다("완전 동일 기사 제거" 로직도 같은 전제로 URL을 키로 씀).
    """
    stage1_grouped, stage1_unmatched = stage1_group(articles)

    if model is None:
        # 모델이 안 주어졌으면(예: 아직 설치 전 단계 테스트) 2차 없이
        # 1차 결과 + 나머지를 단독 그룹으로 반환 - to_singleton_groups와
        # 동일한 안전한 fallback (2차가 없으면 3차의 재료인 borderline_pairs
        # 자체가 안 생기므로 3차도 자연히 생략됨)
        print("[issue_grouper] 🟡 주의 [IG-05] - 임베딩 모델이 없어 2차(임베딩) 매칭 생략 - 1차 결과만 사용")
        singleton = [[a] for a in stage1_unmatched]
        return stage1_grouped + singleton

    stage2_grouped, still_unmatched, borderline_pairs = stage2_group(stage1_unmatched, model=model)

    confirmed_pairs: list[tuple[dict, dict, float]] = []
    if borderline_pairs:
        print(f"[issue_grouper] 임계값 애매 구간 {len(borderline_pairs)}쌍 발견 - 3차 LLM 보조로 최종 판단")
        confirmed_pairs = stage3_llm_assist(borderline_pairs)

    components = stage2_grouped + [[a] for a in still_unmatched]
    components = _merge_confirmed_components(components, confirmed_pairs, extra_confirm=stage3_llm_assist)

    return stage1_grouped + components


def _merge_confirmed_components(components: list[list[dict]],
                                 confirmed_pairs: list[tuple[dict, dict, float]],
                                 extra_confirm=None) -> list[list[dict]]:
    """
    stage3_llm_assist가 확정한 쌍(confirmed_pairs)을 이용해 components(각각
    이미 확정된 이슈 그룹 혹은 단독 기사)를 추가로 병합한다.

    confirmed_pairs를 그냥 Union-Find로만 병합하면 "A~B가 확정되고 B~C가 확정됐다"는 이유만으로 A~C를 LLM에 한 번도 직접 물어본 적 없는데도 A+B+C가 통째로 한 그룹이 되는 연쇄(transitive chaining) 문제가 있다.
    _LLM_SYSTEM_PROMPT의 "지역이 다르면 별개 사건", "단신 vs 종합기사는 별개 사건" 같은 판정은 두 기사를 직접 비교했을 때만 유효한 결론이라, 간접 연결만으로 셋 이상을 한 그룹으로 넘겨짚으면 위험하다.

    수정 방식: edges(확정된 쌍)를 집합으로 따로 보관해두고, Union-Find 결과로 나온 각 연결 그룹에 대해 "그 그룹 안의 모든 쌍이 실제로 전부 직접 확정됐는지"(완전 그래프/클리크인지) 검증한다.
      - 클리크면(모든 쌍이 직접 확인됨) 안전하게 병합.
      - 클리크가 아니면(사슬로만 연결됐으면) 아래 "빠진 쌍 재확인" 라운드로
        넘어간다.

    extra_confirm: callable(pairs: list[tuple[dict, dict, float]]) -> list[tuple[dict, dict, float]]
     - 넘긴 쌍 중 "같은 사건"으로 확정된 것만 골라 돌려주는 함수.
     group_issues가 stage3_llm_assist를 그대로 넘겨줘서 같은 모델 체인/배치/로그 경로를 재사용한다.
     None이면(예: 테스트에서 LLM 없이 순수 로직만 검증하고 싶을 때) 재확인 없이 기존처럼 바로 포기 처리한다.
    """
    if not confirmed_pairs:
        return components

    url_to_component: dict[str, int] = {}
    for idx, comp in enumerate(components):
        for article in comp:
            url_to_component[article.get("url")] = idx

    edges: set[tuple[int, int]] = set()
    for a, b, _sim in confirmed_pairs:
        idx_a = url_to_component.get(a.get("url"))
        idx_b = url_to_component.get(b.get("url"))
        if idx_a is not None and idx_b is not None and idx_a != idx_b:
            edges.add((min(idx_a, idx_b), max(idx_a, idx_b)))

    if not edges:
        return components

    comp_uf = UnionFind(len(components))
    for idx_a, idx_b in edges:
        comp_uf.union(idx_a, idx_b)

    merged_components = []
    pending_recheck: list[tuple[int, ...]] = []  # 클리크 아닌 컴포넌트들 - 재확인 대상
    for indices in comp_uf.groups():
        if len(indices) <= 2:
            # 쌍 하나뿐이면 그 자체가 직접 확인된 관계라 연쇄 문제가 생길 여지가 없음 - 바로 병합.
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
            continue

        is_clique = all(
            (min(x, y), max(x, y)) in edges
            for x, y in combinations(indices, 2)
        )
        if is_clique:
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
        else:
            pending_recheck.append(indices)

    if pending_recheck and extra_confirm is not None:
        # 각 컴포넌트의 대표 기사(첫 기사)로 "아직 직접 확인 안 된" 쌍만 골라 한 번 더 물어본다. pair_lookup은 대표 기사 url 쌍 -> (idx_a, idx_b) 매핑
        # - extra_confirm이 돌려준 확정 쌍을 다시 컴포넌트 인덱스로 되짚어오기 위함.
        extra_candidates: list[tuple[dict, dict, float]] = []
        pair_lookup: dict[tuple, tuple[int, int]] = {}
        for indices in pending_recheck:
            for x, y in combinations(indices, 2):
                key = (min(x, y), max(x, y))
                if key in edges:
                    continue  # 이미 직접 확인된 쌍은 다시 안 물어봄
                rep_a, rep_b = components[x][0], components[y][0]
                extra_candidates.append((rep_a, rep_b, 0.0))
                pair_lookup[(rep_a.get("url"), rep_b.get("url"))] = key

        if extra_candidates:
            print(f"[issue_grouper] 사슬로만 연결된 컴포넌트 {len(pending_recheck)}개 발견 - "
                  f"빠진 쌍 {len(extra_candidates)}개만 대표 기사로 3차 LLM에 추가 확인")
            newly_confirmed = extra_confirm(extra_candidates)
            for a, b, _sim in newly_confirmed:
                key = pair_lookup.get((a.get("url"), b.get("url")))
                if key:
                    edges.add(key)

    for indices in pending_recheck:
        is_clique_now = all(
            (min(x, y), max(x, y)) in edges
            for x, y in combinations(indices, 2)
        )
        if is_clique_now:
            print(f"[issue_grouper] 사슬 컴포넌트 재확인으로 클리크 완성(컴포넌트 {len(indices)}개) - 병합")
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
        else:
            recheck_note = ("빠진 쌍을 추가로 재확인했지만 여전히 일부는 직접 확인 안 됨"
                             if extra_confirm is not None else "일부 쌍은 LLM에 직접 확인된 적 없음")
            print(f"[issue_grouper] 🟡 주의 [IG-06] - 3차 확정 쌍이 사슬로만 연결됨(컴포넌트 "
                  f"{len(indices)}개 - {recheck_note}) - 연쇄 병합 방지로 안 묶고 개별 유지")
            for idx in indices:
                merged_components.append(components[idx])

    return merged_components


# ---------------------------------------------------------------------------
# 4차: Top N 사후 재검토 + 병합 + 순위 승격
# ---------------------------------------------------------------------------
# Top N 후보끼리만 다시 "같은 사건인지" LLM 확인 후 병합, 빈 자리는 다음 순위로 승격.
# 3차와 판정 기준이 다름 - "시점이 달라도 같은 발병의 후속 보도면 같은 사건"으로 처리.
_STAGE4_SYSTEM_PROMPT = (
    "You are a judge that assists final de-duplication of a news issue "
    "ranking. Given two issue summaries (each made of one or more article "
    "titles about the same underlying story), decide whether they are "
    "actually reporting on the same real-world event or outbreak, even if "
    "worded very differently or reported on different days. "
    "Judge them as the SAME event if they describe the same disease "
    "outbreak or incident continuing, escalating, or being confirmed over "
    "time in the same country/region (e.g. an initial suspected-case "
    "report and a later article confirming wider spread of that same "
    "outbreak are the SAME event, even though the specific facts and "
    "wording changed as the story developed). "
    "Judge them as separate events if the country/region differs, or if "
    "they are genuinely unrelated incidents (different disease, different "
    "outbreak) that merely share a topic. "
    "When genuinely unsure, prefer NOT merging (same_event: false) - a "
    "missed merge is safer than an incorrect one. "
    "Output only a JSON array with no other explanation. Each element must "
    "be in the form {\"id\": number, \"same_event\": true|false}, and id "
    "must exactly match the number of the input pair."
)


def _build_stage4_user_prompt(pairs: list[tuple[dict, dict]]) -> str:
    """item(그룹)당 대표 제목 최대 5개를 A/B로 나열해 같은 사건인지 판정 요청."""
    lines = ["Judge whether each of the following pairs of issue summaries covers the same real-world event.\n"]
    for idx, (item_a, item_b) in enumerate(pairs, start=1):
        titles_a = " / ".join(item_a.get("titles", [])[:5]) or "(제목 없음)"
        titles_b = " / ".join(item_b.get("titles", [])[:5]) or "(제목 없음)"
        lines.append(f'{idx}. A: "{titles_a}" / B: "{titles_b}"')
    lines.append(
        f'\nThere are {len(pairs)} pairs total. Include the number above as "id" in each '
        f'element and answer with a JSON array only (e.g. [{{"id": 1, "same_event": true}}, '
        f'{{"id": 2, "same_event": false}}, ...]). Do not omit any id or change the order.'
    )
    return "\n".join(lines)


def _call_stage4_llm_batch(pairs: list[tuple[dict, dict]], api_key: str,
                            session: requests.Session) -> list[bool] | None:
    """단일 배치 호출 - _call_llm과 동일한 파싱/부분 복구 패턴. 배치 전체 실패 시 None."""
    user_prompt = _build_stage4_user_prompt(pairs)

    def _validate(text: str, is_final: bool) -> list:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())
        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")
        return parsed

    try:
        parsed = _request_llm_text(_STAGE4_SYSTEM_PROMPT, user_prompt, api_key, session, validate=_validate)
    except Exception as e:
        print(f"[issue_grouper] 🔴 조치필요 [IG-11] - 4차 재검토 LLM({LLM_PROVIDER}) 호출/파싱 실패 - "
              f"이 배치({len(pairs)}쌍)는 전부 '병합 안 함' fallback: {type(e).__name__} - {e!r}")
        return None

    by_id: dict[int, bool] = {}
    for item in parsed:
        try:
            by_id[int(item["id"])] = bool(item["same_event"])
        except (KeyError, TypeError, ValueError):
            continue

    results = []
    missing = []
    for idx in range(1, len(pairs) + 1):
        if idx in by_id:
            results.append(by_id[idx])
        else:
            missing.append(idx)
            results.append(False)  # 안전한 기본값 - 병합 안 함

    if missing:
        print(f"[issue_grouper] 🟡 주의 [IG-12] - 4차 재검토 LLM({LLM_PROVIDER}) 출력에서 id {missing} 누락"
              f"(기대 {len(pairs)}쌍 중 {len(missing)}쌍) - 그 쌍들만 '병합 안 함' 기본값 처리")

    return results


def _call_stage4_llm(pairs: list[tuple[dict, dict]], api_key: str, session: requests.Session) -> list[bool]:
    """LLM_BATCH_SIZE 단위로 배치 호출 후 결과 병합. 배치 실패분은 False(병합 안 함)로 채움."""
    results: list[bool] = []
    for i in range(0, len(pairs), LLM_BATCH_SIZE):
        batch = pairs[i:i + LLM_BATCH_SIZE]
        batch_results = _call_stage4_llm_batch(batch, api_key, session)
        results.extend(batch_results if batch_results is not None else [False] * len(batch))
    return results


def stage4_dedupe_and_promote(ranked_pool: list[dict], top_n: int, label: str = "") -> list[dict]:
    """
    ranked_pool(top_n 제한 없는 전체 순위 풀) 상위 top_n을 후보로 삼아 같은
    사건 쌍을 LLM으로 재확인, 병합하고 빈 자리는 다음 순위로 채운다.
    회차당 병합 1건만 적용 후 재판단, 최대 3회. API 키 없으면 기존 순위 그대로 반환.
    반환값은 scorer.score_and_rank(top_n=N)과 동일한 형태.
    """
    if top_n is None:
        top_n = len(ranked_pool)
    candidates = list(ranked_pool[:top_n])
    reserve = list(ranked_pool[top_n:])  # 승격 후보 풀 - 이미 점수순 정렬된 상태 유지

    if len(candidates) < 2:
        return candidates

    key_env_var = "OPENROUTER_API_KEY" if LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[issue_grouper] 🟡 주의 [IG-13] - {key_env_var} 없음(LLM_PROVIDER={LLM_PROVIDER}) - "
              f"4차 Top N 재검토 생략(기존 순위 그대로 사용)")
        return candidates

    prefix = f"[issue_grouper] {label} " if label else "[issue_grouper] "
    max_rounds = 3
    merged_any = False

    with requests.Session() as session:
        for round_idx in range(1, max_rounds + 1):
            if len(candidates) < 2:
                break

            index_pairs = list(combinations(range(len(candidates)), 2))
            llm_pairs = [(candidates[i], candidates[j]) for i, j in index_pairs]
            print(f"{prefix}4차 Top N 재검토 {round_idx}회차 - 후보 {len(candidates)}건({len(index_pairs)}쌍) 확인")
            results = _call_stage4_llm(llm_pairs, api_key, session)

            merge_at = next((k for k, same in enumerate(results) if same), None)
            if merge_at is None:
                break  # 이번 회차에 병합할 쌍 없음 - 더 볼 것 없으니 종료

            i, j = index_pairs[merge_at]
            item_a, item_b = candidates[i], candidates[j]
            rep_a = item_a["titles"][0] if item_a.get("titles") else "(제목 없음)"
            rep_b = item_b["titles"][0] if item_b.get("titles") else "(제목 없음)"
            print(f"{prefix}🔗 같은 사건으로 판정돼 병합: '{rep_a}' + '{rep_b}'")

            merged_articles = item_a.get("articles", []) + item_b.get("articles", [])
            merged_item = scorer.score_group(merged_articles)
            merged_any = True

            candidates = [c for k, c in enumerate(candidates) if k not in (i, j)]
            candidates.append(merged_item)

            if reserve:
                promoted = reserve.pop(0)
                rep_p = promoted["titles"][0] if promoted.get("titles") else "(제목 없음)"
                print(f"{prefix}⬆️ 빈 자리에 다음 순위 후보 승격: '{rep_p}'")
                candidates.append(promoted)

            candidates.sort(key=lambda c: c["issue_score"], reverse=True)
            candidates = candidates[:top_n]
        else:
            print(f"{prefix}🟡 주의 - 4차 재검토가 최대 {max_rounds}회 한도에 도달함 - "
                  f"남은 중복이 더 있을 수 있음(다음 실행에서 다시 확인됨)")

    if merged_any:
        print(f"{prefix}4차 Top N 재검토 완료 - 최종 {len(candidates)}건")

    return candidates

