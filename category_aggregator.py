"""
category_aggregator.py
카테고리 전체 집계 (issue_grouper의 이슈 단위 그룹핑과 별개 - 카테고리 단위 거친 주간 개요).
scorer/keyword_score와도 독립적인 보조 지표. 랭킹용 아님.

- 집계 방식: 단순 건수 (recency_weight 미적용) - 수집이 이미 최근 7일 제한이라 가중치 변별력 낮음, 개요용이라 단순한 쪽이 더 투명함
- 집계 범위: 국내/해외 각각 별도 (scorer.split_domestic_international 재사용, "종합 랭킹 없음" 원칙 유지)
"""

import json
import os
from collections import Counter

from keyword_tagger import CATEGORY_KEYWORDS
import scorer
import storage


# 출력 순서 고정용 (실행마다 안 흔들리게)
_CATEGORY_ORDER = list(CATEGORY_KEYWORDS.keys()) + ["기타"]


def count_by_category(articles: list[dict]) -> Counter:
    """기사 리스트의 category 필드(keyword_tagger가 이미 채워둔 값)를 집계. 태깅은 하지 않음."""
    return Counter(a.get("category", "기타") for a in articles)


def aggregate(articles: list[dict]) -> dict[str, Counter]:
    """국내/해외 축으로 나눠 카테고리별 건수 집계. 반환: {"국내": Counter, "해외": Counter}"""
    domestic, international = scorer.split_domestic_international(articles)
    return {
        "국내": count_by_category(domestic),
        "해외": count_by_category(international),
    }


def print_aggregate(aggregated: dict[str, Counter]) -> None:
    """국내/해외 카테고리 집계를 표 형태로 출력."""
    for axis in ("국내", "해외"):
        counter = aggregated.get(axis, Counter())
        total = sum(counter.values())
        print(f"\n=== 카테고리 집계 - {axis} (전체 {total}건) ===")
        if total == 0:
            print("  (기사 없음)")
            continue
        for category in _CATEGORY_ORDER:
            count = counter.get(category, 0)
            if count == 0:
                continue
            pct = count / total * 100
            print(f"  {category:15s} {count:4d}건 ({pct:.1f}%)")


# ---------------------------------------------------------------------------
# 지난주 대비 증감
# ---------------------------------------------------------------------------

def compare_with_last_week(current: dict[str, Counter], base_dir: str = "data",
                            reference=None) -> dict[str, dict[str, dict]] | None:
    """
    이번 주 집계를 지난주 scored.json의 category_distribution과 비교.
    지난주 파일 없음/손상 시 None 반환 (첫 실행 등 정상 케이스).

    반환: {"국내": {카테고리: {"this_week", "last_week", "delta"}, ...}, "해외": {...}}
    (두 주 중 한쪽에만 있는 카테고리도 포함, 없는 쪽은 0)
    """
    path = os.path.join(storage.previous_week_dir(base_dir, reference), "scored.json")
    try:
        with open(path, encoding="utf-8") as f:
            last_week_payload = json.load(f)

        last_week_distribution = last_week_payload.get("category_distribution")
        if not isinstance(last_week_distribution, dict):
            raise ValueError(f"category_distribution이 dict가 아님(타입: {type(last_week_distribution).__name__})")

        comparison = {}
        for axis in ("국내", "해외"):
            this_counter = current.get(axis, Counter())
            last_counter = last_week_distribution.get(axis)
            if not isinstance(last_counter, dict):
                last_counter = {}
            categories = set(this_counter) | set(last_counter)
            axis_result = {}
            for category in categories:
                this_count = this_counter.get(category, 0)
                last_count = last_counter.get(category, 0)
                axis_result[category] = {
                    "this_week": this_count,
                    "last_week": last_count,
                    "delta": this_count - last_count,
                }
            comparison[axis] = axis_result

        return comparison

    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"[category_aggregator] 🟡 주의 [CA-01] - 지난주 데이터 없음/읽기 실패({path}) - "
              f"증감 비교 생략: {type(e).__name__} - {e!r}")
        return None
    except (ValueError, AttributeError, TypeError, KeyError) as e:
        print(f"[category_aggregator] 🟡 주의 [CA-02] - 지난주 데이터 구조 이상({path}) - "
              f"증감 비교 생략: {type(e).__name__} - {e!r}")
        return None


def print_aggregate_with_comparison(aggregated: dict[str, Counter],
                                     comparison: dict[str, dict[str, dict]] | None) -> None:
    """print_aggregate()와 동일하되, comparison이 있으면 각 줄에 지난주 대비 증감을 붙인다."""
    for axis in ("국내", "해외"):
        counter = aggregated.get(axis, Counter())
        total = sum(counter.values())
        suffix = "" if comparison is None else ", 지난주 대비"
        print(f"\n=== 카테고리 집계 - {axis} (전체 {total}건{suffix}) ===")
        if total == 0:
            print("  (기사 없음)")
            continue
        axis_comparison = None if comparison is None else comparison.get(axis, {})
        for category in _CATEGORY_ORDER:
            count = counter.get(category, 0)
            if count == 0:
                continue
            pct = count / total * 100
            line = f"  {category:15s} {count:4d}건 ({pct:.1f}%)"
            if axis_comparison is not None and category in axis_comparison:
                delta = axis_comparison[category]["delta"]
                last_count = axis_comparison[category]["last_week"]
                sign = "+" if delta >= 0 else ""
                line += f" [지난주 {last_count}건, {sign}{delta}]"
            print(line)