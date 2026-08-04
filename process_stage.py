"""
process_stage.py
Job 체이닝(2-Job 구조) 2단계 - 정규화~배포 전용 진입점.

워크플로(run-pipline.yml)의 "process" Job이 이 스크립트를 실행한다. collect_stage.py가
만든 결과(actions/download-artifact로 내려받은 collect_output/collected.json)를 읽어서
이어받고, main.py의 normalize()/score()/step4_llm_summary() 등을 그대로 재사용해
정규화부터 저장/배포까지 실행한다. (main.py.run() 자체는 안 건드렸음 - 로컬에서 전체
파이프라인을 한 번에 테스트하고 싶을 때는 여전히 `python main.py`로 그대로 쓸 수 있다.)

** 이 Job의 시간예산 **
main.py.run()의 5단계 체크포인트(_CHECKPOINT_*_MIN, GDELT~요약이 355분 하나를 나눠 씀)와는
완전히 다른 예산 체계다 - GDELT는 이제 collect Job에서 따로 6시간을 갖고 끝났으므로, 이
Job은 관련성 필터/카테고리 재분류에만 이 Job 시작 후 4시간(RELEVANCE_TIME_BUDGET_SECONDS)을
주고, 나머지(이슈 그룹핑 1~3차, 4차 재검토, LLM 요약)는 시간예산 없이(무제한) 돈다 - TOP_N이
고정값이라 수집량과 무관하게 규모가 안 커지는 단계들이라 상대적으로 안전하다고 판단한
선택. 관련성 필터/재분류만 수집량(최대 어제 하루치 전체)에 비례해서 배치 수가 늘 수 있어
유일하게 예산을 남겨둠.

무제한 단계들엔 deadline=None이 아니라 float("inf")를 넘긴다 - None을 넘기면 각 함수가
"deadline 못 받았을 때 쓰는 자기 모듈 기본값"(standalone 테스트용 TIME_BUDGET_SECONDS 등,
90분/120분 등으로 사실상 예산이 생겨버림)으로 폴백해버리기 때문. inf를 명시적으로 넘기면
"time.monotonic() >= inf"가 항상 False라 그 폴백 없이 정말 무제한으로 돈다 - 각 모듈
코드는 안 건드리고 이 스크립트에서만 값을 다르게 넘겨서 예산 배분을 조정한 것.

** collect_output/collected.json이 없거나 손상됐을 때 **
collect Job이 실패했거나(process 쪽 워크플로에 if: always()를 걸어둬서 이 Job은 그래도
돌아감) artifact 자체가 안 만들어졌을 수 있다 - 이 경우 빈 articles로 안전하게 시작해서
파이프라인 나머지 단계가 "오늘은 기사가 0건"인 것처럼 정상적으로(요약/저장/배포 각 단계의
기존 안전한 기본값 그대로) 흘러가게 한다.
"""

import json
import os
import time
from datetime import datetime, timezone

import main as main_module
import scorer
import category_aggregator
import keyword_tagger
import llm_summarizer
import relevance_filter
import storage
import deploy

INPUT_PATH = os.path.join("collect_output", "collected.json")

# 관련성 필터 + 카테고리 재분류가 이 Job 시작 후 쓸 수 있는 최대 시간. 이 둘은 같은
# deadline을 공유한다(main.py의 예전 체크포인트 설계에서도 두 단계가 한 구간을 공유했던
# 것과 동일한 방침 - 관련성 필터가 시간을 많이 쓰면 재분류가 실제로 쓸 여유는 자동으로 줄어듦).
RELEVANCE_TIME_BUDGET_SECONDS = 4 * 60 * 60  # 240분(4시간)


def _load_collected() -> tuple[list[dict], dict, list[str], datetime, datetime]:
    """collect_stage.py가 만든 결과를 읽어온다. 없거나 손상됐으면 안전하게 빈 값으로 시작."""
    try:
        with open(INPUT_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        articles = payload["articles"]
        gdelt_timeline = payload.get("gdelt_timeline", {})
        failed_sources = payload.get("failed_sources", [])
        window_start = datetime.fromisoformat(payload["window_start"])
        window_end = datetime.fromisoformat(payload["window_end"])
        print(f"[process_stage] 수집 결과 로드 완료 -> {INPUT_PATH} ({len(articles)}건, "
              f"수집 구간 {window_start.isoformat()} ~ {window_end.isoformat()})")
        return articles, gdelt_timeline, failed_sources, window_start, window_end
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[process_stage] 🔴 조치필요 [PS-01] - collect 결과 로드 실패(collect Job이 "
              f"실패했거나 artifact가 없는 것으로 추정) - 빈 기사로 계속 진행: "
              f"{type(e).__name__} - {e!r}")
        window_start, window_end = main_module._compute_collection_window()
        return [], {}, ["네이버", "GDELT"], window_start, window_end


def run() -> None:
    articles, gdelt_timeline, failed_sources, window_start, window_end = _load_collected()

    process_start = time.monotonic()
    deadline_relevance = process_start + RELEVANCE_TIME_BUDGET_SECONDS
    deadline_grouping = float("inf")
    deadline_stage4 = float("inf")
    deadline_summary = float("inf")

    print("\n=== [2] 정규화 ===")
    try:
        articles = main_module.normalize(articles, window_start, window_end)
        keyword_tagger.tag_articles(articles)
        keyword_tagger.print_category_distribution(articles)
    except Exception as e:
        print(f"[process_stage] 🔴 조치필요 [PS-02] - [2] 정규화/태깅 단계에서 예상 못 한 오류 발생 - "
              f"원본 기사 그대로 다음 단계로 진행: {type(e).__name__} - {e!r}")

    print("\n=== [2.5] 관련성 필터 ===")
    try:
        articles = relevance_filter.filter_articles(articles, deadline=deadline_relevance)
    except Exception as e:
        print(f"[process_stage] 🔴 조치필요 [PS-03] - [2.5] 관련성 필터 단계에서 예상 못 한 오류 발생 - "
              f"필터링 없이 다음 단계로 진행: {type(e).__name__} - {e!r}")

    print("\n=== [2.6] 카테고리 재분류 ===")
    try:
        articles = relevance_filter.recategorize_uncategorized(articles, deadline=deadline_relevance)
    except Exception as e:
        print(f"[process_stage] 🔴 조치필요 [PS-04] - [2.6] 카테고리 재분류 단계에서 예상 못 한 오류 발생 - "
              f"재분류 없이 다음 단계로 진행: {type(e).__name__} - {e!r}")

    print("\n=== [2.1] 이슈 그룹핑 임베딩 모델 로드 ===")
    embedding_model = main_module._load_embedding_model()

    print("\n=== [3] 스코어링 ===")
    try:
        (domestic_ranked, international_ranked,
         domestic_category_ranked, international_category_ranked) = main_module.score(
            articles, embedding_model, top_n=main_module.TOP_N,
            grouping_deadline=deadline_grouping, stage4_deadline=deadline_stage4)
        scorer.print_top_n("국내", domestic_ranked, n=main_module.TOP_N)
        scorer.print_top_n("해외", international_ranked, n=main_module.TOP_N)
        scorer.print_category_top_n("국내", domestic_category_ranked, n=main_module.CATEGORY_TOP_N)
        scorer.print_category_top_n("해외", international_category_ranked, n=main_module.CATEGORY_TOP_N)
    except Exception as e:
        print(f"[process_stage] 🔴 조치필요 [PS-05] - [3] 스코어링 단계에서 예상 못 한 오류 발생 - "
              f"오늘은 Top N 없이 진행(저장 단계에서 raw.json은 그대로 남음): {type(e).__name__} - {e!r}")
        domestic_ranked, international_ranked = [], []
        domestic_category_ranked, international_category_ranked = {}, {}

    print("\n=== [3-보조] 카테고리 전체 집계 ===")
    try:
        category_distribution = category_aggregator.aggregate(articles)
        category_comparison = category_aggregator.compare_with_history(category_distribution)
        category_aggregator.print_aggregate_with_history(category_distribution, category_comparison)
    except Exception as e:
        print(f"[process_stage] 🔴 조치필요 [PS-06] - [3-보조] 카테고리 집계 단계에서 예상 못 한 오류 발생 - "
              f"오늘은 집계 없이 진행: {type(e).__name__} - {e!r}")
        category_distribution, category_comparison = {}, None

    print("\n=== [4] 국내/해외 LLM 요약 생성 ===")
    try:
        domestic_summarized, international_summarized = main_module.step4_llm_summary(
            domestic_ranked, international_ranked, deadline=deadline_summary)
        llm_summarizer.print_summaries("국내", domestic_summarized)
        llm_summarizer.print_summaries("해외", international_summarized)
    except Exception as e:
        print(f"[process_stage] 🔴 조치필요 [PS-07] - [4] LLM 요약 단계에서 예상 못 한 오류 발생 - "
              f"요약 없이(원문 제목만) 진행: {type(e).__name__} - {e!r}")
        domestic_summarized, international_summarized = domestic_ranked, international_ranked

    print("\n=== [4-보조] 카테고리별 LLM 요약 생성 ===")
    try:
        domestic_category_summarized, international_category_summarized = main_module.step4_category_llm_summary(
            domestic_category_ranked, international_category_ranked, deadline=deadline_summary)
        for category, items in domestic_category_summarized.items():
            llm_summarizer.print_summaries(f"국내-{category}", items)
        for category, items in international_category_summarized.items():
            llm_summarizer.print_summaries(f"해외-{category}", items)
    except Exception as e:
        print(f"[process_stage] 🔴 조치필요 [PS-08] - [4-보조] 카테고리별 LLM 요약 단계에서 예상 못 한 오류 발생 - "
              f"요약 없이(원문 제목만) 진행: {type(e).__name__} - {e!r}")
        domestic_category_summarized, international_category_summarized = (
            domestic_category_ranked, international_category_ranked)

    print("\n=== [5] 저장 ===")
    # 수동 실행(workflow_dispatch)은 테스트/임시 확인용이라 저장하면 안 됨 - "전일/7일 평균 대비 증감"
    # 비교 기준이 테스트 데이터로 오염될 수 있음. GITHUB_EVENT_NAME은 GitHub Actions 자동 환경변수
    # (로컬 등 없는 환경에선 "수동 아님"으로 처리)
    is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if is_manual_run:
        print("[process_stage] 수동 실행(workflow_dispatch)이라 저장을 건너뜁니다 - "
              "'전일/7일 평균 대비 증감' 비교 기준 오염 방지(콘솔에 출력된 이번 실행 결과는 그대로 확인 가능, data/에는 안 남음)")
        saved_dir = None
    else:
        try:
            saved_dir = storage.save_day(articles, domestic_summarized, international_summarized,
                                          domestic_category_summarized, international_category_summarized,
                                          gdelt_timeline, failed_sources, category_distribution,
                                          category_comparison)
        except Exception as e:
            print(f"[process_stage] 🔴 조치필요 [PS-09] - 저장 단계에서 예상 못 한 오류 발생(콘솔 로그의 결과는 그대로 유효함): "
                  f"{type(e).__name__} - {e!r}")
            saved_dir = None

    print("\n=== [6] 배포 ===")
    try:
        day_label = os.path.basename(saved_dir) if saved_dir else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        deploy.send_daily_email(day_label, domestic_summarized, international_summarized,
                                 domestic_category_summarized, international_category_summarized,
                                 failed_sources, category_comparison)
    except Exception as e:
        print(f"[process_stage] 🔴 조치필요 [PS-10] - 배포 단계에서 예상 못 한 오류 발생(콘솔 로그의 결과는 그대로 유효함): "
              f"{type(e).__name__} - {e!r}")

    if failed_sources:
        saved_dir_note = f"{saved_dir}/scored.json에도" if saved_dir else "(data/ 파일에는 안 남았지만)"
        print(f"\n[process_stage] 🔴 조치필요 [PS-11] - 이번 실행 실패 소스: {failed_sources} "
              f"({saved_dir_note} failed_sources로 같이 저장됨)")


if __name__ == "__main__":
    run()