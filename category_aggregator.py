"""category_aggregator.py - 카테고리 단위 거친 일간 개요 집계(랭킹용 아님). 국내/해외 별도 집계."""

import json
import os
from collections import Counter

import keyword_tagger
import scorer
import storage


def _category_order() -> list[str]:
    """출력 순서 고정용 - keyword_tagger.CATEGORY_KEYWORDS를 매 호출마다 새로 읽는다(시트 갱신 반영)."""
    return list(keyword_tagger.CATEGORY_KEYWORDS.keys()) + ["기타"]


def count_by_category(articles: list[dict]) -> Counter:
    """기사의 category 필드 집계."""
    return Counter(a.get("category", "기타") for a in articles)


def aggregate(articles: list[dict]) -> dict[str, Counter]:
    """국내/해외 축으로 나눠 카테고리별 건수 집계."""
    domestic, international = scorer.split_domestic_international(articles)
    return {
        "국내": count_by_category(domestic),
        "해외": count_by_category(international),
    }


def print_aggregate(aggregated: dict[str, Counter]) -> None:
    for axis in ("국내", "해외"):
        counter = aggregated.get(axis, Counter())
        total = sum(counter.values())
        print(f"\n=== 카테고리 집계 - {axis} (전체 {total}건) ===")
        if total == 0:
            print("  (기사 없음)")
            continue
        for category in _category_order():
            count = counter.get(category, 0)
            if count == 0:
                continue
            pct = count / total * 100
            print(f"  {category:15s} {count:4d}건 ({pct:.1f}%)")


def compare_with_previous_day(current: dict[str, Counter], base_dir: str = "data",
                               reference=None) -> dict[str, dict[str, dict]] | None:
    """오늘 집계를 전일 scored.json과 비교. 전일 파일 없음/손상 시 None."""
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
        print(f"[category_aggregator] 🟡 주의 [CA-01] - 전일 데이터 없음/읽기 실패({path}): {type(e).__name__} - {e!r}")
        return None
    except (ValueError, AttributeError, TypeError, KeyError) as e:
        print(f"[category_aggregator] 🟡 주의 [CA-02] - 전일 데이터 구조 이상({path}): {type(e).__name__} - {e!r}")
        return None


def compare_with_7day_average(current: dict[str, Counter], base_dir: str = "data",
                               reference=None) -> dict[str, dict[str, dict]] | None:
    """오늘 집계를 최근 7일(오늘 제외) 평균과 비교. 하루도 못 찾으면 None."""
    day_paths = [os.path.join(d, "scored.json") for d in storage.previous_n_days_dirs(7, base_dir, reference)]
    daily_distributions: list[dict] = []
    for path in day_paths:
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            distribution = payload.get("category_distribution")
            if isinstance(distribution, dict):
                daily_distributions.append(distribution)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue

    if not daily_distributions:
        print("[category_aggregator] 🟡 주의 [CA-03] - 최근 7일 이내 비교 가능한 데이터 없음")
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
    """전일 대비 + 7일 평균 대비를 합쳐 반환. 둘 다 없으면 None, 한쪽만 있으면 없는 쪽은 None 필드."""
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
    for axis in ("국내", "해외"):
        counter = aggregated.get(axis, Counter())
        total = sum(counter.values())
        suffix = "" if comparison is None else ", 전일/7일 평균 대비"
        print(f"\n=== 카테고리 집계 - {axis} (전체 {total}건{suffix}) ===")
        if total == 0:
            print("  (기사 없음)")
            continue
        axis_comparison = None if comparison is None else comparison.get(axis, {})
        for category in _category_order():
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