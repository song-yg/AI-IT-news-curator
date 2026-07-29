"""
keyword_tagger.py
키워드 태깅 담당 모듈. 정규화(normalizer) 단계에서 각 기사의 제목을 KEYWORDS_KR/EN 사전과 매칭해 이슈 카테고리를 라벨링한다.

주의 - 이슈 그룹핑(issue_grouper.py)과는 완전히 별개 기능이다:
  - 이슈 그룹핑: "이 기사와 저 기사가 같은 사건을 다루는가" (BGE-M3 임베딩)
  - 키워드 태깅(이 모듈): "이 기사가 어떤 카테고리(질병/가격/제도 등)에 속하는가"
  둘은 입력도 출력도 다르고 서로 의존하지 않는다 - 이 모듈은 임베딩/LLM 없이 순수 문자열 매칭만 쓴다.

** lang 열 보정 **
원본 표에서 로봇공학·기타 IT 기술 카테고리 행들은 키워드 자체가 한글인데도
lang이 "en"으로 표시돼 있었다(예: "로봇" en, "블록체인" en). 실제 매칭 로직
(_build_flat_index)은 kr/en 리스트를 합쳐서 쓰기 때문에 어느 쪽에 넣어도
매칭 결과는 동일하지만, kr/en 구분을 문서/유지보수 목적으로 정확히 유지하기
위해 키워드 문자열이 실제로 한글이면 kr로, 로마자면 en으로 넣었다
("nvidia"도 같은 이유로 en으로 재배치). 원본 표의 lang 열을 그대로 신뢰하지
않았다는 점을 밝혀둔다 - 표 자체가 오기였을 가능성이 높다고 판단.
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

# 기존(사료·축산 도메인)에는 "AI"가 인공수정/영어 AI 등으로 오매칭될 위험이 커서
# EXCLUDED_TERMS에 넣어 아예 제외했었다. 지금은 도메인 자체가 AI 산업이라
# "AI"가 핵심 매칭어이므로 더 이상 제외할 이유가 없다 - 그대로 두면 "인공지능"
# 카테고리가 사실상 이 키워드로 기능을 못 하게 된다. "ND"(뉴캣슬병 약자)도
# 새 카테고리 어디에도 없는 축산 전용 용어라 같이 정리한다.
EXCLUDED_TERMS: set[str] = set()


def _build_flat_index():
    """
    {카테고리: {kr, en}} 구조를 {카테고리: [(매칭용 소문자 토큰, 원본 토큰), ...]}
    형태로 한 번만 평탄화해둔다 (매 기사마다 다시 만들지 않도록 모듈 로드
    시점에 1회만 계산).
    """
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
    리스트 안에서 다른 항목의 부분 문자열인 항목은 제거한다(대소문자 무시,
    더 구체적인/긴 쪽을 남김).

    배경: CATEGORY_KEYWORDS에 "feed cost"/"feed costs"처럼 한쪽이 다른 쪽의
    부분 문자열인 쌍이 다수 존재한다(corn/corn futures, wheat/wheat
    futures, 사료/배합사료업체, 계열화/계열화사업법 등). 이런 쌍은 같은
    개념을 사실상 두 번 세는 것이라, 제목에 "feed costs"가 있으면 "feed
    cost"와 "feed costs" 둘 다 매칭돼 그 카테고리의 매칭 개수가 인위적으로
    부풀려진다 - tag_title()의 "가장 많이 매칭된 카테고리 채택" 동점 처리
    공정성에 직접 영향을 준다(부풀려진 카테고리가 실제로는 안 이겨야 할
    경합에서 이기는 경우가 생길 수 있음).

    사전(CATEGORY_KEYWORDS) 자체는 원본 키워드표를 그대로 옮긴 것이라는
    이 모듈의 원칙(모듈 docstring 참고)을 지키기 위해, 사전을 고치는 대신
    이 집계 단계에서 구조적으로 해결한다 - 사전에 두 표현이 다 남아있어도
    "같은 언급"으로 인식되면 카운트는 1로만 잡힌다(더 구체적인 표현 쪽을
    matched_terms에 남겨서 정보 손실은 없음).

    예: ["corn", "corn futures", "CBOT corn"] -> ["corn futures", "CBOT corn"]
    """
    result = []
    for term in terms:
        term_lower = term.lower()
        if any(term != other and term_lower in other.lower() for other in terms):
            continue  # 리스트 안의 다른 항목에 완전히 포함되면 제외
        result.append(term)
    return result


def tag_title(title: str) -> tuple[str, list[str]]:
    """
    제목 하나를 카테고리에 매칭한다.

    매칭 방식: 대소문자 무시 부분 문자열(substring) 매칭. 카테고리별로 매칭된
    키워드 개수를 세서, 가장 많이 매칭된 카테고리를 채택한다 (여러 카테고리에
    걸치는 제목도 있을 수 있어서 - 예: "구제역으로 한우 수출 금지" 같은 제목은
    질병명/축종별/무역 세 카테고리 다 걸릴 수 있음). 동점이면 CATEGORY_KEYWORDS
    사전에 정의된 순서(= 키워드표 카테고리 번호 순) 상 먼저 나오는 쪽을 채택 -
    사전 순서 자체에 우선순위 의미는 없지만, 결과가 실행마다 흔들리지 않도록
    결정적(deterministic) 규칙 하나는 필요해서 정함.

    카테고리별 원 매칭 리스트에 _dedupe_contained를 적용해서, "corn"/"corn
    futures"처럼 한쪽이 다른 쪽에 포함되는 매칭은 하나로 센다(위
    _dedupe_contained 참고 - 매칭 개수 인위적 부풀림이 동점 처리 공정성을
    해치는 걸 방지).

    반환값: (category, matched_terms)
      - 아무 카테고리에도 안 걸리면 ("기타", []) - 2.2 스펙대로 기사를 탈락시키지
        않고 라벨만 "기타"로 붙인다.
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
    기사 리스트 전체에 카테고리를 매긴다 (in-place로 "category" 필드를 채움).

    주의 - WATT 소스와의 관계: WATT_collector는 사이트에서 직접 긁어온
    카테고리(예: "Poultry", "Nutrition" 등 WATT 자체 분류 체계)를 이미
    "category" 필드에 채워서 넘긴다. 반면 naver/gdelt는 이 정규화 단계에서
    채우도록 None으로 비워둔 채 넘어온다.

    이 함수는 소스에 상관없이 모든 기사에 "우리 시스템의 통일된 카테고리
    체계"(질병명/시장가격/정부제도 등, 이 모듈의 CATEGORY_KEYWORDS 기준)를
    새로 매겨서 "category" 필드를 덮어쓴다 - WATT의 원래 사이트 카테고리는
    체계가 다르고(사이트마다 자체 기준) 이 프로젝트의 카테고리별 집계에는
    안 맞아서다. 다만 정보 손실을 막기 위해 WATT의 원래 값은 "site_category"
    필드에 별도로 보존한다.
    """
    other_count = 0
    for article in articles:
        original_category = article.get("category")
        if original_category and article.get("source") not in ("네이버", "GDELT"):
            # WATT 계열만 원래 category가 사이트 자체 분류였을 수 있음
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
    """카테고리별 건수 분포를 눈으로 확인하기 위한 진단용 함수."""
    counter = Counter(a.get("category", "기타") for a in articles)
    total = len(articles)
    print(f"\n=== 카테고리 분포 (전체 {total}건) ===")
    for category, count in counter.most_common():
        pct = count / total * 100 if total else 0
        print(f"  {category:15s} {count:4d}건 ({pct:.1f}%)")


def print_uncategorized_sample(articles: list[dict], sample_size: int = 30) -> None:
    """
    '기타'로 분류된 기사 제목 샘플을 출력한다.

    '기타' 비율이 높게 나올 때, 숫자(비율)만으로는 실제로 뭐가 몰리는지
    감이 안 잡히므로 - 키워드 사전을 어떻게 보강할지 판단하려면 실제
    제목을 눈으로 봐야 한다.

    무작위 추출이 아니라 리스트 앞에서부터 sample_size개만 그대로 보여준다
    - 표본 대표성을 엄밀히 따지는 통계용이 아니라 "감 잡기용" 진단
    도구라 단순하게 유지 (print_category_distribution과 같은 성격).

    ** main.py의 정규 실행 경로에서는 더 이상 호출하지 않음 **: 매 실행마다
    기사 제목을 최대 30건까지 나열해서 운영 로그가 불필요하게 길어짐 -
    사전 보강이 필요할 때 직접 이 함수를 불러써서 확인하는 진단 도구로만
    남겨둔다.
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