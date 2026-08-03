"""
storage.py - 5단계 저장 레이어.

data/YYYY-MM-DD/ 아래에 raw.json / scored.json / summary.md 세 파일을 저장한다.
main.py의 run() 마지막 단계에서 save_day()를 호출한다.
(예전엔 data/YYYY-WW/ 주차 디렉토리였음 - 일간 전환하면서 날짜 디렉토리로 변경.
과거 주차 아카이브는 그대로 리포에 남아있지만 새 실행은 더 이상 참조하지 않음.)

** 저장 방식: repo 커밋 (Actions 아티팩트 아님) **
- category_aggregator의 "전일/7일 평균 대비 증감"이 다음 실행 시점에도 checkout된 리포에서
  과거 데이터를 읽어야 하는데, 아티팩트는 보존 기간 후 사라져서 이 용도에 안 맞음
- data/ 자체가 "매일 큐레이션 결과 아카이브"라는 취지에도 리포 히스토리가 자연스러움
run-pipline.yml에 git commit/push 스텝 추가로 반영 (이 모듈은 파일 생성까지만, 커밋/푸시는
워크플로 책임 - 로컬 실행 시에도 파일까지는 정상 생성됨).
"""

import json
import os
from datetime import datetime, timedelta, timezone


def previous_day_dir(base_dir: str = "data", reference: datetime | None = None) -> str:
    """
    전일의 'data/YYYY-MM-DD' 경로 계산만 함("전일 대비 증감"용) - day_dir()과 달리
    디렉토리를 만들지 않는 읽기 전용 조회.
    """
    now = reference or datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    return os.path.join(base_dir, yesterday.strftime("%Y-%m-%d"))


def previous_n_days_dirs(n: int = 7, base_dir: str = "data", reference: datetime | None = None) -> list[str]:
    """
    오늘을 제외한 최근 n일의 'data/YYYY-MM-DD' 경로 리스트("7일 평균 대비 증감"용) -
    day_dir()과 달리 디렉토리를 만들지 않는 읽기 전용 조회. 존재 여부는 호출부가 확인한다
    (일간 전환 직후에는 과거 날짜 디렉토리가 없는 게 정상).
    """
    now = reference or datetime.now(timezone.utc)
    return [os.path.join(base_dir, (now - timedelta(days=i)).strftime("%Y-%m-%d")) for i in range(1, n + 1)]


def day_dir(base_dir: str = "data", reference: datetime | None = None) -> str | None:
    """
    오늘 날짜 기준 'data/YYYY-MM-DD' 경로를 만들고(없으면 생성) 반환.

    디렉토리 생성 실패 시(권한/디스크 등) 예외를 던지는 대신 로그 남기고 None 반환
      - 호출부(save_day)가 저장 전체를 건너뛸 수 있게 함 (이미 끝난 수집/스코어링/요약 결과까지 날리면 안 됨).
    """
    now = reference or datetime.now(timezone.utc)
    path = os.path.join(base_dir, now.strftime("%Y-%m-%d"))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        print(f"[storage] 🔴 조치필요 [ST-01] - 저장 디렉토리 생성 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    return path


def _strip_body(article: dict) -> dict:
    # "_cross_axis_partner"는 main.py의 score()가 국내/해외 교차 매칭 표시를
    # scorer.score_group()에 넘기려고 붙이는 내부용 임시 필드라 raw.json엔 불필요.
    # 정식 결과는 scored.json의 cross_axis_partner로 이미 승격돼 저장됨(save_scored 참고).
    return {k: v for k, v in article.items() if k not in ("body", "_cross_axis_partner")}


def _strip_scored_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "articles"}


def save_raw(directory: str, articles: list[dict]) -> str | None:
    """
    관련성 필터까지 통과해 실제 스코어링에 쓰인 기사 전체를 raw.json으로 저장한다.
    "raw"지만 수집 직후 원본이 아니라 "이번 주 분석에 실제로 쓰인 최종 데이터셋"이라는 의미.
    필터링 전 원본은 지금은 안 남김 (필요해지면 raw_unfiltered.json 등으로 추후 분리 가능).

    파일 쓰기 실패 시 예외 대신 로그+None 반환 (이 시점엔 이미 수집/스코어링/요약이 끝난 뒤라, 저장 하나 실패했다고 전체를 죽여서 콘솔 결과 확인 기회까지 뺏으면 안 됨).
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
    스코어링+요약 결과, 카테고리 집계, 실패 소스, GDELT 시계열을 scored.json 하나로 저장.

    category_distribution을 저장해두는 이유(category_aggregator.py 참고): 다음 날 "전일/7일 평균 대비 증감" 계산에 이번 날 집계가 필요하기 때문.

    domestic/international_by_category(카테고리별 Top N)도 같이 저장.
    scorer.score_by_category가 이미 각 항목에 category 필드를 남겨둬서 그대로 저장만 하면 됨.
    category_distribution(단순 개수 집계)과 이름이 헷갈리지 않게 별도 키로 구분.

    save_raw와 동일하게 파일 쓰기 실패를 로그+None으로 흡수.
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
    """summary.md의 이슈 하나 분량 마크다운 블록."""
    titles = item.get("titles", [])
    rep_title = titles[0] if titles else "(제목 없음)"
    lines = [
        f"### {rep_title}",
        f"- 언급 {item.get('mention_count', 0)}건"
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
    카테고리별 전일 대비 + 7일 평균 대비 증감을 마크다운 줄 리스트로 만든다. 이슈 목록보다 앞,
    문서 맨 앞부분에 배치해서 "이번 날 큰 흐름"을 먼저 보여주는 구성.
    None(비교할 과거 데이터가 하나도 없음 - 일간 전환 직후 등)이면 빈 리스트 반환(섹션 자체를 생략).

    category_comparison은 category_aggregator.compare_with_history()의 반환 형태:
    {"국내": {카테고리: {"today", "yesterday", "delta_yesterday", "avg_7day", "delta_avg7day"}}, "해외": {...}}
    yesterday/avg_7day 각각 그 비교의 재료가 아직 없으면(예: 전환 이틀째라 7일 평균 재료가
    하루치뿐) 개별 카테고리 단위로 None일 수 있어 그런 경우는 "-"로 표시한다.
    """
    if not category_comparison:
        return []
    lines = ["\n## 카테고리별 전일·7일 평균 대비 증감"]
    for axis in ("국내", "해외"):
        axis_data = category_comparison.get(axis, {})
        if not axis_data:
            continue
        lines.append(f"\n### {axis}")
        for category, values in axis_data.items():
            today = values["today"]
            if values["yesterday"] is not None:
                dy = values["delta_yesterday"]
                sign_y = "+" if dy >= 0 else ""
                yesterday_part = f"전일 {values['yesterday']}건({sign_y}{dy})"
            else:
                yesterday_part = "전일 데이터 없음"
            if values["avg_7day"] is not None:
                da = values["delta_avg7day"]
                sign_a = "+" if da >= 0 else ""
                avg_part = f"7일 평균 {values['avg_7day']}건({sign_a}{da})"
            else:
                avg_part = "7일 평균 데이터 없음"
            lines.append(f"- {category}: {today}건 ({yesterday_part}, {avg_part})")
    return lines


def save_summary_md(directory: str, day_label: str, domestic_summarized: list[dict],
                     international_summarized: list[dict],
                     domestic_by_category: dict[str, list[dict]],
                     international_by_category: dict[str, list[dict]],
                     failed_sources: list[str],
                     category_comparison: dict[str, dict[str, dict]] | None = None) -> str | None:
    """
    사람이 바로 읽는 배포용 요약본 마크다운 파일 (요약 유무와 무관하게 원문 링크는 항상 포함).

    국내/해외 각 섹션 밑에 카테고리별 Top N 하위 섹션 추가 (#### 레벨 - 국내/해외 ##보다 한 단계, 개별 이슈 제목 ###과도 안 겹침). 카테고리가 하나도 없으면 하위 섹션 생략.
    category_comparison이 있으면 문서 맨 앞(생성 시각 다음)에 증감 섹션 추가, None이면 생략.

    save_raw/save_scored와 동일하게 파일 쓰기 실패를 로그+None으로 흡수.
    """
    lines = [f"# AI·IT 뉴스 큐레이션 - {day_label}", ""]
    lines.append(f"생성 시각(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.extend(_format_category_comparison_section(category_comparison))

    lines.append("## 국내")
    if domestic_summarized:
        for item in domestic_summarized:
            lines.append("")
            lines.append(_format_issue_section(item))
    else:
        lines.append("\n(오늘 국내 이슈 없음)")
    if domestic_by_category:
        lines.append("\n### 국내 - 카테고리별 Top N")
        lines.extend(_format_category_sections(domestic_by_category))

    lines.append("\n## 해외")
    if international_summarized:
        for item in international_summarized:
            lines.append("")
            lines.append(_format_issue_section(item))
    else:
        lines.append("\n(오늘 해외 이슈 없음)")
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


def save_day(articles: list[dict], domestic_summarized: list[dict],
             international_summarized: list[dict],
             domestic_by_category: dict[str, list[dict]],
             international_by_category: dict[str, list[dict]],
             gdelt_timeline: dict, failed_sources: list[str], category_distribution: dict,
             category_comparison: dict[str, dict[str, dict]] | None = None,
             base_dir: str = "data") -> str | None:
    """
    main.py에서 부르는 단일 진입점. raw.json/scored.json/summary.md를 한 디렉토리에 저장하고 그 경로를 반환한다.
    (예전 이름 save_week - 일간 전환하면서 리네임)

    디렉토리 생성 실패 시 저장 전체를 포기하고 None 반환 (로그는 day_dir이 이미 남김).
    디렉토리는 만들어졌는데 파일 하나가 실패하면 나머지는 계속 저장 시도하고, 끝난 뒤 무엇이 성공/실패했는지 요약 로그를 남긴다.
    """
    directory = day_dir(base_dir)
    if directory is None:
        print("[storage] 🔴 조치필요 [ST-05] - 저장 디렉토리를 만들지 못해 오늘 저장을 건너뜀 "
              "(raw.json/scored.json/summary.md 전부 저장 안 됨)")
        return None

    day_label = os.path.basename(directory)

    saved = {
        "raw.json": save_raw(directory, articles),
        "scored.json": save_scored(directory, domestic_summarized, international_summarized,
                                    domestic_by_category, international_by_category,
                                    gdelt_timeline, failed_sources, category_distribution),
        "summary.md": save_summary_md(directory, day_label, domestic_summarized,
                                       international_summarized,
                                       domestic_by_category, international_by_category,
                                       failed_sources, category_comparison),
    }

    succeeded = [name for name, path in saved.items() if path is not None]
    failed = [name for name, path in saved.items() if path is None]

    if failed:
        print(f"[storage] 🔴 조치필요 [ST-06] - 오늘 저장 일부 실패 - 성공: {succeeded or '없음'} / "
              f"실패: {failed} (실패 원인은 위 개별 로그 참고) -> {directory}/")
    else:
        print(f"[storage] 오늘 저장 완료 -> {directory}/ (raw.json, scored.json, summary.md)")

    return directory