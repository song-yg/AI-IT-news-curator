"""
main.py
AI·IT 뉴스 큐레이션 시스템의 오케스트레이션 레이어.

6단계 파이프라인:
  1) 수집        -> naver/gdelt collector 순차 실행
  2) 정규화      -> 공통 스키마 통합 + URL 중복 제거 + 키워드 태깅(keyword_tagger) +
                    이슈 그룹핑(issue_grouper - 1차 사전 매칭 + 2차 BGE-M3 임베딩 + 3차
                    LLM 보조) + 관련성 필터(relevance_filter, 그룹 대표 1건씩 판단) +
                    카테고리 재분류(그룹 대표 1건씩 판단)
  3) 스코어링    -> scorer.py가 이슈 단위 점수 계산 + 국내-해외 교차 매칭(🔗) +
                    카테고리 전체 집계(category_aggregator, 전일/7일 평균 대비 증감 포함)
  4) LLM 요약    -> llm_summarizer.py: (A) 자체 요약 + (A-1) 얇은 재료 fallback.
                    (B) 그룹핑 보조는 issue_grouper.stage3_llm_assist. API 키 없음/호출
                    실패 시 "요약 생략, 원문 제목만 노출"로 안전하게 fallback
  5) 저장        -> storage.py - data/YYYY-MM-DD/에 raw.json/scored.json/summary.md 저장.
                    git 커밋/푸시는 워크플로(run-pipline.yml) 책임.
  6) 배포        -> deploy.py - Gmail SMTP로 국내/해외 Top N + 카테고리별 Top N을 HTML
                    이메일 발송. 인증정보 미설정 시 발송만 안전하게 생략.

** 그룹핑을 관련성 필터보다 먼저 실행 **
예전엔 "관련성 필터 -> 카테고리 재분류 -> 그룹핑" 순서로, 관련성 필터가 기사 하나하나를
전부 개별 판단했다. 지금은 그룹핑을 먼저 끝내고, 관련성 필터/카테고리 재분류는 각 그룹의
대표 기사(issue_grouper._sort_group_by_representative가 판단 근거 텍스트가 가장 긴 기사를
대표로 정렬해둠) 1건만 LLM에 물어서 그룹 전체에 판정을 적용한다 - 같은 사건을 다루는
기사들을 굳이 하나씩 따로 물어볼 필요가 없다는 판단(호출 수 절감 + 같은 이슈인데 판정이
갈리는 불일치 방지). run_process()(아래)가 이 순서를 담당.
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

_KST = timezone(timedelta(hours=9))


def _compute_collection_window(reference: datetime | None = None) -> tuple[datetime, datetime]:
    """
    이번 실행이 다룰 "어제" 하루를 절대 구간([window_start, window_end), UTC datetime)으로
    계산한다 - "어제 00:00 KST ~ 오늘 00:00 KST". 파이프라인 시작 시점에 딱 한 번만 호출해서
    naver_collector.collect()/gdelt_collector.collect()/normalize()에 전부 동일하게 넘긴다.

    예전엔 각 collector가 "지금부터 24시간 전까지"를 자기 호출 시점마다 따로 계산했는데
    (naver_collector.py의 옛 _is_recent, gdelt_collector.py의 옛 TIMESPAN="1d"), 이게 문제였다:
    GDELT 수집이 최대 220분에 걸쳐 배치 단위로 순차 요청되다 보니, 같은 실행 안에서도 먼저
    처리된 키워드와 나중에 처리된 키워드가 서로 다른 절대 구간을 보게 되는 드리프트가 있었다
    (최대 3~4시간 - gdelt_collector.py의 _is_in_window 선언부 주석 참고). 여기서 고정 구간을
    한 번만 계산해서 전부에 그대로 넘기면, 몇 시에 처리되든 항상 정확히 같은 구간을 보게 된다.

    파이프라인이 매일 00:01 KST에 도는 것과 맞물려서, "어제 00:00~오늘 00:00 KST"가 곧
    "직전 실행 이후 오늘 실행 시작까지"와 거의 일치한다 - 발송 시점의 "전날 기사"라는 취지에
    자연스럽게 맞아떨어진다.
    """
    now_kst = (reference or datetime.now(timezone.utc)).astimezone(_KST)
    today_midnight_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = today_midnight_kst
    window_start = window_end - timedelta(days=1)
    return window_start.astimezone(timezone.utc), window_end.astimezone(timezone.utc)


# 일간 Top N(국내/해외 각 축) + 카테고리별 Top N. 환경변수로 조정 가능, 미설정 시 기존 기본값(5/1).
# CATEGORY_TOP_N을 올리면 LLM 요약 호출이 카테고리 수(최대 9) x 국내/해외(2) x 이 값만큼 늘어나므로
# OpenRouter 무료 티어 일일 요청 한도를 고려해서 조정할 것.
TOP_N = int(os.environ.get("TOP_N") or 5)
CATEGORY_TOP_N = int(os.environ.get("CATEGORY_TOP_N") or 1)

# --- [2] 정규화 단계의 시간예산 ---
#
# 관련성 필터/카테고리 재분류는 그룹 대표 1건만 판단하지만, 그룹 수 자체가 수집량(최대
# 어제 하루치 전체)에 비례해서 늘 수 있어 예산을 둔다 - 각각 이 예산 계산의 기준 시각
# (run_process 호출부가 process_start로 넘겨줌: run()은 파이프라인 시작 시각, Job
# 체이닝의 process_stage.py는 그 Job 시작 시각)부터 절대 마감까지.
# 이슈 그룹핑(1~3차)/4차 Top N 사후 재검토/LLM 요약은 시간예산 없음(무제한) - TOP_N이
# 고정값이라 수집량과 무관하게 규모가 안 커지는 단계들이라 상대적으로 안전하다고 판단.
# run_process()에서 이 셋에는 deadline=float("inf")를 넘긴다(각 함수의 "deadline 없으면
# 자기 모듈 기본값으로 폴백"하는 동작을 피하기 위함 - None을 넘기면 그 폴백이 발동해
# 버려서 결과적으로 예산이 생겨버림).
RELEVANCE_TIME_BUDGET_SECONDS = 4 * 60 * 60             # 240분(4시간) - 관련성 필터
CATEGORY_RECLASSIFY_TIME_BUDGET_SECONDS = 5 * 60 * 60   # 300분(5시간) - 카테고리 재분류



# ---------------------------------------------------------------------------
# 1) 수집 레이어
# ---------------------------------------------------------------------------

def run_collectors(window_start: datetime, window_end: datetime,
                    deadline: float | None = None) -> tuple[list[dict], dict, list[str]]:
    """
    naver/gdelt collector를 순서대로 실행.

    각 collector를 개별 try/except로 감싸서, 하나가 통째로 실패해도(import 실패 등) 나머지는
    계속 진행한다 - collector 내부의 세밀한 방어와 별개로, 모듈 자체가 죽는 경우의 마지막 방어선.

    window_start/window_end: 이번 실행이 다룰 절대 구간(_compute_collection_window 참고).
    두 collector 모두에 그대로 전달 - 몇 시에 처리되든 정확히 같은 구간을 보게 하기 위함.

    deadline: 파이프라인 전역 공유 예산의 절대 마감 시각. gdelt_collector.collect()에 그대로
    전달한다. 네이버 수집은 재시도 루프가 없어 시간예산 메커니즘 자체가 없으므로 이 값을 안 씀.

    반환값: all_articles(두 collector 결과 합친 리스트, 공통 스키마라 합치기만 하면 됨),
            gdelt_timeline(참고 지표, scored.json에 그대로 저장), failed_sources(실패 소스명)
    """
    all_articles: list[dict] = []
    gdelt_timeline: dict = {}
    failed_sources: list[str] = []

    try:
        naver_articles = naver_collector.collect(window_start, window_end)
        all_articles.extend(naver_articles)
        print(f"[main] 네이버 수집 완료 - {len(naver_articles)}건")
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-02] - 네이버 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("네이버")

    try:
        gdelt_articles, gdelt_timeline = gdelt_collector.collect(
            deadline=deadline, window_start=window_start, window_end=window_end)
        all_articles.extend(gdelt_articles)
        print(f"[main] GDELT 수집 완료 - {len(gdelt_articles)}건")
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-03] - GDELT 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("GDELT")

    return all_articles, gdelt_timeline, failed_sources


# ---------------------------------------------------------------------------
# 2) 정규화 레이어 - 완전 동일 기사 제거 + 키워드 태깅
# ---------------------------------------------------------------------------

def normalize(articles: list[dict], window_start: datetime, window_end: datetime) -> list[dict]:
    """
    완전 동일 기사(같은 URL 중복 수집) 제거 - 이슈 그룹핑과는 다른 개념, 페이지네이션 겹침·재실행
    등으로 정말 똑같은 기사가 두 번 들어온 경우만 거른다. 첫 번째로 본 URL을 유지.

    그다음 scorer.is_in_window()로 [window_start, window_end) 구간을 벗어난 기사를 걸러낸다.
    naver/gdelt 각 collector가 이미 이 구간(_compute_collection_window 참고)으로 정확히
    걸러서 수집하므로, 정상 흐름에서는 여기서 걸러질 기사가 없어야 정상 - 이건 방어선
    (defense in depth) 역할이다(예: collector 로직 결함, 향후 이 구간 필터를 안 지키는 새
    소스가 추가되는 경우 등에 대비).
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

    fresh = []
    out_of_window_count = 0
    for article in deduped:
        try:
            if not scorer.is_in_window(article["published_at"], window_start, window_end):
                out_of_window_count += 1
                continue
        except (KeyError, ValueError, TypeError):
            pass  # published_at 없음/형식 이상 - 판단 못 하니 안전하게 통과시킴(걸러내지 않음)
        fresh.append(article)

    if out_of_window_count:
        print(f"[main] 🟡 주의 - 수집 구간({window_start.isoformat()} ~ {window_end.isoformat()}) "
              f"밖 기사 {out_of_window_count}건 제외 - collector가 이미 이 구간으로 거르므로 "
              f"정상 흐름에선 0건이어야 함, 0이 아니면 collector 쪽 확인 필요")

    return fresh


# ---------------------------------------------------------------------------
# 3) 스코어링 - scorer.py 그대로 사용
# ---------------------------------------------------------------------------

def _collect_shown_titles(ranked: list[dict], category_ranked: dict[str, list[dict]]) -> set[str]:
    """
    축 하나(국내 또는 해외)에서 이메일/summary.md에 실제로 노출되는 모든 제목의 집합.
    메인 Top N + 카테고리별 Top N 전부를 합치고, 각 이슈의 titles 전체(그룹 내 대표
    제목 하나만이 아니라 그룹에 묶인 기사 전체 제목)를 포함한다 - 4차 사후 재검토에서
    그룹이 병합되면 대표 제목이 바뀔 수 있어서, 전체를 봐야 cross_axis_partner 매칭이
    안 깨진다(score()의 _scrub_unshown_cross_axis_partner 참고).
    """
    titles: set[str] = set()
    for item in ranked:
        titles.update(item.get("titles", []))
    for items in category_ranked.values():
        for item in items:
            titles.update(item.get("titles", []))
    return titles


def _scrub_unshown_cross_axis_partner(items: list[dict], other_axis_shown_titles: set[str]) -> None:
    """
    cross_axis_partner가 가리키는 제목이 반대 축의 실제 노출 목록(other_axis_shown_titles)에
    없으면 지운다(제자리 수정). score() 안에서 cross_axis_partner는 이슈 그룹 형성 시점 -
    아직 어느 쪽 Top N에 들지 안 들지 모르는 시점 - 에 일단 붙여두는데, 국내/해외 두 축이
    완전히 독립적으로 순위를 매기다 보니 한쪽엔 들고 반대쪽엔 못 드는 경우가 흔하다.
    그대로 두면 "🔗 반대 축에서도 다뤄짐"이 이메일 어디에도 실제로 안 나오는 기사 제목을
    가리키게 되므로, 최종 노출 목록이 확정된 뒤 이 함수로 마지막에 걸러낸다.
    """
    for item in items:
        partner = item.get("cross_axis_partner")
        if partner and partner not in other_axis_shown_titles:
            item["cross_axis_partner"] = None


def score(groups: list[list[dict]], top_n: int = TOP_N,
          stage4_deadline: float | None = None) -> tuple[list[dict], list[dict], dict, dict]:
    """
    이미 그룹핑된 groups(issue_grouper.group_issues 결과)를 국내/해외로 나눠
    Top N + 카테고리별 Top N을 계산한다.

    ** 그룹핑은 이 함수 밖(run()/process_stage.py)에서 미리 끝나 있어야 함 **
    예전엔 이 함수 안에서 issue_grouper.group_issues()를 직접 호출했는데, 관련성 필터를
    "그룹 대표 1건만 판단"하는 방식으로 바꾸면서 그룹핑 자체가 관련성 필터보다 먼저
    실행돼야 하는 순서가 됐다 - 그래서 그룹핑은 run()이 이 함수 호출 전에 끝내고, 여기서는
    이미 필터링/재분류까지 끝난 groups만 받아 국내/해외 분리·랭킹만 담당한다.

    ** 국내-해외 교차 매칭 그룹 처리 **
    한 그룹이 국내·해외 기사를 동시에 포함할 수 있다. 각 축엔 그 축 기사만 걸러서 넘겨(각 축의
    issue_score가 그 축 원본 신호만 반영하게) 종합 랭킹은 만들지 않고, 대신 양쪽 다 있을 때만
    서로의 대표 기사에 "_cross_axis_partner"를 붙여 scorer.score_group()이 정식 필드로 승격한다.
    이 시점(그룹 형성 시점)엔 아직 어느 쪽이 Top N에 들지 모르므로 일단 붙여두고, 함수 끝에서
    최종 노출 목록이 확정된 뒤 반대 축에 실제로 없는 건 지운다(_scrub_unshown_cross_axis_partner
    참고) - 안 그러면 이메일에 없는 기사 제목이 "🔗 반대 축에서도 다뤄짐"으로 걸려있게 된다.

    ** GDELT 한국어 기사는 국내로 재분류 **
    GDELT가 번역 인덱싱으로 한국어 기사를 물어오는 경우가 있어 소스만으로 판단하면 오분류된다.
    scorer._is_korean_gdelt_article()로 재분류 (category_aggregator도 같은 로직 공유 -
    Top N 스코어링과 카테고리 집계의 국내/해외 기준이 어긋나지 않게 함).

    stage4_deadline: 4차 Top N 사후 재검토 몫의 절대 마감 시각.
    issue_grouper.stage4_dedupe_and_promote() 호출(국내/해외 + 카테고리별 최대 18회)에 전부 전달.

    카테고리별 Top N도 함께 계산해서 반환 - 국내/해외 각 축 안에서 scorer.score_by_category()로
    (최대 카테고리 9개 x 국내/해외 2개 = 최대 18개 리스트). N값은 CATEGORY_TOP_N으로 조정.

    ** 4차 사후 재검토 **
    국내/해외 Top N + 카테고리별 Top N 전부 issue_grouper.stage4_dedupe_and_promote로
    사후 재검토(병합+승격)를 거친다. top_n 없는 전체 순위 풀을 넘겨서, 상위 후보끼리
    같은 사건인지 LLM으로 한 번 더 확인 후 병합하고 빈 자리는 다음 순위로 채운다.
    카테고리별은 scorer.score_by_category의 dedupe_fn 콜백으로 연결한다.
    """
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

    # cross_axis_partner는 위에서 "그룹 형성 시점"에 붙였는데, 그건 아직 Top N 선정
    # *이전*이라 그 그룹이 실제로 반대 축 Top N(메인 또는 카테고리별)에 들지 안 들지
    # 모르는 상태에서 일단 붙인 것이다 - 국내/해외 두 축이 완전히 독립적으로 순위를 매기므로
    # 한쪽엔 들고 한쪽엔 못 드는 경우가 흔하다. 그 상태로 그냥 두면 "🔗 반대 축에서도
    # 다뤄짐"이 실제로는 이메일/summary.md 어디에도 안 나오는 기사 제목을 가리키게 된다.
    # 그래서 여기서 양쪽 축의 최종 노출 제목(메인 Top N + 카테고리별 Top N 전부, 그룹 내
    # titles 전체 - 대표 제목 하나만이 아니라 다 포함해야 4차 병합으로 대표가 바뀌어도
    # 매칭이 안 깨짐) 집합을 만들어두고, 상대측 집합에 없는 cross_axis_partner는 지운다.
    domestic_shown_titles = _collect_shown_titles(domestic_ranked, domestic_category_ranked)
    international_shown_titles = _collect_shown_titles(international_ranked, international_category_ranked)

    _scrub_unshown_cross_axis_partner(domestic_ranked, international_shown_titles)
    _scrub_unshown_cross_axis_partner(international_ranked, domestic_shown_titles)
    for items in domestic_category_ranked.values():
        _scrub_unshown_cross_axis_partner(items, international_shown_titles)
    for items in international_category_ranked.values():
        _scrub_unshown_cross_axis_partner(items, domestic_shown_titles)

    return domestic_ranked, international_ranked, domestic_category_ranked, international_category_ranked


# ---------------------------------------------------------------------------
# 2.1 이슈 그룹핑용 임베딩 모델 로드
# ---------------------------------------------------------------------------

def _load_embedding_model():
    """
    BGE-M3 임베딩 모델을 실행당 한 번만 로드해서 score() 단계에 주입한다.
    (모델 로드가 무거워서 기사 배치마다 새로 로드하면 안 됨 - issue_grouper.stage2_group 참고).
    run() 전체에서 한 번만 호출.

    로드 실패(다운로드 실패, 패키지 미설치 등) 시 예외를 잡아 None 반환
      - group_issues(model=None)이 2차(임베딩) 생략하고 1차 사전 매칭만으로 안전하게 fallback하도록 설계돼 있음.
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
                                deadline: float | None = None,
                                ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """
    step4_llm_summary와 같은 (A)/(A-1) 로직을 카테고리별 Top N 결과에 적용.
    {카테고리: [항목]} 형태를 평평한 리스트로 합쳐서 요약(카테고리 축마다 세션 하나로 묶어 API 호출 오버헤드 절감) 후 다시 카테고리별로 묶어 반환.

    deadline: 파이프라인 전역 공유 예산의 절대 마감 시각. summarize_top_issues 두 호출에 그대로 전달.
    """
    domestic_flat = [item for items in domestic_category_ranked.values() for item in items]
    international_flat = [item for items in international_category_ranked.values() for item in items]

    domestic_summarized_flat = llm_summarizer.summarize_top_issues(domestic_flat, label="국내-카테고리", deadline=deadline)
    international_summarized_flat = llm_summarizer.summarize_top_issues(international_flat, label="해외-카테고리", deadline=deadline)

    return (_regroup_by_category(domestic_summarized_flat),
            _regroup_by_category(international_summarized_flat))


def step4_llm_summary(domestic_ranked: list[dict],
                       international_ranked: list[dict],
                       deadline: float | None = None) -> tuple[list[dict], list[dict]]:
    """
    (A) 자체 요약 + (A-1) 얇은 재료 fallback.
    실제 로직은 llm_summarizer.py, 여기는 국내/해외를 각각 넘기는 얇은 호출부다.
    ((B) 그룹핑 보조는 issue_grouper.stage3_llm_assist가 별도 처리)

    domestic_ranked/international_ranked는 score()에서 이미 TOP_N으로 제한된 상태 (추가로 안 자름).
    반환값은 입력과 같은 형태에 "summary"/"summary_skipped_reason" 필드가 추가된 것.

    deadline: 파이프라인 전역 공유 예산의 절대 마감 시각. summarize_top_issues 두 호출에 그대로 전달.
    """
    domestic_summarized = llm_summarizer.summarize_top_issues(domestic_ranked, label="국내", deadline=deadline)
    international_summarized = llm_summarizer.summarize_top_issues(international_ranked, label="해외", deadline=deadline)
    return domestic_summarized, international_summarized


# ---------------------------------------------------------------------------
# 오케스트레이션 진입점
# ---------------------------------------------------------------------------

def run_process(articles: list[dict], gdelt_timeline: dict, failed_sources: list[str],
                 window_start: datetime, window_end: datetime,
                 process_start: float | None = None,
                 prior_error_codes: list[str] | None = None) -> None:
    """
    [2] 정규화 ~ [6] 배포 전체. run()(단일 실행, 로컬 테스트용)과 process_stage.py(Job
    체이닝의 process Job)가 공유하는 본체 - process_start만 호출부가 정해서 넘겨준다
    (run()은 파이프라인 전체가 시작된 시각, process_stage.py는 자기 Job이 시작된 시각).
    None이면 이 함수 시작 시점을 기준으로 삼는다.

    이 단계부터 [4-보조]까지 각 단계를 개별 try/except 안전망으로 감싼다 - 예상 못 한
    예외가 나도 그 단계만 기본값으로 넘어가고, 이미 모은 articles는 살려서 [5] 저장/
    [6] 배포까지 도달하게 함.

    prior_error_codes: 이 함수 밖(수집 단계)에서 이미 발생한 "🔴 조치필요" 코드 목록
    (error_log.py 참고) - [6] 배포에서 이번 실행 전체(수집+정규화~요약)의 코드를 합쳐
    이메일 하단에 표시하기 위해 받는다. collect_stage.py는 별도 프로세스(Job)라 여기
    캡처에 안 잡히므로, process_stage.py가 collected.json에서 읽어 넘겨준다.
    """
    if process_start is None:
        process_start = time.monotonic()

    deadline_relevance = process_start + RELEVANCE_TIME_BUDGET_SECONDS
    deadline_category = process_start + CATEGORY_RECLASSIFY_TIME_BUDGET_SECONDS
    # 그룹핑/4차 재검토/요약은 무제한 - float("inf")를 명시적으로 넘겨야 각 함수의
    # "deadline 없으면 자기 모듈 기본값으로 폴백"하는 동작을 피할 수 있다(위 상수 선언부
    # 주석 참고).
    deadline_grouping = float("inf")
    deadline_stage4 = float("inf")
    deadline_summary = float("inf")

    # [6] 배포에서 이메일 하단에 표시할 오류 코드 수집 시작 - [5] 저장까지의 모든 🔴 조치필요
    # 로그를 모은다(들여쓰기 없이 쓸 수 있는 짝 함수, error_log.py 참고. 아래 [6] 진입 직전에
    # stop_capture()로 회수).
    error_log.start_capture()

    print("\n=== [2] 정규화 ===")
    try:
        articles = normalize(articles, window_start, window_end)
        keyword_tagger.tag_articles(articles)
        keyword_tagger.print_category_distribution(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-05] - [2] 정규화/태깅 단계에서 예상 못 한 오류 발생 - 원본 기사 그대로 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.1] 이슈 그룹핑 임베딩 모델 로드 ===")
    embedding_model = _load_embedding_model()

    print("\n=== [2.2] 이슈 그룹핑 ===")
    # 관련성 필터보다 먼저 실행 - 필터를 "그룹 대표 1건만 판단"하는 방식으로 바꾸면서
    # 그룹핑 자체가 관련성 필터보다 먼저 끝나 있어야 하는 순서가 됨(아래 [2.5] 참고).
    try:
        groups = issue_grouper.group_issues(articles, model=embedding_model, deadline=deadline_grouping)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-06] - [2.2] 이슈 그룹핑 단계에서 예상 못 한 오류 발생 - "
              f"그룹핑 없이(기사 1건 = 그룹 1개) 다음 단계로 진행: {type(e).__name__} - {e!r}")
        groups = [[a] for a in articles]

    print("\n=== [2.5] 관련성 필터 (그룹 대표 1건씩 판단) ===")
    # 키워드 매칭만으로 못 거르는 오매칭(동음이의어, 각주성 언급 등)을 LLM이 맥락으로 판단해 필터링.
    # 그룹 대표(issue_grouper._sort_group_by_representative가 판단 근거 텍스트가 가장 긴
    # 기사를 대표로 정렬해둠) 1건만 판단해서 그룹 전체에 적용 - 같은 사건 기사를 하나씩
    # 따로 물어볼 필요가 없다는 판단(호출 수 절감 + 판정 불일치 방지).
    try:
        groups = relevance_filter.filter_groups(groups, deadline=deadline_relevance)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-07] - [2.5] 관련성 필터 단계에서 예상 못 한 오류 발생 - 필터링 없이 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.6] 카테고리 재분류 (그룹 대표 1건씩 판단) ===")
    # keyword_tagger(사전 매칭)와 relevance_filter(LLM 판단)의 기준이 달라서, 사전엔 안 걸려 "기타"로 붙었는데
    # relevance_filter는 "관련 있음"으로 확정하는 그룹이 생길 수 있다 - 이런 그룹만 대표 기사로 다시 LLM에 물어 재분류.
    try:
        groups = relevance_filter.recategorize_uncategorized_groups(groups, deadline=deadline_category)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-08] - [2.6] 카테고리 재분류 단계에서 예상 못 한 오류 발생 - 재분류 없이 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    # 카테고리 집계([3-보조])/raw.json 저장([5])은 개별 기사 단위 리스트가 필요해서,
    # 필터링/재분류까지 끝난 groups를 다시 평평한 리스트로 펼친다.
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
        print(f"[main] 🔴 조치필요 [MN-09] - [3] 스코어링 단계에서 예상 못 한 오류 발생 - 오늘은 Top N 없이 진행"
              f"(저장 단계에서 raw.json은 그대로 남음): {type(e).__name__} - {e!r}")
        domestic_ranked, international_ranked = [], []
        domestic_category_ranked, international_category_ranked = {}, {}

    print("\n=== [3-보조] 카테고리 전체 집계 ===")
    # 이슈 그룹핑이 "동일 사건만" 묶는 좁은 정의라 생기는 공백을 메우는 거친 보조 지표 (category_aggregator.py 참고)
    try:
        category_distribution = category_aggregator.aggregate(articles)
        # 과거 데이터 없으면(첫 실행 등) compare_with_history가 안전하게 None 반환
        category_comparison = category_aggregator.compare_with_history(category_distribution)
        category_aggregator.print_aggregate_with_history(category_distribution, category_comparison)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-10] - [3-보조] 카테고리 집계 단계에서 예상 못 한 오류 발생 - 오늘은 집계 없이 진행: "
              f"{type(e).__name__} - {e!r}")
        category_distribution, category_comparison = {}, None

    # 최근 7일(오늘 포함) 카테고리별 증감 그래프 - category_distribution이 있어야(오늘 몫)
    # 그릴 수 있어서 위 집계 바로 다음, [5] 저장으로 아직 파일화되기 전에 생성한다.
    try:
        category_charts = category_chart.generate_charts(category_distribution)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-11] - 카테고리 추이 그래프 단계에서 예상 못 한 오류 발생 - 그래프 없이 진행: "
              f"{type(e).__name__} - {e!r}")
        category_charts = {}

    print("\n=== [4] 국내/해외 LLM 요약 생성 ===")
    try:
        domestic_summarized, international_summarized = step4_llm_summary(
            domestic_ranked, international_ranked, deadline=deadline_summary)
        llm_summarizer.print_summaries("국내", domestic_summarized)
        llm_summarizer.print_summaries("해외", international_summarized)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-12] - [4] LLM 요약 단계에서 예상 못 한 오류 발생 - 요약 없이(원문 제목만) 진행: "
              f"{type(e).__name__} - {e!r}")
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
        print(f"[main] 🔴 조치필요 [MN-13] - [4-보조] 카테고리별 LLM 요약 단계에서 예상 못 한 오류 발생 - 요약 없이(원문 제목만) 진행: "
              f"{type(e).__name__} - {e!r}")
        domestic_category_summarized, international_category_summarized = (
            domestic_category_ranked, international_category_ranked)

    print("\n=== [5] 저장 ===")
    # 수동 실행(workflow_dispatch)은 테스트/임시 확인용이라 저장하면 안 됨 - "전일/7일 평균 대비 증감"
    # 비교 기준이 테스트 데이터로 오염될 수 있음(예: 화요일 테스트 실행이 수요일 정식 실행의 "전일 실적"으로 잘못 비교되는 사고).
    # GITHUB_EVENT_NAME은 GitHub Actions 자동 환경변수 (로컬 등 없는 환경에선 "수동 아님"으로 처리)
    is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if is_manual_run:
        print("[main] 수동 실행(workflow_dispatch)이라 저장을 건너뜁니다 - "
              "'전일/7일 평균 대비 증감' 비교 기준 오염 방지(콘솔에 출력된 이번 실행 결과는 그대로 확인 가능, data/에는 안 남음)")
        saved_dir = None
    else:
        try:
            saved_dir = storage.save_day(articles, domestic_summarized, international_summarized,
                                          domestic_category_summarized, international_category_summarized,
                                          gdelt_timeline, failed_sources, category_distribution,
                                          category_comparison)
        except Exception as e:
            print(f"[main] 🔴 조치필요 [MN-14] - 저장 단계에서 예상 못 한 오류 발생(콘솔 로그의 결과는 그대로 유효함): "
                  f"{type(e).__name__} - {e!r}")
            saved_dir = None
    # [2]~[5]까지 발생한 🔴 조치필요 코드 회수 + 수집 단계(있으면) 몫과 합치기
    this_stage_codes = error_log.stop_capture()
    all_error_codes = error_log.merge_unique(prior_error_codes or [], this_stage_codes)

    print("\n=== [6] 배포 ===")
    # day_label은 저장이 성공했으면 그 디렉토리 이름을 재사용(날짜 계산 중복 방지), 저장이 실패한 드문 경우만 직접 계산.
    try:
        day_label = os.path.basename(saved_dir) if saved_dir else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        deploy.send_daily_email(day_label, domestic_summarized, international_summarized,
                                 domestic_category_summarized, international_category_summarized,
                                 failed_sources, category_comparison, all_error_codes, category_charts)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-15] - 배포 단계에서 예상 못 한 오류 발생(콘솔 로그의 결과는 그대로 유효함): "
              f"{type(e).__name__} - {e!r}")

    if failed_sources:
        saved_dir_note = f"{saved_dir}/scored.json에도" if saved_dir else "(data/ 파일에는 안 남았지만)"
        print(f"\n[main] 🔴 조치필요 [MN-16] - 이번 실행 실패 소스: {failed_sources} "
              f"({saved_dir_note} failed_sources로 같이 저장됨)")


def run() -> None:
    """
    단일 프로세스로 전체 파이프라인([1] 수집 ~ [6] 배포)을 한 번에 실행 - 로컬 테스트용.
    실제 운영은 Job 체이닝(collect_stage.py + process_stage.py, run-pipline.yml)을 쓴다 -
    거긴 GDELT 수집과 [2] 이후를 별도 Job으로 나눠서 각자 자기 몫의 6시간을 쓰지만, 이
    함수는 한 프로세스 안에서 순서대로 다 돈다(GDELT는 gdelt_collector 자체 기본값인
    5시간까지 쓸 수 있음 - deadline을 안 넘기면 그 모듈 기본값으로 폴백하는 동작 그대로 이용).
    """
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