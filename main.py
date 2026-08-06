"""
main.py - AI·IT 뉴스 큐레이션 오케스트레이션.
파이프라인: 1)수집 2)정규화+그룹핑+관련성필터+카테고리재분류 3)스코어링 4)LLM요약 5)저장 6)배포.
그룹핑을 관련성 필터보다 먼저 실행 - 필터/재분류는 각 그룹 대표 기사 1건만 LLM에 물어 그룹 전체에 적용.
"""

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import gdelt_collector
import naver_collector
import scorer
import issue_grouper
import llm_summarizer
import keyword_tagger
import category_aggregator
import category_chart
import relevance_filter
import storage
import deploy
import error_log
import llm_rate_limiter

_KST = timezone(timedelta(hours=9))


def _compute_collection_window(reference: datetime | None = None) -> tuple[datetime, datetime]:
    """이번 실행이 다룰 "어제 00:00 KST ~ 오늘 00:00 KST" 절대 구간(UTC)."""
    now_kst = (reference or datetime.now(timezone.utc)).astimezone(_KST)
    today_midnight_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = today_midnight_kst
    window_start = window_end - timedelta(days=1)
    return window_start.astimezone(timezone.utc), window_end.astimezone(timezone.utc)


TOP_N = int(os.environ.get("TOP_N") or 5)
CATEGORY_TOP_N = int(os.environ.get("CATEGORY_TOP_N") or 1)

# 관련성 필터/카테고리 재분류만 시간예산을 둠(그룹 수는 수집량에 비례해서 늘 수 있음).
# 그룹핑/4차 재검토/요약은 float("inf")로 무제한(TOP_N 고정값이라 규모가 안 커짐).
RELEVANCE_TIME_BUDGET_SECONDS = 4 * 60 * 60
CATEGORY_RECLASSIFY_TIME_BUDGET_SECONDS = 5 * 60 * 60


def run_collectors(window_start: datetime, window_end: datetime,
                    deadline: float | None = None) -> tuple[list[dict], dict, list[str]]:
    """naver/gdelt collector 순차 실행. 하나가 실패해도 나머지는 계속."""
    all_articles: list[dict] = []
    gdelt_timeline: dict = {}
    failed_sources: list[str] = []

    try:
        naver_articles = naver_collector.collect(window_start, window_end)
        all_articles.extend(naver_articles)
        print(f"[main] 네이버 수집 완료 - {len(naver_articles)}건")
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-02] - 네이버 수집 실패: {type(e).__name__} - {e!r}")
        failed_sources.append("네이버")

    try:
        gdelt_articles, gdelt_timeline = gdelt_collector.collect(
            deadline=deadline, window_start=window_start, window_end=window_end)
        all_articles.extend(gdelt_articles)
        print(f"[main] GDELT 수집 완료 - {len(gdelt_articles)}건")
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-03] - GDELT 수집 실패: {type(e).__name__} - {e!r}")
        failed_sources.append("GDELT")

    return all_articles, gdelt_timeline, failed_sources


def normalize(articles: list[dict], window_start: datetime, window_end: datetime) -> list[dict]:
    """URL 중복 제거 + 수집 구간 밖 기사 제외(collector가 이미 걸러서 정상 흐름에선 0건이어야 함)."""
    seen_urls: set[str] = set()
    deduped = []
    for article in articles:
        url = article.get("url")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        deduped.append(article)

    removed = len(articles) - len(deduped)
    if removed:
        print(f"[main] 완전 동일 기사(URL 중복) {removed}건 제거")

    fresh = []
    out_of_window_count = 0
    for article in deduped:
        try:
            if not scorer.is_in_window(article["published_at"], window_start, window_end):
                out_of_window_count += 1
                continue
        except (KeyError, ValueError, TypeError):
            pass
        fresh.append(article)

    if out_of_window_count:
        print(f"[main] 🟡 주의 - 수집 구간 밖 기사 {out_of_window_count}건 제외(정상 흐름에선 0건이어야 함)")

    return fresh


def _collect_shown_titles(ranked: list[dict], category_ranked: dict[str, list[dict]]) -> set[str]:
    """축 하나(국내/해외)에서 이메일/summary.md에 실제 노출되는 모든 제목."""
    titles: set[str] = set()
    for item in ranked:
        titles.update(item.get("titles", []))
    for items in category_ranked.values():
        for item in items:
            titles.update(item.get("titles", []))
    return titles


def _scrub_unshown_cross_axis_partner(items: list[dict], other_axis_shown_titles: set[str]) -> None:
    """반대 축에 실제로 없는 cross_axis_partner는 지운다(이메일에 없는 제목을 가리키지 않게)."""
    for item in items:
        partner = item.get("cross_axis_partner")
        if partner and partner not in other_axis_shown_titles:
            item["cross_axis_partner"] = None


def score(groups: list[list[dict]], top_n: int = TOP_N,
          stage4_deadline: float | None = None) -> tuple[list[dict], list[dict], dict, dict]:
    """이미 그룹핑된 groups를 국내/해외로 나눠 Top N + 카테고리별 Top N 계산."""
    domestic_groups = []
    international_groups = []
    for group in groups:
        domestic_part = [
            a for a in group
            if a.get("source") == "네이버"
            or (a.get("source") == "GDELT" and scorer._is_korean_gdelt_article(a))
        ]
        international_part = [
            a for a in group
            if a.get("source") != "네이버"
            and not (a.get("source") == "GDELT" and scorer._is_korean_gdelt_article(a))
        ]
        if domestic_part and international_part:
            domestic_part[0]["_cross_axis_partner"] = international_part[0].get("title", "")
            international_part[0]["_cross_axis_partner"] = domestic_part[0].get("title", "")
        if domestic_part:
            domestic_groups.append(domestic_part)
        if international_part:
            international_groups.append(international_part)

    domestic_ranked_pool = scorer.score_and_rank(domestic_groups, top_n=None)
    international_ranked_pool = scorer.score_and_rank(international_groups, top_n=None)

    domestic_ranked = issue_grouper.stage4_dedupe_and_promote(
        domestic_ranked_pool, top_n=top_n, label="국내", deadline=stage4_deadline)
    international_ranked = issue_grouper.stage4_dedupe_and_promote(
        international_ranked_pool, top_n=top_n, label="해외", deadline=stage4_deadline)

    def _category_dedupe_fn(axis_label: str):
        def _fn(ranked_pool, n, category):
            return issue_grouper.stage4_dedupe_and_promote(
                ranked_pool, top_n=n, label=f"{axis_label}-{category}", deadline=stage4_deadline)
        return _fn

    domestic_category_ranked = scorer.score_by_category(
        domestic_groups, CATEGORY_TOP_N, dedupe_fn=_category_dedupe_fn("국내"))
    international_category_ranked = scorer.score_by_category(
        international_groups, CATEGORY_TOP_N, dedupe_fn=_category_dedupe_fn("해외"))

    domestic_shown_titles = _collect_shown_titles(domestic_ranked, domestic_category_ranked)
    international_shown_titles = _collect_shown_titles(international_ranked, international_category_ranked)

    _scrub_unshown_cross_axis_partner(domestic_ranked, international_shown_titles)
    _scrub_unshown_cross_axis_partner(international_ranked, domestic_shown_titles)
    for items in domestic_category_ranked.values():
        _scrub_unshown_cross_axis_partner(items, international_shown_titles)
    for items in international_category_ranked.values():
        _scrub_unshown_cross_axis_partner(items, domestic_shown_titles)

    return domestic_ranked, international_ranked, domestic_category_ranked, international_category_ranked


def _load_embedding_model():
    """BGE-M3 임베딩 모델을 1회 로드. 실패 시 None(1차 사전 매칭만으로 fallback)."""
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-m3")
        print("[main] BGE-M3 임베딩 모델 로드 완료")
        return model
    except Exception as e:
        print(f"[main] 🟡 주의 [MN-04] - BGE-M3 모델 로드 실패 - 1차 사전 매칭만 적용: {type(e).__name__} - {e!r}")
        return None


def _regroup_by_category(items: list[dict]) -> dict[str, list[dict]]:
    """item["category"] 기준으로 평평한 리스트를 {카테고리: [항목]}로 재구성."""
    regrouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        regrouped[item.get("category", "미상")].append(item)
    return dict(regrouped)


def step4_category_llm_summary(domestic_category_ranked: dict[str, list[dict]],
                                international_category_ranked: dict[str, list[dict]],
                                deadline: float | None = None,
                                ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """카테고리별 Top N 결과에 LLM 요약 적용."""
    domestic_flat = [item for items in domestic_category_ranked.values() for item in items]
    international_flat = [item for items in international_category_ranked.values() for item in items]

    domestic_summarized_flat = llm_summarizer.summarize_top_issues(domestic_flat, label="국내-카테고리", deadline=deadline)
    international_summarized_flat = llm_summarizer.summarize_top_issues(international_flat, label="해외-카테고리", deadline=deadline)

    return (_regroup_by_category(domestic_summarized_flat),
            _regroup_by_category(international_summarized_flat))


def step4_llm_summary(domestic_ranked: list[dict],
                       international_ranked: list[dict],
                       deadline: float | None = None) -> tuple[list[dict], list[dict]]:
    """국내/해외 Top N에 LLM 요약 적용."""
    domestic_summarized = llm_summarizer.summarize_top_issues(domestic_ranked, label="국내", deadline=deadline)
    international_summarized = llm_summarizer.summarize_top_issues(international_ranked, label="해외", deadline=deadline)
    return domestic_summarized, international_summarized


def run_process(articles: list[dict], gdelt_timeline: dict, failed_sources: list[str],
                 window_start: datetime, window_end: datetime,
                 process_start: float | None = None,
                 prior_error_codes: list[str] | None = None) -> None:
    """[2] 정규화 ~ [6] 배포 전체. run()과 process_stage.py가 공유하는 본체."""
    if process_start is None:
        process_start = time.monotonic()

    deadline_relevance = process_start + RELEVANCE_TIME_BUDGET_SECONDS
    deadline_category = process_start + CATEGORY_RECLASSIFY_TIME_BUDGET_SECONDS
    deadline_grouping = float("inf")
    deadline_stage4 = float("inf")
    deadline_summary = float("inf")

    error_log.start_capture()

    if not llm_rate_limiter.LLM_ENABLED:
        print("[main] 🟡 주의 - LLM_ENABLED=off - 이번 실행은 모든 LLM 호출(관련성필터/카테고리재분류/그룹핑3차/4차재검토/요약)을 스킵합니다")

    print("\n=== [2] 정규화 ===")
    try:
        articles = normalize(articles, window_start, window_end)
        keyword_tagger.tag_articles(articles)
        keyword_tagger.print_category_distribution(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-05] - [2] 정규화/태깅 단계 오류 - 원본 그대로 진행: {type(e).__name__} - {e!r}")

    print("\n=== [2.1] 이슈 그룹핑 임베딩 모델 로드 ===")
    embedding_model = _load_embedding_model()

    print("\n=== [2.2] 이슈 그룹핑 ===")
    try:
        groups = issue_grouper.group_issues(articles, model=embedding_model, deadline=deadline_grouping)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-06] - [2.2] 이슈 그룹핑 오류 - 그룹핑 없이 진행: {type(e).__name__} - {e!r}")
        groups = [[a] for a in articles]

    print("\n=== [2.5] 관련성 필터 (그룹 대표 1건씩 판단) ===")
    try:
        groups = relevance_filter.filter_groups(groups, deadline=deadline_relevance)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-07] - [2.5] 관련성 필터 오류 - 필터링 없이 진행: {type(e).__name__} - {e!r}")

    print("\n=== [2.6] 카테고리 재분류 (그룹 대표 1건씩 판단) ===")
    try:
        groups = relevance_filter.recategorize_uncategorized_groups(groups, deadline=deadline_category)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-08] - [2.6] 카테고리 재분류 오류 - 재분류 없이 진행: {type(e).__name__} - {e!r}")

    articles = [a for g in groups for a in g]

    print("\n=== [3] 스코어링 ===")
    try:
        (domestic_ranked, international_ranked,
         domestic_category_ranked, international_category_ranked) = score(
            groups, top_n=TOP_N, stage4_deadline=deadline_stage4)
        scorer.print_top_n("국내", domestic_ranked, n=TOP_N)
        scorer.print_top_n("해외", international_ranked, n=TOP_N)
        scorer.print_category_top_n("국내", domestic_category_ranked, n=CATEGORY_TOP_N)
        scorer.print_category_top_n("해외", international_category_ranked, n=CATEGORY_TOP_N)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-09] - [3] 스코어링 오류 - Top N 없이 진행: {type(e).__name__} - {e!r}")
        domestic_ranked, international_ranked = [], []
        domestic_category_ranked, international_category_ranked = {}, {}

    print("\n=== [3-보조] 카테고리 전체 집계 ===")
    try:
        category_distribution = category_aggregator.aggregate(articles)
        category_comparison = category_aggregator.compare_with_history(category_distribution)
        category_aggregator.print_aggregate_with_history(category_distribution, category_comparison)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-10] - [3-보조] 카테고리 집계 오류 - 집계 없이 진행: {type(e).__name__} - {e!r}")
        category_distribution, category_comparison = {}, None

    try:
        category_charts = category_chart.generate_charts(category_distribution)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-11] - 카테고리 추이 그래프 오류 - 그래프 없이 진행: {type(e).__name__} - {e!r}")
        category_charts = {}

    print("\n=== [4] 국내/해외 LLM 요약 생성 ===")
    try:
        domestic_summarized, international_summarized = step4_llm_summary(
            domestic_ranked, international_ranked, deadline=deadline_summary)
        llm_summarizer.print_summaries("국내", domestic_summarized)
        llm_summarizer.print_summaries("해외", international_summarized)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-12] - [4] LLM 요약 오류 - 요약 없이 진행: {type(e).__name__} - {e!r}")
        domestic_summarized, international_summarized = domestic_ranked, international_ranked

    print("\n=== [4-보조] 카테고리별 LLM 요약 생성 ===")
    try:
        domestic_category_summarized, international_category_summarized = step4_category_llm_summary(
            domestic_category_ranked, international_category_ranked, deadline=deadline_summary)
        for category, items in domestic_category_summarized.items():
            llm_summarizer.print_summaries(f"국내-{category}", items)
        for category, items in international_category_summarized.items():
            llm_summarizer.print_summaries(f"해외-{category}", items)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-13] - [4-보조] 카테고리별 LLM 요약 오류 - 요약 없이 진행: {type(e).__name__} - {e!r}")
        domestic_category_summarized, international_category_summarized = (
            domestic_category_ranked, international_category_ranked)

    print("\n=== [5] 저장 ===")
    # 수동 실행(workflow_dispatch)은 테스트용이라 저장 안 함 - 전일/7일 평균 비교 기준 오염 방지
    is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if is_manual_run:
        print("[main] 수동 실행이라 저장을 건너뜁니다")
        saved_dir = None
    else:
        try:
            saved_dir = storage.save_day(articles, domestic_summarized, international_summarized,
                                          domestic_category_summarized, international_category_summarized,
                                          gdelt_timeline, failed_sources, category_distribution,
                                          category_comparison)
        except Exception as e:
            print(f"[main] 🔴 조치필요 [MN-14] - 저장 단계 오류: {type(e).__name__} - {e!r}")
            saved_dir = None

    this_stage_codes = error_log.stop_capture()
    all_error_codes = error_log.merge_unique(prior_error_codes or [], this_stage_codes)

    print("\n=== [6] 배포 ===")
    try:
        day_label = os.path.basename(saved_dir) if saved_dir else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        deploy.send_daily_email(day_label, domestic_summarized, international_summarized,
                                 domestic_category_summarized, international_category_summarized,
                                 failed_sources, category_comparison, all_error_codes, category_charts)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-15] - 배포 단계 오류: {type(e).__name__} - {e!r}")

    if failed_sources:
        print(f"\n[main] 🔴 조치필요 [MN-16] - 이번 실행 실패 소스: {failed_sources}")


def run() -> None:
    """단일 프로세스로 전체 파이프라인을 한 번에 실행 - 로컬 테스트용(실제 운영은 Job 체이닝)."""
    window_start, window_end = _compute_collection_window()
    print(f"[main] 이번 실행 수집 구간: {window_start.isoformat()} ~ {window_end.isoformat()} (UTC)")

    pipeline_start = time.monotonic()

    print("=== [1] 수집 시작 ===")
    error_log.start_capture()
    articles, gdelt_timeline, failed_sources = run_collectors(window_start, window_end)
    collect_error_codes = error_log.stop_capture()

    run_process(articles, gdelt_timeline, failed_sources, window_start, window_end,
                process_start=pipeline_start, prior_error_codes=collect_error_codes)


if __name__ == "__main__":
    run()