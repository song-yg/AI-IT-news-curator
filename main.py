"""
main.py
AI·IT 뉴스 큐레이션 시스템의 오케스트레이션 레이어.

6단계 파이프라인 전부 구현 완료:
  1) 수집        -> naver/gdelt collector 순차 실행
  2) 정규화      -> 공통 스키마 통합+ 완전 동일 기사(URL) 제거 + 키워드
                    태깅(keyword_tagger.py) + 관련성 필터(relevance_filter.py)
                    + 카테고리 재분류(relevance_filter.py) + 이슈
                    그룹핑(issue_grouper.py - 1차 사전 매칭 + 2차 BGE-M3
                    임베딩 + 3차 LLM 보조)
  3) 스코어링    -> scorer.py가 이슈(여러 기사 묶음) 단위로 점수 계산 +
                    국내-해외 교차 매칭(🔗) + 카테고리 전체 집계
                    (category_aggregator.py, 지난주 대비 증감 포함) 보조 지표
  4) LLM 요약    -> llm_summarizer.py: (A) 자체 요약 + (A-1) 얇은 재료
                    fallback. (B) 그룹핑 보조는 issue_grouper.stage3_llm_assist
                    담당. 프로바이더 설정(LLM_PROVIDER, 모델명, X-Title 등)은
                    issue_grouper.py에서 그대로 재사용. API 키가 없거나
                    LLM 호출이 실패해도 그 이슈는 "요약 생략, 원문 제목만
                    노출"로 안전하게 fallback
  5) 저장        -> storage.py - data/YYYY-WW/에 raw.json(정규화+필터링된
                    최종 기사 데이터)/scored.json(스코어링+요약 결과,
                    articles 필드는 raw.json과 중복이라 제외)/summary.md
                    (사람이 읽을 배포용 요약본) 저장. WATT body는 저작권상
                    raw.json에서도 제외(storage.py docstring 참고). git
                    커밋/푸시는 워크플로(run-pipline.yml) 책임 - 이 단계는
                    파일 생성까지만.
  6) 배포        -> deploy.py - Gmail SMTP로 국내/해외 Top N + 카테고리별
                    Top N을 HTML 이메일로 발송. 인증정보(SMTP_USER/
                    SMTP_APP_PASSWORD/EMAIL_RECIPIENTS)는 GitHub Secrets에서
                    읽고, 미설정 시 안전하게 발송만 생략(파이프라인 안 죽음).
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

# 주간 Top N (국내/해외 축 각각 몇 건까지 뽑을지) + 카테고리별 Top N
# (카테고리 축에서도 별도로 몇 건까지 뽑을지) - 둘 다 환경변수로 조정
# 가능하게 함(2026-07-28, 담당자 요청). 미설정 시 기존과 동일한 기본값
# (5 / 1)으로 동작 - 하위호환.
#
# CATEGORY_TOP_N 관련: LLM 요약 호출이 카테고리 수(최대 9개) x 국내/해외(2)
# x 이 값만큼 늘어나므로 무작정 올리면 API 호출이 급격히 늘어남 - 특히
# OpenRouter 무료 티어 일일 요청 한도(위 "4. LLM 요약" 섹션 참고)를 고려해서
# 조정할 것.
TOP_N = int(os.environ.get("TOP_N") or 5)
CATEGORY_TOP_N = int(os.environ.get("CATEGORY_TOP_N") or 1)


# ---------------------------------------------------------------------------
# 1) 수집 레이어
# ---------------------------------------------------------------------------

def run_collectors() -> tuple[list[dict], dict, list[str]]:
    """
    naver/gdelt collector를 순서대로 실행한다.

    소스별 독립 실행 구조 - 각 collector 호출을 개별 try/except로 감싸서 하나가 완전히 죽어도(예: import 실패, 예상 밖 예외) 나머지 소스는 계속 진행한다.
    각 collector 내부에도 이미 더 세밀한 단위(키워드별)의 방어가 있지만, 여기 main.py 레벨의 try/except는 "collector 모듈 자체가 통째로 실패하는 경우"에 대한 마지막 방어선이다.

    반환값:
      all_articles: naver+gdelt 기사를 하나로 합친 리스트 (아직 정규화 전 원본 스키마 - 이미 두 collector가 공통 스키마를 지키므로 합치기만 하면 됨)
      gdelt_timeline: GDELT 시계열 데이터 (스코어링에는 안 들어가고 참고 지표 전용 - storage.py가 scored.json에 그대로 저장)
      failed_sources: 실패한 소스 이름 목록 (storage.py/deploy.py가 결과물에 반영)
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
    "완전 동일 기사 제거": 같은 URL이 중복 수집된 경우만 제거.
    (이슈 그룹핑과는 다른 개념. 여긴 정말 똑같은 기사가 두 번 들어온 경우만 거른다 - 예: 페이지네이션 겹침, 재실행 등).

    첫 번째로 본 URL을 유지하고 이후 중복은 버린다 (순서 유지를 위해 dict를 순서 보존 집합처럼 사용).
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
    이슈 그룹핑(issue_grouper.group_issues) + 국내/해외 개별 랭킹(Top N)까지 수행.

    ** 그룹핑을 먼저, 축 분리는 그 다음 (설계 결정) **
    이슈 매칭 범위는 "전체 기사 벡터 x 전체 기사 벡터"(국내-국내 / 해외-해외 / 국내-해외 전부 포함)여야 한다.
    국내/해외를 먼저 나눠서 각각 그룹핑하면 국내-해외 교차 매칭이 구조적으로 아예 발생할 수 없게 된다.
    그래서 순서는: ① 전체 기사를 대상으로 group_issues()를 한 번 호출 -> ② 그 결과 그룹들을 국내/해외 축으로 "나눠서" scorer에 넘긴다.

    ** 국내-해외 교차 매칭된 그룹의 처리 **
    group_issues가 만든 그룹 하나가 국내(네이버)·해외(WATT/GDELT) 기사를 동시에 포함할 수 있다.
    "양쪽 리스트 모두에서 노출하되 하나의 점수로 합치지는 않음, 정규화·환산 없이 각 축의 원본 신호를 그대로 보존"하는 원칙대로,
    이런 그룹은 국내 축 스코어링엔 그 그룹 안의 네이버 기사만, 해외 축 스코어링엔 그 그룹 안의 WATT/GDELT 기사만 걸러서 넘긴다 - 각 축의 issue_score가 그 축 안에서의 원본 신호만 반영하게 하기 위함이다.
    "이 이슈가 다른 축에서도 다뤄졌다"는 🔗 표시로 화면에 노출된다 - 아래 domestic_part/international_part 둘 다 비어있지 않은 경우에만 서로에게 "_cross_axis_partner"를 붙이고, scorer.score_group()이 이를 cross_axis_partner 정식 필드로 승격한다.

    ** GDELT 소스의 한국어 기사는 국내로 재분류 **
    국내/해외 분리를 소스(source)만으로 판단하면, GDELT가 번역 인덱싱 기능 때문에 한국어 기사를 물어오는 경우
    (예: 영문 키워드 검색에 한국어 동음이의 기사가 걸리는 경우) 국내 이슈가 "해외" 축으로 잘못 분류된다.
    `scorer._is_korean_gdelt_article()`로 재분류한다 - GDELT 응답에 붙어있는 `language` 필드("Korean" 등,
    GDELT가 크롤링 시점에 자체 판별한 원본 신호)를 우선 쓰고,
    이 필드가 없는 예외적인 경우에만 제목의 한글 유니코드 비율 체크로 안전하게 fallback한다.

    같은 국내/해외 판별 로직은 category_aggregator.py가 쓰는 `scorer.split_domestic_international()`도 함께 쓴다
    - Top N 스코어링과 카테고리 집계가 서로 다른 기준으로 국내/해외를 나누지 않도록 단일 소스(scorer._is_korean_gdelt_article)로 통일돼 있다.

    model: sentence_transformers.SentenceTransformer 인스턴스, 또는 None.
           None이면 issue_grouper.group_issues가 2차(임베딩) 없이 1차 결과만으로 안전하게 fallback한다 (아래 _load_embedding_model 참고).

    ** 카테고리별 Top N도 함께 계산해서 반환 **
    "주간 Top N + 카테고리별 Top N" 목표에 맞춰, 국내/해외 축을 나눈 domestic_groups/international_groups 각각에 대해 scorer.score_by_category()로 카테고리별 Top N도 같이 계산한다.
    카테고리 축도 국내/해외 축과 독립적으로 유지(교차 안 함)
    - 즉 "국내 질병명 Top N", "해외 질병명 Top N"처럼 최대 카테고리 9개 x 국내/해외 2개 = 18개 리스트가 나올 수 있다.
    N 값은 CATEGORY_TOP_N 상수로 조정 가능.
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
            # 같은 그룹이 국내/해외 양쪽에 걸쳐 있으면, 각 축의 대표 기사(scorer.score_group이 titles[0]로 쓰는 group[0])에 반대 축 대표 제목을 "_cross_axis_partner"로 붙여둔다.
            # 앞에 _를 붙인 이유는 내부 전달용 임시 필드임을 표시하기 위함(storage.py가 raw.json 저장 시 body와 함께 제거)
            # - scorer.score_group()이 이 필드를 읽어 정식 필드 cross_axis_partner로 승격시킨다.
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
    BGE-M3 임베딩 모델을 실행당 한 번만 로드해서 score() 단계에 주입한다.

    한 번만 로드하는 이유: issue_grouper.stage2_group의 docstring에 이미 명시돼 있음 - "모델 로드 자체가 무거운 작업이라, 기사 배치마다 매번 새로 로드하면 안 되기 때문...
    호출하는 쪽(main.py)에서 한 번만 로드해서 넘겨주는 구조로 설계".
    main.py에서는 국내/해외 두 축을 스코어링하지만 그룹핑 자체는 score() 안에서 한 번만(전체 기사 대상) 일어나므로, 이 함수도 run() 전체를 통틀어 딱 한 번만 호출하면 된다.

    모델 로드 실패(최초 실행 시 다운로드 실패, 패키지 미설치, 캐시 문제 등) 시에도 전체 파이프라인이 죽지 않도록 여기서 예외를 잡아 None을 반환한다.
    issue_grouper.group_issues(articles, model=None)이 이미 "2차(임베딩) 생략, 1차 사전 매칭 결과만 사용"으로 안전하게 fallback하도록 설계돼 있으므로(issue_grouper.py group_issues 참고),
    이 함수의 실패가 소스별 독립 실행 구조와 같은 철학으로 전체 중단 없이 흡수된다.
    (완전한 자동 복구는 아님 - 다음 실행에서 모델 로드가 다시 성공하길 기대하는 정도의 완화책).
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
# 4)~6) 아직 미구현 - 자리만 표시
# ---------------------------------------------------------------------------

def _regroup_by_category(items: list[dict]) -> dict[str, list[dict]]:
    """
    scorer.score_by_category()가 붙여둔 item["category"]를 기준으로, 평평한 리스트를 다시 {카테고리: [항목, ...]} 형태로 묶는다.
    dict는 카테고리가 처음 등장한 순서를 그대로 유지한다.
    (파이썬 dict가 삽입 순서를 보존하므로 - print/저장 시 순서가 뒤섞이지 않게 하기 위함).
    """
    regrouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        regrouped[item.get("category", "미상")].append(item)
    return dict(regrouped)


def step4_category_llm_summary(domestic_category_ranked: dict[str, list[dict]],
                                international_category_ranked: dict[str, list[dict]],
                                ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """
    step4_llm_summary와 같은 (A)/(A-1) 로직을 카테고리별 Top N 결과에도 적용한다.

    domestic_category_ranked/international_category_ranked는 각각{카테고리: [항목, ...]} 형태(scorer.score_by_category 반환값)라서,
    llm_summarizer.summarize_top_issues가 기대하는 "평평한 리스트"로 한 번 합쳤다가(카테고리 축마다 세션 하나로 묶어 API 호출 오버헤드를 줄이는 이점도 있음
    - llm_summarizer.py의 세션 재사용 참고) 요약이 끝나면 다시 카테고리별로 묶어서 돌려준다(_regroup_by_category).
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
    (A) 자체 요약 + (A-1) 얇은 재료 fallback.
    실제 로직은 llm_summarizer.py에 있고, 이 함수는 국내/해외 축을 각각 넘겨주는 얇은 호출부다.
    (B) 그룹핑 보조는 issue_grouper.stage3_llm_assist가 처리한다.
    - 여기서는 (A)/(A-1)만 다룬다.

    domestic_ranked/international_ranked는 score()에서 이미 TOP_N(환경변수 미설정 시 기본 5)으로 제한된 상태로 들어온다 (여기서 추가로 자르지 않음).

    반환값은 입력과 같은 형태(list[dict])에 "summary"/"summary_skipped_reason" 필드가 추가된 것
    - storage.py 저장 단계에 그대로 넘긴다.
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
    # 이 단계부터 [4-보조]까지는 각각 안전망을 둔다
    # - 예상 못 한 예외(예: 리포 파일 동기화 문제로 실제 배포된 코드에 함수가 없는 경우 등)가 나도 그 단계만 안전한 기본값으로 넘어가고,
    # 이미 모은 articles는 그대로 살려서 [5] 저장/[6] 배포까지 도달하게 한다.
    # storage.py/deploy.py 호출부에 이미 있던 것과 같은 방향(소스별 독립 실행 구조)
    # - 한 단계의 예상 못 한 실패가 그 이전까지 쌓은 결과 전체를 날려버리면 안 됨.
    try:
        articles = normalize(articles)
        keyword_tagger.tag_articles(articles)
        keyword_tagger.print_category_distribution(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-05] - [2] 정규화/태깅 단계에서 예상 못 한 오류 발생 - 원본 기사 그대로 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.5] 관련성 필터 ===")
    # 키워드 매칭만으로는 못 거르는 오매칭(동음이의어, 기관명 일부로만 등장, 각주성 언급 등)을 LLM이 제목/요약 맥락으로 판단해 걸러낸다.
    # - 이후 단계(임베딩 계산, 3차 LLM 그룹핑 보조)의 대상도 함께 줄어드는 효과가 있음.
    try:
        articles = relevance_filter.filter_articles(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-06] - [2.5] 관련성 필터 단계에서 예상 못 한 오류 발생 - 필터링 없이 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.6] 카테고리 재분류 ===")
    # keyword_tagger(사전 매칭)와 relevance_filter(LLM 관련성 판단)는 기준이 서로 달라서, 사전엔 안 걸려 category="기타"로 붙었는데 relevance_filter 가 "관련 있음"으로 확정하는 기사가 생길 수 있음.
    # - 이 기사는 필터는 통과하는데 category는 계속 "기타"라, "기타"를 제외하는 카테고리별 Top N에는 영원히 못 들어가는 공백이 있었음.
    # 이 단계로 그 기사들만 다시 LLM에 물어 재분류한다.
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
    # 이슈 그룹핑이 "동일 사건만" 묶는 좁은 정의라 큰 트렌드가 개별 이슈로 흩어져 보이는 공백을 메우는 거친(coarse) 보조 지표
    # - 순위(Top N)와는 별개로, 카테고리 자체가 이번 주 몇 건 다뤄졌는지만 보여준다.
    # (category_aggregator.py 모듈 docstring 참고)
    try:
        category_distribution = category_aggregator.aggregate(articles)
        # 지난주 scored.json이 있으면 카테고리별 증감을 같이 보여준다.
        # 지난주 데이터가 없으면(첫 실행 등) compare_with_last_week가 안전하게 None을 반환하고, 아래 출력 함수는 그 경우 증감 없이 출력한다.
        category_comparison = category_aggregator.compare_with_last_week(category_distribution)
        category_aggregator.print_aggregate_with_comparison(category_distribution, category_comparison)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-09] - [3-보조] 카테고리 집계 단계에서 예상 못 한 오류 발생 - 이번 주는 집계 없이 진행: "
              f"{type(e).__name__} - {e!r}")
        category_distribution, category_comparison = {}, None

    # 4~6단계
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
    # 수동 실행(workflow_dispatch)은 테스트/임시 확인용이라,
    # 저장을 그대로 하면 "지난주 대비 증감" 비교 기준(category_aggregator.compare_with_ last_week)이 실제 정식 주간 실행이 아닌 테스트 데이터로 오염될 수 있다
    # - 예: 토요일에 테스트로 키워드 1개만 돌렸는데 그게 다음 월요일 정식 실행의 "지난주 실적"으로 잘못 비교되는 사고.
    # GITHUB_EVENT_NAME은 GitHub Actions가 모든 스텝에 자동으로 주는 기본 환경변수라 워크플로 파일 수정 없이 바로 참조 가능(로컬 실행 등 이 값이 없는 환경에서는 안전하게 "수동 아님"으로 취급돼 기존과 동일하게 저장됨).
    is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if is_manual_run:
        print("[main] 수동 실행(workflow_dispatch)이라 저장을 건너뜁니다 - "
              "'지난주 대비 증감' 비교 기준 오염 방지(콘솔에 출력된 이번 실행 결과는 그대로 확인 가능, data/에는 안 남음)")
        saved_dir = None
    else:
        # storage.py 내부는 이미 파일 단위로 안전하게 실패를 흡수하도록 만들었지만(storage.py docstring 참고),
        # 예상 못 한 예외까지 완벽히 막을 순 없으므로 소스별 독립 실행 구조와 같은 방향으로 마지막 방어선을 하나 더 둔다
        # - 저장이 통째로 실패해도 이미 콘솔에 다 출력된 이번 실행 결과(수집/스코어링/요약)는 그대로 남는다.
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
    # storage.py와 같은 방향으로 마지막 방어선을 둔다.
    # - 배포 실패가 이미 끝난 나머지 단계 결과에 영향을 주면 안 됨.
    # week_label은 저장이 성공했으면 그 디렉토리 이름을 그대로 쓰고(주차 계산 로직 중복 방지),
    # 저장 자체가 실패한 드문 경우에만 직접 계산한다.
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