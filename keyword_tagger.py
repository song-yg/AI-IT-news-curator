"""
keyword_tagger.py - 기사 제목을 CATEGORY_KEYWORDS와 매칭해 카테고리 라벨링(순수 문자열 매칭).
issue_grouper.py(같은 사건인가)와 별개 - 이 모듈은 어떤 카테고리인가만 판단.
CATEGORY_KEYWORDS는 구글 시트 category 컬럼에서 동적으로 읽음(tag_articles() 시작 시 갱신),
실패 시 _FALLBACK_CATEGORY_KEYWORDS 사용.
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
CATEGORY_KEYWORDS = _FALLBACK_CATEGORY_KEYWORDS


def _build_flat_index(category_keywords: dict) -> dict:
    """{카테고리: [(소문자 토큰, 원본 토큰)]}로 평탄화."""
    index = {}
    for category, terms in category_keywords.items():
        flat = [(t.lower(), t) for t in terms["kr"] + terms["en"] if t not in EXCLUDED_TERMS]
        index[category] = flat
    return index


_FLAT_INDEX = _build_flat_index(CATEGORY_KEYWORDS)


def _refresh_category_keywords() -> None:
    """구글 시트에서 CATEGORY_KEYWORDS/_FLAT_INDEX 갱신. tag_articles() 시작 시 1회 호출."""
    global CATEGORY_KEYWORDS, _FLAT_INDEX
    CATEGORY_KEYWORDS = keyword_source.get_category_keywords(_FALLBACK_CATEGORY_KEYWORDS)
    _FLAT_INDEX = _build_flat_index(CATEGORY_KEYWORDS)


def _dedupe_contained(terms: list[str]) -> list[str]:
    """다른 항목의 부분 문자열인 항목 제거(예: "corn" -> "corn futures"에 흡수)."""
    result = []
    for term in terms:
        term_lower = term.lower()
        if any(term != other and term_lower in other.lower() for other in terms):
            continue
        result.append(term)
    return result


def tag_title(title: str) -> tuple[str, list[str]]:
    """제목을 카테고리에 매칭(대소문자 무시 부분 문자열, 매칭 최다 카테고리 채택). ("기타", [])면 미매칭."""
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
    """기사 리스트 전체에 category 필드를 채움(in-place)."""
    other_count = 0
    _refresh_category_keywords()
    for article in articles:
        original_category = article.get("category")
        if original_category and article.get("source") not in ("네이버", "GDELT"):
            article["site_category"] = original_category

        category, matched_terms = tag_title(article.get("title", ""))
        article["category"] = category
        article["matched_keywords"] = matched_terms

        if category == "기타":
            other_count += 1

    total = len(articles)
    other_ratio = (other_count / total * 100) if total else 0.0
    print(f"[keyword_tagger] {total}건 중 '기타' {other_count}건 ({other_ratio:.1f}%)")

    return articles


def print_category_distribution(articles: list[dict]) -> None:
    """카테고리별 건수 분포 출력."""
    counter = Counter(a.get("category", "기타") for a in articles)
    total = len(articles)
    print(f"\n=== 카테고리 분포 (전체 {total}건) ===")
    for category, count in counter.most_common():
        pct = count / total * 100 if total else 0
        print(f"  {category:15s} {count:4d}건 ({pct:.1f}%)")


def print_uncategorized_sample(articles: list[dict], sample_size: int = 30) -> None:
    """'기타' 분류 기사 제목 샘플 출력(진단용, 정규 실행 경로에서는 미호출)."""
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