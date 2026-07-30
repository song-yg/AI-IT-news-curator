"""
keyword_tagger.py
키워드 태깅 담당. 정규화 단계에서 기사 제목을 CATEGORY_KEYWORDS와 매칭해 카테고리를 라벨링한다.
순수 문자열 매칭만 사용 (임베딩/LLM 없음).

주의 - issue_grouper.py와 별개 기능:
  - 이슈 그룹핑: "같은 사건인가" (BGE-M3 임베딩)
  - 키워드 태깅(이 모듈): "어떤 카테고리(질병/가격/제도 등)인가"

lang 열 보정: 원본 표에서 로봇공학·기타 IT 기술 행들의 키워드가 한글인데 lang="en"으로
잘못 표시돼 있었다. 매칭 로직 자체는 kr/en을 합쳐서 쓰므로 결과에는 영향 없지만, 문서/유지보수
목적으로 실제 문자 구성(한글->kr, 로마자->en) 기준으로 재배치했다 ("nvidia"도 en으로 이동).
"""

import re
from collections import Counter

CATEGORY_KEYWORDS = {
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

# 사료·축산 도메인 시절엔 "AI"가 인공수정 등으로 오매칭될 위험이 커서 제외했었으나,
# 지금은 도메인 자체가 AI 산업이라 핵심 매칭어이므로 제외하지 않는다. "ND"(뉴캣슬병)도 정리.
EXCLUDED_TERMS: set[str] = set()


def _build_flat_index():
    """{카테고리: [(소문자 토큰, 원본 토큰)]} 형태로 평탄화 (모듈 로드 시 1회만 계산)."""
    index = {}
    for category, terms in CATEGORY_KEYWORDS.items():
        flat = []
        for term in terms["kr"] + terms["en"]:
            if term in EXCLUDED_TERMS:
                continue
            flat.append((term.lower(), term))
        index[category] = flat
    return index


_FLAT_INDEX = _build_flat_index()


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
    제목을 카테고리에 매칭. 대소문자 무시 부분 문자열 매칭, 카테고리별 매칭 개수가
    가장 많은 쪽을 채택 (한 제목이 여러 카테고리에 걸칠 수 있음). 동점 시 CATEGORY_KEYWORDS
    사전 순서(정의 순위 아님, 실행마다 결과 안 흔들리게 하기 위한 결정적 규칙)로 채택.

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

    WATT_collector는 사이트 자체 분류(예: "Poultry")를 이미 "category"에 채워서 넘기지만,
    이 함수는 소스 상관없이 우리 시스템 통일 카테고리 체계로 덮어쓴다. 정보 손실 방지를 위해
    WATT의 원래 값은 "site_category"에 별도 보존.
    """
    other_count = 0
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

    main.py 정규 실행 경로에서는 호출 안 함 (매번 최대 30건 나열하면 로그가 길어짐) -
    사전 보강 필요할 때 직접 불러쓰는 진단 도구.
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