"""
category_aggregator.py
카테고리 전체 집계 (issue_grouper의 이슈 단위 그룹핑과 별개 - 카테고리 단위 거친 일간 개요).
scorer/keyword_score와도 독립적인 보조 지표. 랭킹용 아님.

- 집계 방식: 단순 건수 (recency_weight 미적용) - 수집이 이미 최근 1일 제한이라 가중치 변별력 낮음, 개요용이라 단순한 쪽이 더 투명함
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
# 전일 대비 + 7일 평균 대비 증감
# ---------------------------------------------------------------------------
#
# 일간 전환 전에는 "지난주 대비 증감" 하나만 있었음. 매일 실행으로 바뀌면서 요일별
# 뉴스량 편차(평일 vs 주말) 때문에 "전일 대비" 하나만 보면 노이즈가 커질 수 있어서,
# 추세를 더 안정적으로 보여주는 "최근 7일 평균 대비"를 같이 계산해 함께 보여준다.

def compare_with_previous_day(current: dict[str, Counter], base_dir: str = "data",
                               reference=None) -> dict[str, dict[str, dict]] | None:
    """
    오늘 집계를 전일 scored.json의 category_distribution과 비교.
    전일 파일 없음/손상 시 None 반환 (일간 전환 직후 등 정상 케이스).

    반환: {"국내": {카테고리: {"today", "yesterday", "delta"}, ...}, "해외": {...}}
    (두 날 중 한쪽에만 있는 카테고리도 포함, 없는 쪽은 0)
    """
    path = os.path.join(storage.previous_day_dir(base_dir, reference), "scored.json")
    try:
        with open(path, encoding="utf-8") as f:
            yesterday_payload = json.load(f)

        yesterday_distribution = yesterday_payload.get("category_distribution")
        if not isinstance(yesterday_distribution, dict):
            raise ValueError(f"category_distribution이 dict가 아님(타입: {type(yesterday_distribution).__name__})")

        comparison = {}
        for axis in ("국내", "해외"):
            this_counter = current.get(axis, Counter())
            yesterday_counter = yesterday_distribution.get(axis)
            if not isinstance(yesterday_counter, dict):
                yesterday_counter = {}
            categories = set(this_counter) | set(yesterday_counter)
            axis_result = {}
            for category in categories:
                today_count = this_counter.get(category, 0)
                yesterday_count = yesterday_counter.get(category, 0)
                axis_result[category] = {
                    "today": today_count,
                    "yesterday": yesterday_count,
                    "delta": today_count - yesterday_count,
                }
            comparison[axis] = axis_result

        return comparison

    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"[category_aggregator] 🟡 주의 [CA-01] - 전일 데이터 없음/읽기 실패({path}) - "
              f"전일 대비 비교 생략: {type(e).__name__} - {e!r}")
        return None
    except (ValueError, AttributeError, TypeError, KeyError) as e:
        print(f"[category_aggregator] 🟡 주의 [CA-02] - 전일 데이터 구조 이상({path}) - "
              f"전일 대비 비교 생략: {type(e).__name__} - {e!r}")
        return None


def compare_with_7day_average(current: dict[str, Counter], base_dir: str = "data",
                               reference=None) -> dict[str, dict[str, dict]] | None:
    """
    오늘 집계를 최근 7일(오늘 제외) scored.json의 category_distribution 평균과 비교.
    하루도 못 찾으면(일간 전환 직후 등) None 반환. 찾은 날짜 수만큼만 평균을 낸다
    (예: 아직 3일치만 쌓였으면 3일 평균) - 각 날짜에 없는 카테고리는 그 날짜 0건으로 취급
    (compare_with_previous_day와 동일한 0-채우기 방침).

    반환: {"국내": {카테고리: {"today", "avg_7day", "delta"}, ...}, "해외": {...}}
    avg_7day/delta는 소수점 1자리로 반올림(정수 건수의 평균이라 정수로 안 떨어질 수 있음).
    """
    day_paths = [os.path.join(d, "scored.json") for d in storage.previous_n_days_dirs(7, base_dir, reference)]
    daily_distributions: list[dict] = []
    for path in day_paths:
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            distribution = payload.get("category_distribution")
            if isinstance(distribution, dict):
                daily_distributions.append(distribution)
            # 구조 이상이면 이 날짜만 조용히 스킵 - 나머지 날짜로 평균 계속(개별 로그는 안 남김,
            # compare_with_previous_day와 달리 "여러 날짜 중 하나"라 매일 로그가 쌓이면 소음이 큼)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue  # 이 날짜 파일 없음/읽기 실패 - 나머지 날짜로 평균 계속

    if not daily_distributions:
        print("[category_aggregator] 🟡 주의 [CA-03] - 최근 7일 이내 비교 가능한 데이터 없음 - "
              "7일 평균 대비 비교 생략")
        return None

    n = len(daily_distributions)
    comparison = {}
    for axis in ("국내", "해외"):
        this_counter = current.get(axis, Counter())
        axis_daily_counters = [
            d.get(axis) if isinstance(d.get(axis), dict) else {}
            for d in daily_distributions
        ]

        categories = set(this_counter)
        for counter in axis_daily_counters:
            categories |= set(counter)

        axis_result = {}
        for category in categories:
            today_count = this_counter.get(category, 0)
            avg = sum(counter.get(category, 0) for counter in axis_daily_counters) / n
            axis_result[category] = {
                "today": today_count,
                "avg_7day": round(avg, 1),
                "delta": round(today_count - avg, 1),
            }
        comparison[axis] = axis_result

    print(f"[category_aggregator] 7일 평균 대비 비교 - 최근 {n}일치 데이터로 평균 계산")
    return comparison


def compare_with_history(current: dict[str, Counter], base_dir: str = "data",
                          reference=None) -> dict[str, dict[str, dict]] | None:
    """
    compare_with_previous_day + compare_with_7day_average를 하나로 합쳐서 반환.
    main.py/storage.py/deploy.py는 이 함수 하나만 부르면 됨. 둘 다 비교 재료가 전혀 없으면
    (일간 전환 첫날 등) None. 한쪽만 있으면(예: 이틀째라 전일 대비는 있지만 7일 평균 재료는
    하루치뿐 - 이 경우도 평균 자체는 나옴, 정말 하나도 없는 경우는 전환 첫날뿐) 있는 쪽만 채우고
    없는 쪽 필드는 카테고리별로 None을 넣어 storage.py/deploy.py가 "데이터 없음"으로 표시하게 한다.

    반환: {"국내": {카테고리: {"today", "yesterday", "delta_yesterday", "avg_7day", "delta_avg7day"}}, "해외": {...}}
    """
    vs_yesterday = compare_with_previous_day(current, base_dir, reference)
    vs_avg7 = compare_with_7day_average(current, base_dir, reference)
    if vs_yesterday is None and vs_avg7 is None:
        return None

    merged: dict[str, dict[str, dict]] = {}
    for axis in ("국내", "해외"):
        y_axis = (vs_yesterday or {}).get(axis, {})
        a_axis = (vs_avg7 or {}).get(axis, {})
        categories = set(current.get(axis, Counter())) | set(y_axis) | set(a_axis)
        axis_result = {}
        for category in categories:
            today_count = current.get(axis, Counter()).get(category, 0)
            y = y_axis.get(category)
            a = a_axis.get(category)
            axis_result[category] = {
                "today": today_count,
                "yesterday": y["yesterday"] if y else None,
                "delta_yesterday": y["delta"] if y else None,
                "avg_7day": a["avg_7day"] if a else None,
                "delta_avg7day": a["delta"] if a else None,
            }
        merged[axis] = axis_result
    return merged


def print_aggregate_with_history(aggregated: dict[str, Counter],
                                  comparison: dict[str, dict[str, dict]] | None) -> None:
    """print_aggregate()와 동일하되, comparison이 있으면 각 줄에 전일/7일 평균 대비 증감을 붙인다."""
    for axis in ("국내", "해외"):
        counter = aggregated.get(axis, Counter())
        total = sum(counter.values())
        suffix = "" if comparison is None else ", 전일/7일 평균 대비"
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
                values = axis_comparison[category]
                if values["yesterday"] is not None:
                    dy = values["delta_yesterday"]
                    sign_y = "+" if dy >= 0 else ""
                    line += f" [전일 {values['yesterday']}건, {sign_y}{dy}]"
                if values["avg_7day"] is not None:
                    da = values["delta_avg7day"]
                    sign_a = "+" if da >= 0 else ""
                    line += f" [7일평균 {values['avg_7day']}건, {sign_a}{da}]"
            print(line)