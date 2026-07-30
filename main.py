"""
main.py
AI·IT 뉴스 큐레이션 시스템의 오케스트레이션 레이어.

6단계 파이프라인:
  1) 수집        -> naver/gdelt collector 순차 실행
  2) 정규화      -> 공통 스키마 통합 + URL 중복 제거 + 키워드 태깅(keyword_tagger) +
                    관련성 필터(relevance_filter) + 카테고리 재분류 + 이슈 그룹핑(issue_grouper -
                    1차 사전 매칭 + 2차 BGE-M3 임베딩 + 3차 LLM 보조)
  3) 스코어링    -> scorer.py가 이슈 단위 점수 계산 + 국내-해외 교차 매칭(🔗) +
                    카테고리 전체 집계(category_aggregator, 지난주 대비 증감 포함)
  4) LLM 요약    -> llm_summarizer.py: (A) 자체 요약 + (A-1) 얇은 재료 fallback.
                    (B) 그룹핑 보조는 issue_grouper.stage3_llm_assist. API 키 없음/호출
                    실패 시 "요약 생략, 원문 제목만 노출"로 안전하게 fallback
  5) 저장        -> storage.py - data/YYYY-WW/에 raw.json/scored.json/summary.md 저장.
                    git 커밋/푸시는 워크플로(run-pipline.yml) 책임.
  6) 배포        -> deploy.py - Gmail SMTP로 국내/해외 Top N + 카테고리별 Top N을 HTML
                    이메일 발송. 인증정보 미설정 시 발송만 안전하게 생략.
"""

import os
from collections import defaultdict
from datetime import datetime, timezone

import gdelt_collector
import naver_collector
import scorer
import issue_grouper
import llm_summarizer
import keyword_tagger
import category_aggregator
import relevance_filter
import storage
import deploy

# 주간 Top N(국내/해외 각 축) + 카테고리별 Top N. 환경변수로 조정 가능, 미설정 시 기존 기본값(5/1).
# CATEGORY_TOP_N을 올리면 LLM 요약 호출이 카테고리 수(최대 9) x 국내/해외(2) x 이 값만큼 늘어나므로
# OpenRouter 무료 티어 일일 요청 한도를 고려해서 조정할 것.
TOP_N = int(os.environ.get("TOP_N") or 5)
CATEGORY_TOP_N = int(os.environ.get("CATEGORY_TOP_N") or 1)


# ---------------------------------------------------------------------------
# 1) 수집 레이어
# ---------------------------------------------------------------------------

def run_collectors() -> tuple[list[dict], dict, list[str]]:
    """
    naver/gdelt collector를 순서대로 실행.

    각 collector를 개별 try/except로 감싸서, 하나가 통째로 실패해도(import 실패 등) 나머지는
    계속 진행한다 - collector 내부의 세밀한 방어와 별개로, 모듈 자체가 죽는 경우의 마지막 방어선.

    반환값: all_articles(두 collector 결과 합친 리스트, 공통 스키마라 합치기만 하면 됨),
            gdelt_timeline(참고 지표, scored.json에 그대로 저장), failed_sources(실패 소스명)
    """
    all_articles: list[dict] = []
    gdelt_timeline: dict = {}
    failed_sources: list[str] = []

    try:
        naver_articles = naver_collector.collect()
        all_articles.extend(naver_articles)
        print(f"[main] 네이버 수집 완료 - {len(naver_articles)}건")
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-02] - 네이버 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("네이버")

    try:
        gdelt_articles, gdelt_timeline = gdelt_collector.collect()
        all_articles.extend(gdelt_articles)
        print(f"[main] GDELT 수집 완료 - {len(gdelt_articles)}건")
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-03] - GDELT 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("GDELT")

    return all_articles, gdelt_timeline, failed_sources


# ---------------------------------------------------------------------------
# 2) 정규화 레이어 - 완전 동일 기사 제거 + 키워드 태깅
# ---------------------------------------------------------------------------

def normalize(articles: list[dict]) -> list[dict]:
    """
    완전 동일 기사(같은 URL 중복 수집) 제거 - 이슈 그룹핑과는 다른 개념, 페이지네이션 겹침·재실행
    등으로 정말 똑같은 기사가 두 번 들어온 경우만 거른다. 첫 번째로 본 URL을 유지.
    """
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

    return deduped


# ---------------------------------------------------------------------------
# 3) 스코어링 - scorer.py 그대로 사용
# ---------------------------------------------------------------------------

def score(articles: list[dict], model, top_n: int = TOP_N) -> tuple[list[dict], list[dict], dict, dict]:
    """
    이슈 그룹핑(issue_grouper.group_issues) + 국내/해외 개별 랭킹(Top N) 수행.

    ** 그룹핑을 먼저, 축 분리는 그 다음 **
    이슈 매칭은 전체 기사(국내-국내/해외-해외/국내-해외 전부) 대상이어야 국내-해외 교차 매칭이
    가능하다. 먼저 축을 나누면 교차 매칭이 구조적으로 불가능해지므로, 전체 기사로 group_issues()를
    한 번 호출한 뒤 그 결과를 국내/해외로 나눠서 scorer에 넘긴다.

    ** 국내-해외 교차 매칭 그룹 처리 **
    한 그룹이 국내·해외 기사를 동시에 포함할 수 있다. 각 축엔 그 축 기사만 걸러서 넘겨(각 축의
    issue_score가 그 축 원본 신호만 반영하게) 종합 랭킹은 만들지 않고, 대신 양쪽 다 있을 때만
    서로의 대표 기사에 "_cross_axis_partner"를 붙여 scorer.score_group()이 정식 필드로 승격한다.

    ** GDELT 한국어 기사는 국내로 재분류 **
    GDELT가 번역 인덱싱으로 한국어 기사를 물어오는 경우가 있어 소스만으로 판단하면 오분류된다.
    scorer._is_korean_gdelt_article()로 재분류 (category_aggregator도 같은 로직 공유 -
    Top N 스코어링과 카테고리 집계의 국내/해외 기준이 어긋나지 않게 함).

    model: SentenceTransformer 인스턴스 또는 None (None이면 issue_grouper가 1차 결과만으로 fallback).

    카테고리별 Top N도 함께 계산해서 반환 - 국내/해외 각 축 안에서 scorer.score_by_category()로
    (최대 카테고리 9개 x 국내/해외 2개 = 최대 18개 리스트). N값은 CATEGORY_TOP_N으로 조정.
    """
    groups = issue_grouper.group_issues(articles, model=model)

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
            # 앞의 _는 내부 전달용 임시 필드 표시 (storage.py가 raw.json 저장 시 제거,
            # scorer.score_group()이 정식 필드 cross_axis_partner로 승격)
            domestic_part[0]["_cross_axis_partner"] = international_part[0].get("title", "")
            international_part[0]["_cross_axis_partner"] = domestic_part[0].get("title", "")
        if domestic_part:
            domestic_groups.append(domestic_part)
        if international_part:
            international_groups.append(international_part)

    domestic_ranked = scorer.score_and_rank(domestic_groups, top_n=top_n)
    international_ranked = scorer.score_and_rank(international_groups, top_n=top_n)

    domestic_category_ranked = scorer.score_by_category(domestic_groups, CATEGORY_TOP_N)
    international_category_ranked = scorer.score_by_category(international_groups, CATEGORY_TOP_N)

    return domestic_ranked, international_ranked, domestic_category_ranked, international_category_ranked


# ---------------------------------------------------------------------------
# 2.1 이슈 그룹핑용 임베딩 모델 로드
# ---------------------------------------------------------------------------

def _load_embedding_model():
    """
    BGE-M3 임베딩 모델을 실행당 한 번만 로드해서 score() 단계에 주입한다 (모델 로드가 무거워서
    기사 배치마다 새로 로드하면 안 됨 - issue_grouper.stage2_group 참고). run() 전체에서 한 번만 호출.

    로드 실패(다운로드 실패, 패키지 미설치 등) 시 예외를 잡아 None 반환 - group_issues(model=None)이
    2차(임베딩) 생략하고 1차 사전 매칭만으로 안전하게 fallback하도록 이미 설계돼 있음.
    """
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-m3")
        print("[main] BGE-M3 임베딩 모델 로드 완료")
        return model
    except Exception as e:
        print(f"[main] 🟡 주의 [MN-04] - BGE-M3 모델 로드 실패 - 2차(임베딩) 매칭 없이 진행 "
              f"(1차 사전 매칭만 적용됨): {type(e).__name__} - {e!r}")
        return None


# ---------------------------------------------------------------------------
# 4) 카테고리별 요약 보조
# ---------------------------------------------------------------------------

def _regroup_by_category(items: list[dict]) -> dict[str, list[dict]]:
    """scorer.score_by_category()가 붙인 item["category"] 기준으로 평평한 리스트를 다시 {카테고리: [항목]}로 묶는다."""
    regrouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        regrouped[item.get("category", "미상")].append(item)
    return dict(regrouped)


def step4_category_llm_summary(domestic_category_ranked: dict[str, list[dict]],
                                international_category_ranked: dict[str, list[dict]],
                                ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """
    step4_llm_summary와 같은 (A)/(A-1) 로직을 카테고리별 Top N 결과에 적용.
    {카테고리: [항목]} 형태를 평평한 리스트로 합쳐서 요약(카테고리 축마다 세션 하나로 묶어 API
    호출 오버헤드 절감) 후 다시 카테고리별로 묶어 반환.
    """
    domestic_flat = [item for items in domestic_category_ranked.values() for item in items]
    international_flat = [item for items in international_category_ranked.values() for item in items]

    domestic_summarized_flat = llm_summarizer.summarize_top_issues(domestic_flat, label="국내-카테고리")
    international_summarized_flat = llm_summarizer.summarize_top_issues(international_flat, label="해외-카테고리")

    return (_regroup_by_category(domestic_summarized_flat),
            _regroup_by_category(international_summarized_flat))


def step4_llm_summary(domestic_ranked: list[dict],
                       international_ranked: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    (A) 자체 요약 + (A-1) 얇은 재료 fallback. 실제 로직은 llm_summarizer.py, 여기는 국내/해외를
    각각 넘기는 얇은 호출부다 ((B) 그룹핑 보조는 issue_grouper.stage3_llm_assist가 별도 처리).

    domestic_ranked/international_ranked는 score()에서 이미 TOP_N으로 제한된 상태 (추가로 안 자름).
    반환값은 입력과 같은 형태에 "summary"/"summary_skipped_reason" 필드가 추가된 것.
    """
    domestic_summarized = llm_summarizer.summarize_top_issues(domestic_ranked, label="국내")
    international_summarized = llm_summarizer.summarize_top_issues(international_ranked, label="해외")
    return domestic_summarized, international_summarized


# ---------------------------------------------------------------------------
# 오케스트레이션 진입점
# ---------------------------------------------------------------------------

def run() -> None:
    print("=== [1] 수집 시작 ===")
    articles, gdelt_timeline, failed_sources = run_collectors()

    print("\n=== [2] 정규화 ===")
    # 이 단계부터 [4-보조]까지 각 단계를 안전망으로 감싼다 - 예상 못 한 예외가 나도 그 단계만
    # 기본값으로 넘어가고, 이미 모은 articles는 살려서 [5] 저장/[6] 배포까지 도달하게 함.
    try:
        articles = normalize(articles)
        keyword_tagger.tag_articles(articles)
        keyword_tagger.print_category_distribution(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-05] - [2] 정규화/태깅 단계에서 예상 못 한 오류 발생 - 원본 기사 그대로 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.5] 관련성 필터 ===")
    # 키워드 매칭만으로 못 거르는 오매칭(동음이의어, 각주성 언급 등)을 LLM이 맥락으로 판단해 필터링
    try:
        articles = relevance_filter.filter_articles(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-06] - [2.5] 관련성 필터 단계에서 예상 못 한 오류 발생 - 필터링 없이 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.6] 카테고리 재분류 ===")
    # keyword_tagger(사전 매칭)와 relevance_filter(LLM 판단)의 기준이 달라서, 사전엔 안 걸려
    # "기타"로 붙었는데 relevance_filter는 "관련 있음"으로 확정하는 기사가 생길 수 있다 -
    # 이런 기사만 다시 LLM에 물어 재분류.
    try:
        articles = relevance_filter.recategorize_uncategorized(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-07] - [2.6] 카테고리 재분류 단계에서 예상 못 한 오류 발생 - 재분류 없이 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.1] 이슈 그룹핑 임베딩 모델 로드 ===")
    embedding_model = _load_embedding_model()

    print("\n=== [3] 스코어링 ===")
    try:
        (domestic_ranked, international_ranked,
         domestic_category_ranked, international_category_ranked) = score(articles, embedding_model, top_n=TOP_N)
        scorer.print_top_n("국내", domestic_ranked, n=TOP_N)
        scorer.print_top_n("해외", international_ranked, n=TOP_N)
        scorer.print_category_top_n("국내", domestic_category_ranked, n=CATEGORY_TOP_N)
        scorer.print_category_top_n("해외", international_category_ranked, n=CATEGORY_TOP_N)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-08] - [3] 스코어링 단계에서 예상 못 한 오류 발생 - 이번 주는 Top N 없이 진행"
              f"(저장 단계에서 raw.json은 그대로 남음): {type(e).__name__} - {e!r}")
        domestic_ranked, international_ranked = [], []
        domestic_category_ranked, international_category_ranked = {}, {}

    print("\n=== [3-보조] 카테고리 전체 집계 ===")
    # 이슈 그룹핑이 "동일 사건만" 묶는 좁은 정의라 생기는 공백을 메우는 거친 보조 지표
    # (category_aggregator.py 참고)
    try:
        category_distribution = category_aggregator.aggregate(articles)
        # 지난주 데이터 없으면(첫 실행 등) compare_with_last_week가 안전하게 None 반환
        category_comparison = category_aggregator.compare_with_last_week(category_distribution)
        category_aggregator.print_aggregate_with_comparison(category_distribution, category_comparison)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-09] - [3-보조] 카테고리 집계 단계에서 예상 못 한 오류 발생 - 이번 주는 집계 없이 진행: "
              f"{type(e).__name__} - {e!r}")
        category_distribution, category_comparison = {}, None

    print("\n=== [4] 국내/해외 LLM 요약 생성 ===")
    try:
        domestic_summarized, international_summarized = step4_llm_summary(
            domestic_ranked, international_ranked)
        llm_summarizer.print_summaries("국내", domestic_summarized)
        llm_summarizer.print_summaries("해외", international_summarized)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-10] - [4] LLM 요약 단계에서 예상 못 한 오류 발생 - 요약 없이(원문 제목만) 진행: "
              f"{type(e).__name__} - {e!r}")
        domestic_summarized, international_summarized = domestic_ranked, international_ranked

    print("\n=== [4-보조] 카테고리별 LLM 요약 생성 ===")
    try:
        domestic_category_summarized, international_category_summarized = step4_category_llm_summary(
            domestic_category_ranked, international_category_ranked)
        for category, items in domestic_category_summarized.items():
            llm_summarizer.print_summaries(f"국내-{category}", items)
        for category, items in international_category_summarized.items():
            llm_summarizer.print_summaries(f"해외-{category}", items)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-11] - [4-보조] 카테고리별 LLM 요약 단계에서 예상 못 한 오류 발생 - 요약 없이(원문 제목만) 진행: "
              f"{type(e).__name__} - {e!r}")
        domestic_category_summarized, international_category_summarized = (
            domestic_category_ranked, international_category_ranked)

    print("\n=== [5] 저장 ===")
    # 수동 실행(workflow_dispatch)은 테스트/임시 확인용이라 저장하면 안 됨 - "지난주 대비 증감"
    # 비교 기준이 테스트 데이터로 오염될 수 있음(예: 토요일 테스트 실행이 다음 월요일 정식
    # 실행의 "지난주 실적"으로 잘못 비교되는 사고).
    # GITHUB_EVENT_NAME은 GitHub Actions 자동 환경변수 (로컬 등 없는 환경에선 "수동 아님"으로 처리)
    is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if is_manual_run:
        print("[main] 수동 실행(workflow_dispatch)이라 저장을 건너뜁니다 - "
              "'지난주 대비 증감' 비교 기준 오염 방지(콘솔에 출력된 이번 실행 결과는 그대로 확인 가능, data/에는 안 남음)")
        saved_dir = None
    else:
        try:
            saved_dir = storage.save_week(articles, domestic_summarized, international_summarized,
                                           domestic_category_summarized, international_category_summarized,
                                           gdelt_timeline, failed_sources, category_distribution,
                                           category_comparison)
        except Exception as e:
            print(f"[main] 🔴 조치필요 [MN-12] - 저장 단계에서 예상 못 한 오류 발생(콘솔 로그의 결과는 그대로 유효함): "
                  f"{type(e).__name__} - {e!r}")
            saved_dir = None
    print("\n=== [6] 배포 ===")
    # week_label은 저장이 성공했으면 그 디렉토리 이름을 재사용(주차 계산 중복 방지),
    # 저장이 실패한 드문 경우만 직접 계산.
    try:
        week_label = os.path.basename(saved_dir) if saved_dir else datetime.now(timezone.utc).strftime("%G-%V")
        deploy.send_weekly_email(week_label, domestic_summarized, international_summarized,
                                  domestic_category_summarized, international_category_summarized,
                                  failed_sources, category_comparison)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-13] - 배포 단계에서 예상 못 한 오류 발생(콘솔 로그의 결과는 그대로 유효함): "
              f"{type(e).__name__} - {e!r}")

    if failed_sources:
        saved_dir_note = f"{saved_dir}/scored.json에도" if saved_dir else "(data/ 파일에는 안 남았지만)"
        print(f"\n[main] 🔴 조치필요 [MN-14] - 이번 실행 실패 소스: {failed_sources} "
              f"({saved_dir_note} failed_sources로 같이 저장됨)")


if __name__ == "__main__":
    run()