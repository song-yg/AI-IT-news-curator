"""
keyword_tagger.py
키워드 태깅 담당. 정규화 단계에서 기사 제목을 CATEGORY_KEYWORDS와 매칭해 카테고리를 라벨링한다.
순수 문자열 매칭만 사용 (임베딩/LLM 없음).

주의 - issue_grouper.py와 별개 기능:
  - 이슈 그룹핑: "같은 사건인가" (BGE-M3 임베딩)
  - 키워드 태깅(이 모듈): "어떤 카테고리(질병/가격/제도 등)인가"

** CATEGORY_KEYWORDS 출처: 구글 시트의 category 컬럼 **
keyword_source.get_category_keywords()로 naver/gdelt 키워드와 같은 시트에서 읽어온다 -
시트에 category 컬럼이 없거나(구버전 스키마) 읽기 실패 시 _FALLBACK_CATEGORY_KEYWORDS로
안전하게 대체. tag_articles() 시작 시 한 번 갱신하므로, 그 이후 실행되는 다른 모듈
(relevance_filter.py의 카테고리 재분류 등)은 항상 갱신된 값을 본다(파이프라인 순서 의존 -
정규화가 항상 관련성 필터/재분류보다 먼저 실행되므로 안전).
"""

import re
from collections import Counter

import keyword_source

_FALLBACK_CATEGORY_KEYWORDS = {
    "인공지능": {
        "kr": ['인공지능', 'AI', '머신러닝', '딥러닝', '챗GPT', '생성형 AI', '빅데이터'],
        "en": ['Artificial intelligence'],
    },
    "기업 단위": {
        "kr": ['엔비디아'],
        "en": ['nvidia'],
    },
    "로봇공학": {
        "kr": ['자율주행', '로봇', '드론', '메타버스'],
        "en": [],
    },
    "기타 IT 기술": {
        "kr": ['가상현실', '증강현실', '블록체인', 'NFT', '클라우드', '사이버보안', '양자컴퓨팅'],
        "en": [],
    },
}

EXCLUDED_TERMS: set[str] = set()

# 모듈 로드 시점엔 fallback으로 시작 - tag_articles()가 구글 시트에서 다시 읽어 갱신한다.
CATEGORY_KEYWORDS = _FALLBACK_CATEGORY_KEYWORDS


def _build_flat_index(category_keywords: dict) -> dict:
    """{카테고리: [(소문자 토큰, 원본 토큰)]} 형태로 평탄화."""
    index = {}
    for category, terms in category_keywords.items():
        flat = []
        for term in terms["kr"] + terms["en"]:
            if term in EXCLUDED_TERMS:
                continue
            flat.append((term.lower(), term))
        index[category] = flat
    return index


_FLAT_INDEX = _build_flat_index(CATEGORY_KEYWORDS)


def _refresh_category_keywords() -> None:
    """구글 시트의 category 컬럼으로 CATEGORY_KEYWORDS/_FLAT_INDEX를 다시 만든다. tag_articles() 시작 시 1회 호출."""
    global CATEGORY_KEYWORDS, _FLAT_INDEX
    CATEGORY_KEYWORDS = keyword_source.get_category_keywords(_FALLBACK_CATEGORY_KEYWORDS)
    _FLAT_INDEX = _build_flat_index(CATEGORY_KEYWORDS)


def _dedupe_contained(terms: list[str]) -> list[str]:
    """
    리스트 안에서 다른 항목의 부분 문자열인 항목은 제거 (더 구체적인/긴 쪽만 남김).

    "feed cost"/"feed costs"처럼 한쪽이 다른 쪽을 포함하는 키워드 쌍이 사전에 여러 개 있어서,
    그대로 두면 같은 언급이 두 번 카운트돼 tag_title()의 동점 처리가 왜곡된다.
    사전 자체는 원본 표 그대로 두고, 집계 단계에서 구조적으로 해결.

    예: ["corn", "corn futures", "CBOT corn"] -> ["corn futures", "CBOT corn"]
    """
    result = []
    for term in terms:
        term_lower = term.lower()
        if any(term != other and term_lower in other.lower() for other in terms):
            continue
        result.append(term)
    return result


def tag_title(title: str) -> tuple[str, list[str]]:
    """
    제목을 카테고리에 매칭.
    대소문자 무시 부분 문자열 매칭, 카테고리별 매칭 개수가 가장 많은 쪽을 채택 (한 제목이 여러 카테고리에 걸칠 수 있음).
    동점 시 CATEGORY_KEYWORDS 사전 순서(정의 순위 아님, 실행마다 결과 안 흔들리게 하기 위한 결정적 규칙)로 채택.

    _dedupe_contained로 "corn"/"corn futures" 같은 포함 관계 매칭은 하나로 셈.

    반환: (category, matched_terms). 아무 카테고리에도 안 걸리면 ("기타", []).
    """
    if not title:
        return "기타", []

    title_lower = title.lower()
    best_category = None
    best_matches: list[str] = []

    for category, flat_terms in _FLAT_INDEX.items():
        raw_matches = [orig for lower, orig in flat_terms if lower in title_lower]
        matches = _dedupe_contained(raw_matches)
        if len(matches) > len(best_matches):
            best_category = category
            best_matches = matches

    if best_category is None:
        return "기타", []
    return best_category, best_matches


def tag_articles(articles: list[dict]) -> list[dict]:
    """
    기사 리스트 전체에 카테고리를 매김 (in-place로 "category" 필드 채움).

    이 함수는 소스 상관없이 우리 시스템 통일 카테고리 체계로 "category"를 덮어쓴다.
    네이버/GDELT가 아닌 소스가 나중에 추가되고 그 소스가 자체 분류(예: "Poultry")를 이미
    "category"에 채워서 넘기는 경우를 대비해, 원래 값은 정보 손실 없이 "site_category"에
    별도 보존해두는 조건도 남겨둠 - 지금은 실제로 그런 소스가 없어(네이버/GDELT뿐) 이
    분기가 항상 미실행 상태.
    """
    other_count = 0
    _refresh_category_keywords()
    for article in articles:
        original_category = article.get("category")
        if original_category and article.get("source") not in ("네이버", "GDELT"):
            article["site_category"] = original_category

        category, matched_terms = tag_title(article.get("title", ""))
        article["category"] = category
        article["matched_keywords"] = matched_terms  # 디버깅/검수용, 저장 스펙 확정 시 유지 여부 재검토

        if category == "기타":
            other_count += 1

    total = len(articles)
    other_ratio = (other_count / total * 100) if total else 0.0
    print(f"[keyword_tagger] {total}건 중 '기타' {other_count}건 ({other_ratio:.1f}%) "
          f"- 비율이 높으면 사전에 신규 키워드 추가 검토")

    return articles


def print_category_distribution(articles: list[dict]) -> None:
    """카테고리별 건수 분포 진단용 출력."""
    counter = Counter(a.get("category", "기타") for a in articles)
    total = len(articles)
    print(f"\n=== 카테고리 분포 (전체 {total}건) ===")
    for category, count in counter.most_common():
        pct = count / total * 100 if total else 0
        print(f"  {category:15s} {count:4d}건 ({pct:.1f}%)")


def print_uncategorized_sample(articles: list[dict], sample_size: int = 30) -> None:
    """
    '기타' 분류 기사 제목 샘플 출력 - '기타' 비율이 높을 때 실제로 뭐가 몰리는지 눈으로 확인용.
    무작위가 아니라 리스트 앞에서부터 그대로 보여줌 (감 잡기용 진단 도구, 통계용 아님).

    main.py 정규 실행 경로에서는 호출 안 함 (매번 최대 30건 나열하면 로그가 길어짐)
      - 사전 보강 필요할 때 직접 불러쓰는 진단 도구.
    """
    uncategorized = [a for a in articles if a.get("category", "기타") == "기타"]
    total = len(uncategorized)
    print(f"\n=== '기타' 분류 기사 샘플 (전체 {total}건 중 최대 {sample_size}건) ===")
    if total == 0:
        print("  (해당 없음)")
        return
    for i, article in enumerate(uncategorized[:sample_size], start=1):
        source = article.get("source", "?")
        title = article.get("title", "(제목 없음)")
        print(f"  {i:2d}. [{source}] {title}")
    if total > sample_size:
        print(f"  ... 외 {total - sample_size}건 생략")