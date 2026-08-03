"""
scorer.py
언급빈도 계산(Scoring) 담당. 순수 계산만 함 - LLM 안 씀.
(예전엔 "언급빈도 x 최신가중치"였음 - 일간 전환 이후 최신가중치를 없애고 mention_count
그대로를 issue_score로 씀. 대신 "24시간 초과 신선도 판정"(is_stale)을 여기서 제공하고,
실제 필터링은 main.py가 정규화 직후 수행함 - 아래 "신선도 판정" 섹션 참고)

이 모듈의 함수는 전부 "이슈 그룹"(같은 사건 기사 묶음, list[dict])을 입력으로 받는다.
실제 그룹핑(BGE-M3)은 issue_grouper.group_issues()가 하고, 여기서는 결과를 스코어링만 함.

`to_singleton_groups()`는 그룹핑이 없던 초기 개발 단계에 스코어링만 먼저 검증하려고 만든 테스트용 유틸.
지금은 실제 그룹핑 결과를 바로 넘기므로 사용되지 않음.

국내-해외 교차 매칭 🔗 태그는 score_group의 cross_axis_partner 필드로 구현 (main.py의 score()가 채워줌 - 상세는 score_group docstring 참고).
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone


# --- 신선도(freshness) 판정 ---
#
# 예전엔 여기 recency_weight(계단형 가중치)가 있었음 - 일간 전환(DAYS_BACK=1) 이후 재검토한
# 결과, 데일리 배치 구조에서는 오히려 역효과였음: naver/gdelt 각 collector가 "수집 시점
# 기준 24시간 이내"로 이미 걸러주는데, 실제 스코어링은 그보다 한참 뒤(GDELT 수집에 최대
# 220분, 이후 필터링/그룹핑까지 몇 시간 더)에 일어나서 recency_weight가 "얼마나 오래된
# 뉴스냐"가 아니라 "하루 중 몇 시 뉴스냐"에 더 가깝게 작동했음 - 늦게 터진 속보는 아직
# 언론사들이 못 받아써서 mention_count가 원래 낮은데 가중치까지 높게 받고, 일찍 터져서
# 하루 종일 보도된(그래서 mention_count가 높은) 이슈는 가중치가 깎이는 이중 왜곡이 있었음.
# 그래서 가중치는 완전히 없애고 issue_score = mention_count로 단순화하고, 대신 "24시간
# 초과"는 가중치로 깎는 게 아니라 아예 스코어링 이전 단계(main.py normalize 직후)에서
# 걸러내기로 함(is_stale 참고) - 그 시점 기준으로 걸러야 스코어링 시점까지 시간이 더
# 지나면서 새로 24시간을 넘기는 경계 사례를 최대한 줄일 수 있다.
FRESHNESS_WINDOW_HOURS = 24  # naver/gdelt 둘 다 DAYS_BACK=1(24시간)과 맞춤


def hours_elapsed(published_at: str, reference: datetime | None = None) -> float:
    """경과시간 계산(시간 단위, 기준 시각 기본값은 지금). 미래 날짜 등 이상치는 0으로 clamp."""
    pub_dt = datetime.fromisoformat(published_at)
    ref = reference if reference is not None else datetime.now(pub_dt.tzinfo or timezone.utc)
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    delta_seconds = (ref - pub_dt).total_seconds()
    return max(0.0, delta_seconds / 3600)


def is_stale(published_at: str, reference: datetime | None = None) -> bool:
    """FRESHNESS_WINDOW_HOURS(24시간)를 넘겼는지 - main.py가 정규화 직후 이 기준으로 걸러낸다."""
    return hours_elapsed(published_at, reference) > FRESHNESS_WINDOW_HOURS


# --- 동일 언론사 도배 dedup ---
# 같은 언론사가 같은 이슈를 반복 게재하는 경우만 mention_count 집계를 제한한다.
# 서로 다른 언론사가 각자 취재하는 경우는 캡을 걸지 않고 원본 그대로 반영.
PRESS_DEDUP_CAP = 3  # 언론사당 그룹 내 최대 카운트 (잠정값, 미확정)


def _press_of(article: dict) -> str:
    """
    언론사 식별자. naver/gdelt는 "press"(도메인) 필드가 있고, WATT는 없는 대신
    "source"(예: "WATTAgNet")가 사실상 언론사 역할이라 이를 대체값으로 씀.
    """
    return article.get("press") or article.get("source") or "(미상)"


def dedup_group_by_press(group: list[dict], cap: int = PRESS_DEDUP_CAP) -> list[dict]:
    """
    그룹 내 같은 언론사 기사가 cap건을 넘으면 초과분을 잘라낸다.
    최신순 정렬 후 앞에서부터 cap개만 유지 (오래된 반복 게재보다 최근 것 우선).
    서로 다른 언론사끼리는 캡 미적용 - 이는 실제 화제성 신호이기 때문.
    """
    by_press: dict[str, list[dict]] = defaultdict(list)
    for article in group:
        by_press[_press_of(article)].append(article)

    kept = []
    for press, press_articles in by_press.items():
        press_articles.sort(key=lambda a: a.get("published_at", ""), reverse=True)
        kept.extend(press_articles[:cap])
    return kept


def score_group(group: list[dict]) -> dict:
    """
    이슈 그룹 하나를 스코어링한다.

    반환값:
      issue_score: mention_count와 동일(dedup 이후 건수) - 예전엔 recency_weight를 곱해
        더한 값이었는데, 일간 전환 이후 재검토해서 가중치를 없앴다(신선도는 이제
        스코어링이 아니라 main.py 정규화 직후 is_stale()로 아예 걸러내는 쪽으로 이동 -
        위 "신선도 판정" 섹션 참고). 그래도 필드 자체는 정렬/화면 표시에 계속 쓰이므로
        이름은 유지.
      mention_count: dedup 이후 건수 (화면 노출용)
      raw_mention_count: dedup 이전 원본 건수 (data/scored.json에만 보존)
      titles / urls: 그룹 내 전체 제목/원문 링크 (LLM 요약 단계에서 사용)
      press_list: 참여 언론사 목록
      cross_axis_partner: 같은 이슈가 국내/해외 양쪽에서 동시에 다뤄진 경우 반대 축 대표 제목 (없으면 None).
                          main.py의 score()가 국내/해외로 나누기 전 article dict에 "_cross_axis_partner"(내부용)를 붙여두면 여기서 꺼내옴.
    """
    raw_mention_count = len(group)
    deduped = dedup_group_by_press(group)
    mention_count = len(deduped)

    cross_axis_partner = None
    for a in group:
        if a.get("_cross_axis_partner"):
            cross_axis_partner = a["_cross_axis_partner"]
            break

    return {
        "issue_score": mention_count,
        "mention_count": mention_count,
        "raw_mention_count": raw_mention_count,
        "titles": [a["title"] for a in group],
        "urls": [a["url"] for a in group],
        "press_list": sorted({_press_of(a) for a in group}),
        "cross_axis_partner": cross_axis_partner,
        "articles": group,  # 하위 단계(LLM 요약 등)에서 원본 기사 접근용
    }


def score_and_rank(groups: list[list[dict]], top_n: int | None = None) -> list[dict]:
    """
    이슈 그룹 리스트를 스코어링하고 issue_score 내림차순 정렬.
    top_n 지정 시 상위 N개만 반환(main.py에서 LLM 요약 호출 전 top_n=5로 넘기면 "일간 Top 5" 운영이 됨).

    각 축(국내/해외)의 원본 issue_score를 그대로 비교 - 정규화나 통합 종합 랭킹은 만들지 않음.
    (이 함수는 이미 한 축으로 분리된 groups만 받는다는 전제, 호출부가 국내/해외 각각 호출)
    """
    scored = [score_group(g) for g in groups]
    scored.sort(key=lambda s: s["issue_score"], reverse=True)
    return scored[:top_n] if top_n is not None else scored


def to_singleton_groups(articles: list[dict]) -> list[list[dict]]:
    """"기사 1건 = 그룹 1개"로 변환. 현재 미사용 - 스코어링 로직만 단독 테스트할 때 쓰는 유틸."""
    return [[a] for a in articles]


def _is_korean_title(title: str, threshold: float = 0.2) -> bool:
    """
    GDELT는 영어 키워드로 검색해도 한국어 기사가 번역 인덱싱으로 걸려 들어올 때가 있어서, 제목의 한글 비율이 threshold 이상이면 국내 기사로 판단한다.
    langdetect 등은 제목처럼 짧은 텍스트에서 신뢰도가 낮아, 결정론적인 한글 유니코드(U+AC00-D7A3) 비율 체크를 쓴다.
    """
    if not title:
        return False
    hangul_count = sum(1 for ch in title if "\uac00" <= ch <= "\ud7a3")
    non_space_count = sum(1 for ch in title if not ch.isspace())
    if non_space_count == 0:
        return False
    return (hangul_count / non_space_count) >= threshold


def _is_korean_gdelt_article(article: dict) -> bool:
    """
    GDELT 기사가 실제로 한국어(국내) 기사인지 판단.
    GDELT가 자체 판별한 "language" 필드가 있으면 우선 사용(제목 글자 수보다 신뢰도 높음), 없으면 _is_korean_title()로 fallback.

    main.py의 score()와 split_domestic_international()이 이 함수 하나만 공유해서 국내/해외 판별 기준이 Top N 스코어링과 카테고리 집계 사이에서 어긋나지 않게 한다.
    """
    language = article.get("language")
    if language:
        return language == "Korean"
    return _is_korean_title(article.get("title", ""))


def split_domestic_international(articles: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    국내/해외 집계 축 분리. 네이버=국내, WATT/GDELT=해외.
    GDELT로 수집됐지만 실제로 한국어 기사면 "국내"로 재분류 (_is_korean_gdelt_article 참고).
    """
    domestic = []
    international = []
    for a in articles:
        if a.get("source") == "네이버":
            domestic.append(a)
        elif a.get("source") == "GDELT" and _is_korean_gdelt_article(a):
            domestic.append(a)
        else:
            international.append(a)
    return domestic, international


def _majority_category(group: list[dict]) -> str:
    """
    그룹 내 가장 많이 나온 category를 대표로 정한다 (그룹 내 기사마다 사전 매칭이 미묘하게 다를 수 있어 다수결).
    동률이면 먼저 나온 걸 유지 (Counter.most_common()은 안정정렬이라 삽입 순서 보존).
    """
    counts = Counter(a.get("category", "기타") for a in group)
    return counts.most_common(1)[0][0]


def score_by_category(groups: list[list[dict]], top_n: int,
                       exclude: tuple[str, ...] = ("기타",),
                       dedupe_fn=None) -> dict[str, list[dict]]:
    """
    카테고리별 Top N. 국내/해외 축과 독립적인 카테고리 축을 만들되 국내/해외 구분은 유지.
    이 함수는 domestic_groups/international_groups 중 하나를 받아 그 축 안에서만 카테고리별로 나눈다 (main.py가 국내용/해외용으로 각각 호출).

    "기타"는 기본 제외 (카테고리별 Top N의 목적과 성격이 다름).
    이슈 없는 카테고리는 결과 dict에 키 자체가 안 남음 (빈 리스트를 안 만들어서 "이슈 없음"과 "카테고리 자체 없음"을 구분할 필요를 없앰).

    dedupe_fn: 넘기면 카테고리별 전체 순위 풀을 dedupe_fn(전체_풀, top_n, category)에
    태워 그 결과를 씀(issue_grouper.stage4_dedupe_and_promote 용도). scorer.py는
    issue_grouper를 import 안 하므로(순환 참조) 콜백 방식. None이면 기존처럼
    score_and_rank(top_n=top_n)로 바로 자른다.
    """
    by_category: dict[str, list[list[dict]]] = defaultdict(list)
    for group in groups:
        category = _majority_category(group)
        if category in exclude:
            continue
        by_category[category].append(group)

    result = {}
    for category, cat_groups in by_category.items():
        if dedupe_fn is not None:
            full_pool = score_and_rank(cat_groups, top_n=None)
            ranked = dedupe_fn(full_pool, top_n, category)
        else:
            ranked = score_and_rank(cat_groups, top_n=top_n)
        if ranked:
            # 이후 단계에서 여러 카테고리 결과를 평평한 리스트로 합칠 때 원래 카테고리를 추적할 수 있도록 항목 자체에 남겨둠
            for item in ranked:
                item["category"] = category
            result[category] = ranked
    return result


def print_category_top_n(label: str, category_ranked: dict[str, list[dict]], n: int) -> None:
    """카테고리별 Top N 진단용 출력."""
    if not category_ranked:
        print(f"\n=== {label} 카테고리별 Top {n} === (해당 카테고리 이슈 없음)")
        return
    print(f"\n=== {label} 카테고리별 Top {n} ===")
    for category, ranked in category_ranked.items():
        print(f"\n[{category}]")
        for i, item in enumerate(ranked, start=1):
            rep_title = item["titles"][0] if item["titles"] else "(제목 없음)"
            print(f"  {i}. [언급 {item['mention_count']}건] {rep_title}")
            if len(item["titles"]) > 1:
                print(f"     (그룹 내 추가 {len(item['titles']) - 1}건 생략)")
            if item.get("cross_axis_partner"):
                print(f"     🔗 반대 축에서도 다뤄짐: {item['cross_axis_partner']}")


def print_top_n(label: str, ranked: list[dict], n: int = 5) -> None:
    """상위 N개 이슈 진단용 출력."""
    print(f"\n=== {label} Top {min(n, len(ranked))} ===")
    for i, item in enumerate(ranked[:n], start=1):
        rep_title = item["titles"][0] if item["titles"] else "(제목 없음)"
        print(f"{i}. [언급 {item['mention_count']}건] {rep_title}")
        if len(item["titles"]) > 1:
            print(f"   (그룹 내 추가 {len(item['titles']) - 1}건 생략)")
        if item.get("cross_axis_partner"):
            print(f"   🔗 반대 축에서도 다뤄짐: {item['cross_axis_partner']}")