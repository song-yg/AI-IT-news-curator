"""
storage.py - 5단계 저장 레이어.

data/YYYY-WW/ 아래에 raw.json / scored.json /
summary.md 세 파일을 저장한다. main.py의 run() 마지막 단계에서 이 모듈의
save_week()를 호출한다.

** 저장 방식 결정 (repo 커밋 vs Actions 아티팩트) **
repo 커밋 쪽으로 결정함 - 이유는 두 가지:
1. category_aggregator.py의 "지난주 대비 증감" 비교 기능이 지난주 데이터를
   다음 실행 시점에도 checkout된 리포 안에서 읽어와야 한다. Actions
   아티팩트는 보존 기간이 지나면 사라지고 다음 실행에서 자동으로 안
   딸려오므로 이 용도에 안 맞음.
2. data/ 폴더 자체가 "매주 큐레이션 결과의 아카이브"라는 프로젝트 취지에
   맞게 리포 히스토리에 남는 게 자연스러움.
이 결정에 따라 run-pipline.yml에 git commit/push 스텝을 추가함(이 모듈은
파일 생성까지만 하고, 커밋/푸시는 워크플로 책임 - main.py/이 모듈은 git과
무관하게 동작해서 로컬 실행 시에도 파일까지는 정상 생성됨).

** raw.json에서 제외하는 필드 **
WATT_collector.py가 이미 "body"에 "메모리에서만 사용 - repo 저장 시 제외
(저장 레이어 책임)"이라는 주석을 남겨뒀음 - 스크랩한 기사 본문 전체를 공개
리포에 커밋하는 건 저작권상 바람직하지 않다는 판단으로 보임. 이 모듈에서
그 책임을 이행 - raw.json 저장 전 모든 기사에서 "body" 필드를 제거한다.
naver_collector.py의 "description"은 네이버 API가 제공하는 짧은 요약
스니펫이라(전문 스크랩이 아님) 그대로 유지.

** scored.json에서 제외하는 필드 **
scorer.score_group()이 만드는 각 이슈 항목엔 "articles"(그룹에 속한 원본
기사 dict 전체, WATT body 포함)가 들어있는데, 이건 raw.json에 이미 다
있는 데이터라 scored.json에도 그대로 넣으면 (a) WATT body가 또 한 번 저장돼
위 저작권 문제가 재발하고 (b) 같은 데이터가 두 파일에 중복 저장돼 리포
용량만 커진다. scored.json에는 "articles"를 빼고 issue_score/mention_count/
titles/urls/press_list/summary 등 요약된 필드만 남긴다.
"""

import json
import os
from datetime import datetime, timedelta, timezone


def previous_week_dir(base_dir: str = "data", reference: datetime | None = None) -> str:
    """
    지난주의 'data/YYYY-WW' 경로를 계산만 해서 반환한다("지난주 대비 증감"
    기능용) - week_dir()과 달리 존재 여부와 무관하게 경로 문자열만 계산하고,
    디렉토리를 만들지도 않는다(읽기 전용 조회 용도라 없는 경로를 새로 만들
    이유가 없음 - 있으면 읽고, 없으면 호출부가 "지난주 데이터 없음"으로 처리).

    지난주 계산은 "오늘 날짜 - 7일"의 ISO 주차를 그대로 쓴다 - 연도
    경계(예: 올해 1주차의 지난주 = 작년 마지막 주차)도 날짜 뺄셈이
    자연스럽게 처리해줘서, 주차 번호를 직접 -1 해서 계산하는 것보다
    안전하다(직접 계산은 "1주차 - 1 = 0주차"처럼 존재하지 않는 값이
    나올 위험이 있음).
    """
    now = reference or datetime.now(timezone.utc)
    last_week = now - timedelta(weeks=1)
    iso = last_week.isocalendar()
    return os.path.join(base_dir, f"{iso.year}-{iso.week:02d}")


def week_dir(base_dir: str = "data", reference: datetime | None = None) -> str | None:
    """
    ISO 주차 기준 'data/YYYY-WW' 경로를 만들고(없으면 생성) 반환한다.
    ISO 주차를 쓰는 이유: 그냥 달력 주(일~토 등)와 달리 "월요일 시작 +
    연도 경계에서도 주차가 안 꼬임"이 보장돼서, 이 프로젝트처럼 매주
    월요일 실행을 상정한 파이프라인과 자연스럽게 맞는다.

    디렉토리 생성 자체가 실패하면(권한 문제, 디스크 공간 부족 등) 예외를
    그대로 던지는 대신 로그를 남기고 None을 반환한다 - 호출부(save_week)가
    이걸 보고 저장 전체를 안전하게 건너뛸 수 있게 함 - 저장 실패가 이미
    끝난 수집/스코어링/요약 결과까지 통째로 날려버리면 안 됨.
    """
    now = reference or datetime.now(timezone.utc)
    iso = now.isocalendar()
    path = os.path.join(base_dir, f"{iso.year}-{iso.week:02d}")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        print(f"[storage] 🔴 조치필요 [ST-01] - 저장 디렉토리 생성 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    return path


def _strip_body(article: dict) -> dict:
    # "_cross_axis_partner"는 main.py의 score()가 국내/해외 교차 매칭
    # 표시를 scorer.score_group()에 전달하려고 잠깐 붙이는 내부용 임시
    # 필드(앞의 _가 그 표시) - raw.json에 남길 필요 없어 body와 함께 제거.
    # 정식 결과는 scored.json의 각 이슈 항목에 cross_axis_partner로 이미
    # 승격돼 저장됨(save_scored 참고).
    return {k: v for k, v in article.items() if k not in ("body", "_cross_axis_partner")}


def _strip_scored_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "articles"}


def save_raw(directory: str, articles: list[dict]) -> str | None:
    """
    2.5 관련성 필터까지 통과해 실제로 스코어링에 쓰인 기사 전체(정규화+태깅+
    필터링 완료 상태)를 raw.json으로 저장한다 - "raw"라는 이름이지만 수집
    직후 원본이 아니라 "이번 주 분석에 실제로 쓰인 최종 데이터셋"이라는
    의미. 수집 직후 원본(필터링 전)은 지금은 별도로 안 남긴다 - 필요해지면
    추후 raw_unfiltered.json 등으로 분리 추가 가능.

    파일 쓰기(디스크 공간 부족, 권한 문제, JSON 직렬화 불가능한 값 섞임
    등) 실패 시 예외를 그대로 던지지 않고 로그만 남기고 None을 반환한다 -
    이 시점엔 이미 수집/스코어링/요약이 다 끝난 뒤라, 저장 하나 실패했다고
    전체 실행을 죽여서 콘솔에 남은 결과 확인 기회까지 뺏으면 안 된다는
    판단(save_week이 이 None을 보고 나머지 파일 저장은 계속 시도함).
    """
    cleaned = [_strip_body(a) for a in articles]
    path = os.path.join(directory, "raw.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError) as e:
        print(f"[storage] 🔴 조치필요 [ST-02] - raw.json 저장 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    print(f"[storage] raw.json 저장 완료 ({len(cleaned)}건) -> {path}")
    return path


def save_scored(directory: str, domestic_summarized: list[dict],
                 international_summarized: list[dict],
                 domestic_by_category: dict[str, list[dict]],
                 international_by_category: dict[str, list[dict]],
                 gdelt_timeline: dict, failed_sources: list[str],
                 category_distribution: dict) -> str | None:
    """
    스코어링+요약 결과, 카테고리 전체 집계, 실패 소스, GDELT 시계열 참고
    지표를 scored.json 하나로 묶어 저장한다.

    category_distribution을 여기 같이 저장해두는 이유는 category_aggregator.py
    모듈 docstring에 명시돼 있음 - 다음 주 실행에서 "지난주 대비 증감"을
    계산하려면 지난주 집계 결과가 파일로 남아있어야 하기 때문(이슈 그룹핑
    자체는 매주 새로 도니, 주 단위 비교는 이 파일을 통해서만 가능).

    카테고리별 Top N(국내/해외 각각 {카테고리: [항목, ...]})도
    domestic_by_category/international_by_category로 같이 저장한다 -
    scorer.score_by_category()가 이미 각 항목에 "category" 필드를 남겨두므로
    (main.py의 _regroup_by_category 참고) 여기서는 그대로 저장만 하면 됨.
    "category_distribution"(단순 집계 개수)과 이름이 헷갈리지 않게 구분해서
    키를 붙임 - category_distribution은 개수만 세는 3-보조 지표이고, 이건
    카테고리별 실제 Top N 이슈 목록이라 성격이 다름.

    save_raw와 동일한 이유로 파일 쓰기 실패를 안전하게 흡수한다(로그 +
    None 반환).
    """
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "domestic": [_strip_scored_item(item) for item in domestic_summarized],
        "international": [_strip_scored_item(item) for item in international_summarized],
        "domestic_by_category": {
            category: [_strip_scored_item(item) for item in items]
            for category, items in domestic_by_category.items()
        },
        "international_by_category": {
            category: [_strip_scored_item(item) for item in items]
            for category, items in international_by_category.items()
        },
        "category_distribution": category_distribution,
        "gdelt_timeline": gdelt_timeline,
        "failed_sources": failed_sources,
    }
    path = os.path.join(directory, "scored.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except (OSError, TypeError, ValueError) as e:
        print(f"[storage] 🔴 조치필요 [ST-03] - scored.json 저장 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    print(f"[storage] scored.json 저장 완료 -> {path}")
    return path


def _format_issue_section(item: dict) -> str:
    """summary.md의 이슈 하나 분량 마크다운 블록을 만든다."""
    titles = item.get("titles", [])
    rep_title = titles[0] if titles else "(제목 없음)"
    lines = [
        f"### {rep_title}",
        f"- 점수: {item.get('issue_score', 0):.2f} / 언급 {item.get('mention_count', 0)}건"
        + (f" (그룹 내 추가 {len(titles) - 1}건 생략)" if len(titles) > 1 else ""),
    ]
    if item.get("cross_axis_partner"):
        lines.append(f"- 🔗 반대 축에서도 다뤄짐: {item['cross_axis_partner']}")
    if item.get("summary"):
        lines.append(f"\n{item['summary']}\n")
    else:
        reason = item.get("summary_skipped_reason", "사유 불명")
        lines.append(f"\n(요약 생략 - {reason})\n")

    urls = item.get("urls", [])
    shown = urls[:3]
    more = f" 외 {len(urls) - 3}건" if len(urls) > 3 else ""
    if shown:
        lines.append("원문 링크: " + ", ".join(shown) + more)
    return "\n".join(lines)


def _format_category_sections(by_category: dict[str, list[dict]]) -> list[str]:
    """카테고리별 Top N을 summary.md용 마크다운 줄 리스트로 만든다."""
    lines = []
    for category, items in by_category.items():
        lines.append(f"\n#### {category}")
        for item in items:
            lines.append("")
            lines.append(_format_issue_section(item))
    return lines


def _format_category_comparison_section(category_comparison: dict[str, dict[str, dict]] | None) -> list[str]:
    """
    category_aggregator.compare_with_last_week()의 결과를 summary.md용
    마크다운 줄 리스트로 만든다. 콘솔의 category_aggregator.
    print_aggregate_with_comparison()과 같은 정보를 담는다 - 이슈 목록보다
    위, 문서 맨 앞부분에 배치해서 "이번 주 큰 흐름"을 먼저 보여주는 구성.

    category_comparison이 None이면(지난주 데이터 없음) 빈 리스트 반환 -
    호출부가 이 경우 섹션 자체를 아예 안 넣도록.
    """
    if not category_comparison:
        return []
    lines = ["\n## 카테고리별 지난주 대비 증감"]
    for axis in ("국내", "해외"):
        axis_data = category_comparison.get(axis, {})
        if not axis_data:
            continue
        lines.append(f"\n### {axis}")
        for category, values in axis_data.items():
            delta = values["delta"]
            sign = "+" if delta >= 0 else ""
            lines.append(f"- {category}: {values['this_week']}건 (지난주 {values['last_week']}건, {sign}{delta})")
    return lines


def save_summary_md(directory: str, week_label: str, domestic_summarized: list[dict],
                     international_summarized: list[dict],
                     domestic_by_category: dict[str, list[dict]],
                     international_by_category: dict[str, list[dict]],
                     failed_sources: list[str],
                     category_comparison: dict[str, dict[str, dict]] | None = None) -> str | None:
    """
    사람이 바로 읽을 배포용 요약본. llm_summarizer.print_summaries와 같은
    내용을 마크다운 파일로 남긴다(요약 유무와 무관하게 원문 링크는 항상
    같이 남김).

    문서 작업 시 언더바(_) 등 앞의 이스케이프는 넣지 않는다(마크다운 렌더링
    시 불필요한 백슬래시가 그대로 노출되는 문제 방지 - 프로젝트 방침).

    save_raw/save_scored와 동일한 이유로 파일 쓰기 실패를 안전하게
    흡수한다(로그 + None 반환).

    국내/해외 각 섹션 밑에 "카테고리별 Top N" 하위 섹션을 추가한다(#####
    대신 #### 레벨 - 국내/해외(##)보다 한 단계, 개별 이슈 제목(###)과도
    겹치지 않게 구분). 카테고리가 하나도 없으면(이번 주 그 축에 "기타"
    아닌 카테고리 이슈가 전혀 없었던 경우) 하위 섹션 자체를 생략한다.

    category_comparison(카테고리별 지난주 대비 증감,
    category_aggregator.compare_with_last_week() 결과)이 있으면 문서 맨
    앞(생성 시각 바로 다음)에 "카테고리별 지난주 대비 증감" 섹션을 추가한다.
    None(지난주 데이터 없음)이면 섹션 자체를 생략한다.
    """
    lines = [f"# 사료·축산업 뉴스 큐레이션 - {week_label}", ""]
    lines.append(f"생성 시각(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.extend(_format_category_comparison_section(category_comparison))

    lines.append("## 국내")
    if domestic_summarized:
        for item in domestic_summarized:
            lines.append("")
            lines.append(_format_issue_section(item))
    else:
        lines.append("\n(이번 주 국내 이슈 없음)")
    if domestic_by_category:
        lines.append("\n### 국내 - 카테고리별 Top N")
        lines.extend(_format_category_sections(domestic_by_category))

    lines.append("\n## 해외")
    if international_summarized:
        for item in international_summarized:
            lines.append("")
            lines.append(_format_issue_section(item))
    else:
        lines.append("\n(이번 주 해외 이슈 없음)")
    if international_by_category:
        lines.append("\n### 해외 - 카테고리별 Top N")
        lines.extend(_format_category_sections(international_by_category))

    if failed_sources:
        lines.append("\n## 참고 - 이번 실행에서 실패한 소스")
        lines.append(", ".join(failed_sources))

    content = "\n".join(lines) + "\n"
    path = os.path.join(directory, "summary.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        print(f"[storage] 🔴 조치필요 [ST-04] - summary.md 저장 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    print(f"[storage] summary.md 저장 완료 -> {path}")
    return path


def save_week(articles: list[dict], domestic_summarized: list[dict],
              international_summarized: list[dict],
              domestic_by_category: dict[str, list[dict]],
              international_by_category: dict[str, list[dict]],
              gdelt_timeline: dict, failed_sources: list[str], category_distribution: dict,
              category_comparison: dict[str, dict[str, dict]] | None = None,
              base_dir: str = "data") -> str | None:
    """
    main.py에서 부르는 단일 진입점. raw.json/scored.json/summary.md를 한
    디렉토리에 다 저장하고 그 디렉토리 경로를 반환한다.

    디렉토리 생성 자체가 실패하면 저장을 아예 포기하고 None을 반환한다
    (로그는 week_dir이 이미 남김). 디렉토리는 만들어졌는데 파일 하나가
    실패하는 경우엔 - 나머지 파일 저장은 계속 시도하고, 끝나고 나서 뭐가
    저장되고 뭐가 안 됐는지 요약 로그를 남긴다(부분 성공도 사람이 바로
    알 수 있게).

    domestic_by_category/international_by_category(카테고리별 Top N,
    main.py의 step4_category_llm_summary 결과)도 같이 받아서
    scored.json/summary.md에 반영한다.
    """
    directory = week_dir(base_dir)
    if directory is None:
        print("[storage] 🔴 조치필요 [ST-05] - 저장 디렉토리를 만들지 못해 이번 주 저장을 건너뜀 "
              "(raw.json/scored.json/summary.md 전부 저장 안 됨)")
        return None

    week_label = os.path.basename(directory)

    saved = {
        "raw.json": save_raw(directory, articles),
        "scored.json": save_scored(directory, domestic_summarized, international_summarized,
                                    domestic_by_category, international_by_category,
                                    gdelt_timeline, failed_sources, category_distribution),
        "summary.md": save_summary_md(directory, week_label, domestic_summarized,
                                       international_summarized,
                                       domestic_by_category, international_by_category,
                                       failed_sources, category_comparison),
    }

    succeeded = [name for name, path in saved.items() if path is not None]
    failed = [name for name, path in saved.items() if path is None]

    if failed:
        print(f"[storage] 🔴 조치필요 [ST-06] - 이번 주 저장 일부 실패 - 성공: {succeeded or '없음'} / "
              f"실패: {failed} (실패 원인은 위 개별 로그 참고) -> {directory}/")
    else:
        print(f"[storage] 이번 주 저장 완료 -> {directory}/ (raw.json, scored.json, summary.md)")

    return directory


if __name__ == "__main__":
    # 자체 점검용 - main.py 없이도 저장 로직만 빠르게 확인.
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp()
    try:
        sample_articles = [
            {"title": "구제역 확산", "url": "https://a.com/1", "source": "네이버",
             "press": "yna.co.kr", "published_at": "2026-07-22T00:00:00+00:00"},
            {"title": "Spain Newcastle disease", "url": "https://b.com/2", "source": "WATT",
             "press": "wattagnet.com", "published_at": "2026-07-21T00:00:00+00:00",
             "body": "본문 - raw.json에는 남되 이 필드는 제외돼야 함"},
        ]
        sample_scored = [{
            "issue_score": 3.5, "mention_count": 2, "raw_mention_count": 2,
            "titles": ["구제역 확산", "구제역 추가 발생"],
            "urls": ["https://a.com/1", "https://a.com/2"],
            "press_list": ["yna.co.kr"],
            "articles": sample_articles,  # scored.json엔 빠져야 함
            "summary": "테스트 요약입니다.",
        }]
        # 카테고리별 결과 샘플 (score_by_category가 각 항목에 남기는
        # "category" 필드 포함해서 구성)
        sample_by_category = {
            "질병명": [{
                "issue_score": 3.5, "mention_count": 2, "raw_mention_count": 2,
                "titles": ["구제역 확산", "구제역 추가 발생"],
                "urls": ["https://a.com/1", "https://a.com/2"],
                "press_list": ["yna.co.kr"],
                "articles": sample_articles,  # scored.json엔 빠져야 함
                "summary": "카테고리별 테스트 요약입니다.",
                "category": "질병명",
            }]
        }
        d = save_week(sample_articles, sample_scored, [], sample_by_category, {},
                      {}, [], {"국내": {}, "해외": {}}, base_dir=tmp)
        with open(os.path.join(d, "raw.json"), encoding="utf-8") as f:
            raw = json.load(f)
        assert "body" not in raw[1], "raw.json에 body가 남아있으면 안 됨"
        with open(os.path.join(d, "scored.json"), encoding="utf-8") as f:
            scored = json.load(f)
        assert "articles" not in scored["domestic"][0], "scored.json에 articles가 남아있으면 안 됨"
        assert "질병명" in scored["domestic_by_category"], "카테고리별 결과가 scored.json에 없음"
        assert "articles" not in scored["domestic_by_category"]["질병명"][0], \
            "카테고리별 결과에도 articles가 빠져야 함"
        assert os.path.exists(os.path.join(d, "summary.md"))
        print("[storage] 자체 점검 통과")
    finally:
        shutil.rmtree(tmp)