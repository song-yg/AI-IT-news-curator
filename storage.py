"""storage.py - data/YYYY-MM-DD/에 raw.json/scored.json/summary.md 저장(repo 커밋, git push는 워크플로 책임)."""

import json
import os
from datetime import datetime, timedelta, timezone


def previous_day_dir(base_dir: str = "data", reference: datetime | None = None) -> str:
    """전일의 data/YYYY-MM-DD 경로(읽기 전용, 생성 안 함)."""
    now = reference or datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    return os.path.join(base_dir, yesterday.strftime("%Y-%m-%d"))


def previous_n_days_dirs(n: int = 7, base_dir: str = "data", reference: datetime | None = None) -> list[str]:
    """오늘 제외 최근 n일의 data/YYYY-MM-DD 경로 리스트(읽기 전용, 존재 여부는 호출부가 확인)."""
    now = reference or datetime.now(timezone.utc)
    return [os.path.join(base_dir, (now - timedelta(days=i)).strftime("%Y-%m-%d")) for i in range(1, n + 1)]


def day_dir(base_dir: str = "data", reference: datetime | None = None) -> str | None:
    """오늘 날짜 기준 data/YYYY-MM-DD 경로를 만들고 반환. 생성 실패 시 None."""
    now = reference or datetime.now(timezone.utc)
    path = os.path.join(base_dir, now.strftime("%Y-%m-%d"))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        print(f"[storage] 🔴 조치필요 [ST-01] - 저장 디렉토리 생성 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    return path


def _strip_body(article: dict) -> dict:
    return {k: v for k, v in article.items() if k not in ("body", "_cross_axis_partner")}


def _strip_scored_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "articles"}


def save_raw(directory: str, articles: list[dict]) -> str | None:
    """관련성 필터까지 통과한 최종 기사 전체를 raw.json으로 저장."""
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
    """스코어링+요약 결과, 카테고리 집계, 실패 소스, GDELT 시계열을 scored.json으로 저장."""
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
    title_ko = item.get("title_ko")
    display_title = title_ko or rep_title
    lines = [f"### {display_title}"]
    if title_ko and title_ko != rep_title:
        lines.append(f"*원문: {rep_title}*")
    lines.append(
        f"- 언급 {item.get('mention_count', 0)}건"
        + (f" (그룹 내 추가 {len(titles) - 1}건 생략)" if len(titles) > 1 else "")
    )
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
    lines = []
    for category, items in by_category.items():
        lines.append(f"\n#### {category}")
        for item in items:
            lines.append("")
            lines.append(_format_issue_section(item))
    return lines


def _format_category_comparison_section(category_comparison: dict[str, dict[str, dict]] | None) -> list[str]:
    """카테고리별 전일/7일 평균 대비 증감을 마크다운으로. None이면 섹션 생략."""
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
    """사람이 바로 읽는 배포용 요약본 마크다운 파일."""
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
    """main.py에서 부르는 단일 진입점. raw.json/scored.json/summary.md를 저장하고 경로 반환."""
    directory = day_dir(base_dir)
    if directory is None:
        print("[storage] 🔴 조치필요 [ST-05] - 저장 디렉토리를 만들지 못해 오늘 저장을 건너뜀")
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
        print(f"[storage] 🔴 조치필요 [ST-06] - 오늘 저장 일부 실패 - 성공: {succeeded or '없음'} / 실패: {failed} -> {directory}/")
    else:
        print(f"[storage] 오늘 저장 완료 -> {directory}/ (raw.json, scored.json, summary.md)")

    return directory