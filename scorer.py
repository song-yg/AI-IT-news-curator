"""
scorer.py - 언급빈도 계산(순수 계산, LLM 안 씀). 이슈 그룹(list[dict])을 받아 스코어링만 함.
실제 그룹핑은 issue_grouper.group_issues()가 담당.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone


def is_in_window(published_at: str, window_start: datetime, window_end: datetime) -> bool:
    """published_at(ISO 8601)이 [window_start, window_end) 안에 있는지 확인."""
    pub_dt = datetime.fromisoformat(published_at)
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    return window_start <= pub_dt < window_end


PRESS_DEDUP_CAP = 3  # 언론사당 그룹 내 최대 카운트


def _press_of(article: dict) -> str:
    return article.get("press") or article.get("source") or "(미상)"


def dedup_group_by_press(group: list[dict], cap: int = PRESS_DEDUP_CAP) -> list[dict]:
    """같은 언론사 기사가 cap건 넘으면 최신순으로 cap개만 유지."""
    by_press: dict[str, list[dict]] = defaultdict(list)
    for article in group:
        by_press[_press_of(article)].append(article)

    kept = []
    for press, press_articles in by_press.items():
        press_articles.sort(key=lambda a: a.get("published_at", ""), reverse=True)
        kept.extend(press_articles[:cap])
    return kept


def score_group(group: list[dict]) -> dict:
    """이슈 그룹 하나를 스코어링(issue_score=mention_count, cross_axis_partner 등 포함)."""
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
        "articles": group,
    }


def score_and_rank(groups: list[list[dict]], top_n: int | None = None) -> list[dict]:
    """이슈 그룹 리스트를 스코어링하고 issue_score 내림차순 정렬."""
    scored = [score_group(g) for g in groups]
    scored.sort(key=lambda s: s["issue_score"], reverse=True)
    return scored[:top_n] if top_n is not None else scored


def _is_korean_title(title: str, threshold: float = 0.2) -> bool:
    """제목의 한글 비율이 threshold 이상이면 국내로 판단."""
    if not title:
        return False
    hangul_count = sum(1 for ch in title if "\uac00" <= ch <= "\ud7a3")
    non_space_count = sum(1 for ch in title if not ch.isspace())
    if non_space_count == 0:
        return False
    return (hangul_count / non_space_count) >= threshold


def _is_korean_gdelt_article(article: dict) -> bool:
    """GDELT 기사가 실제 한국어 기사인지 판단(language 필드 우선, 없으면 제목 기준)."""
    language = article.get("language")
    if language:
        return language == "Korean"
    return _is_korean_title(article.get("title", ""))


def split_domestic_international(articles: list[dict]) -> tuple[list[dict], list[dict]]:
    """국내/해외 축 분리. 네이버=국내, GDELT는 한국어면 국내로 재분류."""
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
    """그룹 내 최다 category를 대표로."""
    counts = Counter(a.get("category", "기타") for a in group)
    return counts.most_common(1)[0][0]


def score_by_category(groups: list[list[dict]], top_n: int,
                       exclude: tuple[str, ...] = ("기타",),
                       dedupe_fn=None) -> dict[str, list[dict]]:
    """카테고리별 Top N("기타" 제외). dedupe_fn 넘기면 그걸로 상위 재검토(issue_grouper 콜백)."""
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
            for item in ranked:
                item["category"] = category
            result[category] = ranked
    return result


def print_category_top_n(label: str, category_ranked: dict[str, list[dict]], n: int) -> None:
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
    print(f"\n=== {label} Top {min(n, len(ranked))} ===")
    for i, item in enumerate(ranked[:n], start=1):
        rep_title = item["titles"][0] if item["titles"] else "(제목 없음)"
        print(f"{i}. [언급 {item['mention_count']}건] {rep_title}")
        if len(item["titles"]) > 1:
            print(f"   (그룹 내 추가 {len(item['titles']) - 1}건 생략)")
        if item.get("cross_axis_partner"):
            print(f"   🔗 반대 축에서도 다뤄짐: {item['cross_axis_partner']}")