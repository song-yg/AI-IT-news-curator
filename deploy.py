"""deploy.py - Gmail SMTP로 오늘 큐레이션 결과를 HTML 이메일로 발송(main.py [6]에서 send_daily_email() 호출)."""

import html
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

DOMESTIC_ACCENT = "#1a73e8"
INTERNATIONAL_ACCENT = "#7c3aed"
DOMESTIC_PILL_BG = "#e8f0fe"
INTERNATIONAL_PILL_BG = "#f3ebfd"

HEADER_BG = "#0f2f5c"


def _escape(value) -> str:
    return html.escape(str(value))


def _humanize_day_label(day_label: str) -> str:
    """"2026-07-31" -> "2026년 7월 31일". 변환 실패 시 원본 그대로."""
    try:
        day = date.fromisoformat(day_label)
    except (ValueError, TypeError) as e:
        print(f"[deploy] 🟡 주의 [DP-01] - day_label 형식 변환 실패({day_label!r}): {type(e).__name__} - {e!r}")
        return day_label
    return f"{day.year}년 {day.month}월 {day.day}일"


def _format_issue_html(item: dict, rank: int | None = None, accent: str = DOMESTIC_ACCENT) -> str:
    """이슈 하나 분량의 HTML 카드."""
    titles = item.get("titles", [])
    rep_title = titles[0] if titles else "(제목 없음)"
    title_ko = item.get("title_ko")
    display_title = title_ko or rep_title
    rank_html = ""
    if rank is not None:
        rank_html = (
            f'<span style="display:inline-block; min-width:20px; height:20px; line-height:20px; '
            f'text-align:center; border-radius:50%; background:{accent}; color:#fff; font-size:11px; '
            f'font-weight:bold; margin-right:6px;">{rank}</span>'
        )

    orig_title_html = ""
    if title_ko and title_ko != rep_title:
        orig_title_html = (f'<p style="margin:2px 0 4px 0; font-size:11px; color:#aaa; '
                           f'font-style:italic;">원문: {_escape(rep_title)}</p>')

    extra_html = ""
    if len(titles) > 1:
        extra_html = (f'<p style="margin:2px 0 4px 0; font-size:11px; color:#aaa;">'
                      f'(총 {len(titles)}건 기사를 종합)</p>')

    cross_html = ""
    if item.get("cross_axis_partner"):
        cross_html = (f'<p style="margin:2px 0 4px 0; font-size:12px; color:{accent};">'
                      f'🔗 반대 축에서도 다뤄짐: {_escape(item["cross_axis_partner"])}</p>')

    if item.get("summary"):
        body_html = f'<p style="margin:4px 0 8px 0; color:#333; font-size:13px; line-height:1.5;">{_escape(item["summary"])}</p>'
    else:
        reason = item.get("summary_skipped_reason", "사유 불명")
        body_html = (f'<p style="margin:4px 0 8px 0; color:#999; font-size:12px; font-style:italic;">'
                     f'(요약 생략 - {_escape(reason)})</p>')

    urls = item.get("urls", [])
    shown = urls[:3]
    more = f" 외 {len(urls) - 3}건" if len(urls) > 3 else ""
    links_html = ""
    if shown:
        link_tags = ", ".join(f'<a href="{_escape(u)}" style="color:{accent}; text-decoration:none;">원문</a>' for u in shown)
        links_html = f'<p style="margin:0; font-size:12px; color:#888;">원문 링크: {link_tags}{more}</p>'

    return f"""
    <div style="margin-bottom:12px; padding:12px 14px; border:1px solid #eee; border-radius:8px; background:#fff;">
      <p style="margin:0; font-weight:bold; font-size:14px; color:#111;">{rank_html}{_escape(display_title)}</p>
      {orig_title_html}
      {extra_html}
      {cross_html}
      {body_html}
      {links_html}
    </div>
    """


def _format_section_html(title: str, items: list[dict], accent: str = DOMESTIC_ACCENT) -> str:
    header = (f'<h3 style="font-size:16px; color:#222; margin:20px 0 10px 0; padding-left:10px; '
              f'border-left:4px solid {accent};">{_escape(title)}</h3>')
    if not items:
        return header + '<p style="color:#999; font-size:13px;">(이번 주 이슈 없음)</p>'
    body = "".join(_format_issue_html(item, rank=i, accent=accent) for i, item in enumerate(items, start=1))
    return header + body


def _format_category_html(label: str, by_category: dict[str, list[dict]],
                           accent: str = DOMESTIC_ACCENT, pill_bg: str = DOMESTIC_PILL_BG) -> str:
    if not by_category:
        return ""
    blocks = []
    for category, items in by_category.items():
        blocks.append(
            f'<div><span style="display:inline-block; padding:4px 14px; border-radius:14px; '
            f'background:{pill_bg}; color:{accent}; font-size:15px; font-weight:bold; '
            f'margin:14px 0 8px 0;">{_escape(category)}</span></div>'
        )
        blocks.append("".join(_format_issue_html(item, rank=i, accent=accent) for i, item in enumerate(items, start=1)))
    header = (f'<h3 style="font-size:16px; color:#222; margin:24px 0 8px 0; padding-left:10px; '
              f'border-left:4px solid {accent};">{_escape(label)}</h3>')
    return header + "".join(blocks)


def _format_category_comparison_axis_html(axis_data: dict[str, dict] | None, accent: str) -> str:
    """카테고리별 전일/7일 평균 대비 증감 표(축 하나 분량). None 값은 "-"로 표시."""
    if not axis_data:
        return '<p style="font-size:13px; color:#999; margin:4px 0;">(비교할 과거 데이터 없음)</p>'
    rows = []
    for category, values in axis_data.items():
        if values["delta_yesterday"] is not None:
            dy = values["delta_yesterday"]
            sign_y = "+" if dy >= 0 else ""
            color_y = "#1a7f37" if dy > 0 else ("#c0392b" if dy < 0 else "#888")
            yesterday_cell = f'{values["yesterday"]}건'
            delta_y_cell = f'<span style="color:{color_y}; font-weight:bold;">{sign_y}{dy}</span>'
        else:
            yesterday_cell = "-"
            delta_y_cell = "-"
        if values["delta_avg7day"] is not None:
            da = values["delta_avg7day"]
            sign_a = "+" if da >= 0 else ""
            color_a = "#1a7f37" if da > 0 else ("#c0392b" if da < 0 else "#888")
            avg_cell = f'{values["avg_7day"]}건'
            delta_a_cell = f'<span style="color:{color_a}; font-weight:bold;">{sign_a}{da}</span>'
        else:
            avg_cell = "-"
            delta_a_cell = "-"
        rows.append(
            '<tr>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px;">{_escape(category)}</td>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right;">{values["today"]}건</td>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right; color:#999;">{yesterday_cell}</td>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right;">{delta_y_cell}</td>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right; color:#999;">{avg_cell}</td>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right;">{delta_a_cell}</td>'
            '</tr>'
        )
    return (
        '<table style="width:100%; border-collapse:collapse;">'
        '<tr style="background:#fafafa;">'
        '<th style="text-align:left; padding:6px 8px; font-size:11px; color:#888;">카테고리</th>'
        '<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">오늘</th>'
        '<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">전일</th>'
        '<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">전일比</th>'
        '<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">7일평균</th>'
        '<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">평균比</th>'
        '</tr>'
        + "".join(rows) +
        '</table>'
    )


def _two_column_table(left_html: str, right_html: str) -> str:
    """국내/해외 두 블록을 좌우로 배치(구형 Outlook 호환 위해 table 사용)."""
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%; border-collapse:collapse; table-layout:fixed;">
      <tr>
        <td width="50%" valign="top" style="padding-right:14px;">{left_html}</td>
        <td width="50%" valign="top" style="padding-left:14px;">{right_html}</td>
      </tr>
    </table>
    """


def render_email_html(day_label: str, domestic_summarized: list[dict], international_summarized: list[dict],
                       domestic_by_category: dict[str, list[dict]],
                       international_by_category: dict[str, list[dict]],
                       failed_sources: list[str],
                       category_comparison: dict[str, dict[str, dict]] | None = None,
                       error_codes: list[str] | None = None,
                       category_charts: dict[str, str] | None = None) -> str:
    """scored.json 데이터로 이메일 본문 HTML 생성."""
    header_html = f"""
    <div style="background:{HEADER_BG}; padding:26px 32px; border-radius:10px 10px 0 0;">
      <p style="margin:0; font-size:11px; letter-spacing:2px; color:#9fc0ff; font-weight:bold;">NEWSLETTER</p>
      <h1 style="margin:6px 0 0 0; font-size:22px; color:#fff; font-weight:bold;">AI·IT 뉴스 큐레이션</h1>
      <p style="margin:5px 0 0 0; font-size:13px; color:#c9dcff;">{_escape(_humanize_day_label(day_label))}</p>
    </div>
    """

    section_header = lambda text: f'<h2 style="font-size:18px; color:#111; margin:26px 0 12px 0;">{_escape(text)}</h2>'

    parts = [
        '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Arial, sans-serif; '
        'background:#f2f4f7; padding:24px 0;">',
        '<div style="max-width:1000px; margin:0 auto; background:#fff; border-radius:10px; '
        'overflow:hidden; border:1px solid #e5e5e5;">',
        header_html,
        '<div style="padding:24px 32px; color:#333;">',
    ]

    if category_comparison:
        parts.append(section_header("카테고리별 전일·7일 평균 대비 증감"))
        parts.append(_two_column_table(
            _format_category_comparison_axis_html(category_comparison.get("국내"), DOMESTIC_ACCENT),
            _format_category_comparison_axis_html(category_comparison.get("해외"), INTERNATIONAL_ACCENT),
        ))

    if category_charts:
        parts.append(section_header("카테고리별 최근 7일 추이"))
        chart_cell = lambda axis: (
            f'<img src="data:image/png;base64,{category_charts[axis]}" '
            f'style="width:100%; max-width:480px; height:auto; display:block;" alt="{axis} 카테고리 추이">'
            if axis in category_charts else '<p style="font-size:13px; color:#999; margin:4px 0;">(그래프 없음)</p>'
        )
        parts.append(_two_column_table(chart_cell("국내"), chart_cell("해외")))

    parts.append(section_header("오늘의 Top 이슈"))
    parts.append(_two_column_table(
        _format_section_html("국내", domestic_summarized, accent=DOMESTIC_ACCENT),
        _format_section_html("해외", international_summarized, accent=INTERNATIONAL_ACCENT),
    ))

    parts.append(section_header("카테고리별 Top N"))
    parts.append(_two_column_table(
        _format_category_html("국내", domestic_by_category, accent=DOMESTIC_ACCENT, pill_bg=DOMESTIC_PILL_BG),
        _format_category_html("해외", international_by_category, accent=INTERNATIONAL_ACCENT, pill_bg=INTERNATIONAL_PILL_BG),
    ))

    if failed_sources:
        parts.append(
            f'<p style="margin-top:24px; font-size:12px; color:#c0392b;">'
            f'참고 - 이번 실행에서 실패한 소스: {_escape(", ".join(failed_sources))}</p>'
        )

    parts.append(
        '<p style="margin-top:28px; padding-top:16px; border-top:1px solid #eee; '
        'font-size:11px; color:#aaa; text-align:center;">'
        '이 메일은 AI·IT 뉴스 큐레이션 시스템이 매일 자동으로 발송합니다.<br>'
        '이 요약은 AI가 자동으로 작성했으며, 실수가 있을 수 있습니다. '
        '정확한 내용은 원문 링크를 확인해주세요.</p>'
    )

    if error_codes:
        parts.append(
            f'<p style="margin-top:6px; font-size:9px; color:#ddd; text-align:center;">'
            f'{_escape(", ".join(error_codes))}</p>'
        )

    parts.append('</div>')
    parts.append('</div>')
    parts.append('</div>')
    return "".join(parts)


def send_email(html_content: str, subject: str, recipients: list[str],
               smtp_user: str, smtp_app_password: str) -> bool:
    """Gmail SMTP(587, STARTTLS)로 HTML 이메일 발송."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_app_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
    except (smtplib.SMTPException, OSError) as e:
        print(f"[deploy] 🔴 조치필요 [DP-02] - 이메일 발송 실패: {type(e).__name__} - {e!r}")
        return False

    print(f"[deploy] 이메일 발송 완료 -> {', '.join(recipients)}")
    return True


def send_daily_email(day_label: str, domestic_summarized: list[dict], international_summarized: list[dict],
                      domestic_by_category: dict[str, list[dict]],
                      international_by_category: dict[str, list[dict]],
                      failed_sources: list[str],
                      category_comparison: dict[str, dict[str, dict]] | None = None,
                      error_codes: list[str] | None = None,
                      category_charts: dict[str, str] | None = None) -> bool:
    """main.py에서 부르는 단일 진입점. 인증정보 없으면 안전하게 생략."""
    smtp_user = os.environ.get("SMTP_USER")
    smtp_app_password = os.environ.get("SMTP_APP_PASSWORD")
    recipients_raw = os.environ.get("EMAIL_RECIPIENTS")

    if not smtp_user or not smtp_app_password:
        print("[deploy] 🔴 조치필요 [DP-03] - SMTP_USER/SMTP_APP_PASSWORD 없음 - 이메일 발송 생략")
        return False
    if not recipients_raw:
        print("[deploy] 🔴 조치필요 [DP-04] - EMAIL_RECIPIENTS 없음 - 이메일 발송 생략")
        return False

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    if not recipients:
        print("[deploy] 🔴 조치필요 [DP-05] - EMAIL_RECIPIENTS가 비어있음 - 이메일 발송 생략")
        return False

    html_content = render_email_html(day_label, domestic_summarized, international_summarized,
                                      domestic_by_category, international_by_category, failed_sources,
                                      category_comparison, error_codes, category_charts)
    day_label_human = _humanize_day_label(day_label)
    subject = f"[AI·IT 뉴스] {day_label_human} 일간 큐레이션"
    return send_email(html_content, subject, recipients, smtp_user, smtp_app_password)