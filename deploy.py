"""
deploy.py - 6단계 배포 레이어.

Gmail SMTP로 이번 주 큐레이션 결과를 HTML 이메일로 발송. main.py의 [6]에서 send_weekly_email() 호출.

** 인증정보 ** GitHub Secrets에서 읽음: SMTP_USER(발신 Gmail), SMTP_APP_PASSWORD(앱 비밀번호),
EMAIL_RECIPIENTS(콤마 구분 수신자).

** 콘텐츠 구성 ** storage.py가 만든 domestic/international_summarized·by_category(scored.json과
동일 데이터)를 받아 summary.md와 같은 구조로 HTML 렌더링. 이메일 클라이언트는 외부 스타일시트를
지원 안 하는 경우가 많아 인라인 스타일만 사용.

** Outlook 렌더링 ** border-radius는 아웃룩 데스크톱(Word 렌더링 엔진)에서 무시되고 각진
사각형으로 보인다 - 레이아웃은 안 깨지고 덜 둥글게만 보이는 수준이라 VML 폴백 없이 그대로 둠.

** 안전 실패 ** storage.py와 같은 방향 - 발송 실패(SMTP 인증 오류 등) 시 예외 대신 로그만 남기고
조용히 실패 (이미 수집/스코어링/요약/저장이 끝난 뒤라 배포 하나로 전체를 죽이면 안 됨).
"""

import html
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# 국내/해외 두 축을 색상으로 구분 - 순위 뱃지/섹션 좌측 보더/카테고리 pill에 공통 사용.
DOMESTIC_ACCENT = "#1a73e8"
INTERNATIONAL_ACCENT = "#7c3aed"
DOMESTIC_PILL_BG = "#e8f0fe"
INTERNATIONAL_PILL_BG = "#f3ebfd"


def _escape(value) -> str:
    return html.escape(str(value))


def _humanize_week_label(week_label: str) -> str:
    """
    week_label(ISO 연도-주차, "2026-31")을 "2026년 7월 5주차"처럼 사람이 읽기 편한 형식으로
    변환 - 헤더/제목 표시용, 저장/비교에는 안 쓰임.

    ISO 주차의 월요일 날짜를 구해서 그 달 1일부터 7일 단위로 끊은 몇 번째 구간인지 계산
    ((day-1)//7+1) - 달력 주(일~토)와는 다른 단순 정의라 별도 라이브러리 없이 결정적으로 계산 가능.
    변환 실패 시 원본 week_label 그대로 반환 (표시 형식 하나 때문에 발송 전체가 막히면 안 됨).
    """
    try:
        year_str, week_str = week_label.split("-")
        monday = date.fromisocalendar(int(year_str), int(week_str), 1)
    except (ValueError, TypeError, IndexError) as e:
        print(f"[deploy] 🟡 주의 [DP-05] - week_label 형식 변환 실패({week_label!r}) - "
              f"원본 그대로 사용: {type(e).__name__} - {e!r}")
        return week_label

    week_of_month = (monday.day - 1) // 7 + 1
    return f"{monday.year}년 {monday.month}월 {week_of_month}주차"


def _format_issue_html(item: dict, rank: int | None = None, accent: str = DOMESTIC_ACCENT) -> str:
    """
    이슈 하나 분량의 HTML 카드 (storage._format_issue_section과 같은 정보).
    rank: 목록 내 순서(None이면 순위 뱃지 없음, 호출부가 enumerate로 1부터 매겨서 넘김).
    accent: 국내(파랑)/해외(보라) 구분 색상.
    """
    titles = item.get("titles", [])
    rep_title = titles[0] if titles else "(제목 없음)"
    extra = f" (그룹 내 추가 {len(titles) - 1}건 생략)" if len(titles) > 1 else ""
    rank_html = ""
    if rank is not None:
        rank_html = (
            f'<span style="display:inline-block; min-width:20px; height:20px; line-height:20px; '
            f'text-align:center; border-radius:50%; background:{accent}; color:#fff; font-size:11px; '
            f'font-weight:bold; margin-right:6px;">{rank}</span>'
        )

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
      <p style="margin:0; font-weight:bold; font-size:14px; color:#111;">{rank_html}{_escape(rep_title)}</p>
      <p style="margin:2px 0 4px 0; font-size:12px; color:#aaa;">점수 {item.get('issue_score', 0):.2f} / 언급 {item.get('mention_count', 0)}건{extra}</p>
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
    # items는 이미 점수순 정렬 상태 - 그대로 1부터 번호만 매김
    body = "".join(_format_issue_html(item, rank=i, accent=accent) for i, item in enumerate(items, start=1))
    return header + body


def _format_category_html(label: str, by_category: dict[str, list[dict]],
                           accent: str = DOMESTIC_ACCENT, pill_bg: str = DOMESTIC_PILL_BG) -> str:
    if not by_category:
        return ""
    header = (f'<h3 style="font-size:16px; color:#222; margin:24px 0 8px 0; padding-left:10px; '
              f'border-left:4px solid {accent};">{_escape(label)} - 카테고리별 Top N</h3>')
    blocks = []
    for category, items in by_category.items():
        blocks.append(
            f'<div><span style="display:inline-block; padding:3px 12px; border-radius:12px; '
            f'background:{pill_bg}; color:{accent}; font-size:12px; font-weight:bold; '
            f'margin:14px 0 8px 0;">{_escape(category)}</span></div>'
        )
        # 카테고리별 별도 Top N이라 전체 순위가 아니라 카테고리 안에서 1부터 다시 매김
        blocks.append("".join(_format_issue_html(item, rank=i, accent=accent) for i, item in enumerate(items, start=1)))
    return header + "".join(blocks)


def _format_category_comparison_html(category_comparison: dict[str, dict[str, dict]] | None) -> str:
    """
    지난주 대비 증감을 이메일 상단에 <table>로 표시 (storage._format_category_comparison_section과
    같은 정보, 렌더링만 표 형식). <table>을 쓰는 이유: 이메일 클라이언트(특히 아웃룩)는
    flex/grid 지원이 들쭉날쭉해도 표는 안정적으로 지원됨.
    """
    if not category_comparison:
        return ""
    blocks = ['<h3 style="font-size:16px; color:#222; margin:20px 0 10px 0;">카테고리별 지난주 대비 증감</h3>']
    axis_colors = {"국내": DOMESTIC_ACCENT, "해외": INTERNATIONAL_ACCENT}
    for axis in ("국내", "해외"):
        axis_data = category_comparison.get(axis, {})
        if not axis_data:
            continue
        accent = axis_colors[axis]
        rows = []
        for category, values in axis_data.items():
            delta = values["delta"]
            sign = "+" if delta >= 0 else ""
            color = "#1a7f37" if delta > 0 else ("#c0392b" if delta < 0 else "#888")
            rows.append(
                '<tr>'
                f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px;">{_escape(category)}</td>'
                f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right;">{values["this_week"]}건</td>'
                f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right; color:#999;">{values["last_week"]}건</td>'
                f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right; color:{color}; font-weight:bold;">{sign}{delta}</td>'
                '</tr>'
            )
        blocks.append(f'<p style="margin:10px 0 4px 0; font-weight:bold; font-size:13px; color:{accent};">{axis}</p>')
        blocks.append(
            '<table style="width:100%; border-collapse:collapse; margin-bottom:8px;">'
            '<tr style="background:#fafafa;">'
            '<th style="text-align:left; padding:6px 8px; font-size:11px; color:#888;">카테고리</th>'
            '<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">이번 주</th>'
            '<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">지난주</th>'
            '<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">증감</th>'
            '</tr>'
            + "".join(rows) +
            '</table>'
        )
    return "".join(blocks)


def render_email_html(week_label: str, domestic_summarized: list[dict], international_summarized: list[dict],
                       domestic_by_category: dict[str, list[dict]],
                       international_by_category: dict[str, list[dict]],
                       failed_sources: list[str],
                       category_comparison: dict[str, dict[str, dict]] | None = None) -> str:
    """
    scored.json과 동일한 데이터로 이메일 본문(HTML)을 만든다. summary.md와 정보량은 같고
    렌더링만 헤더 배너 + 카드형 HTML로 다르다.
    category_comparison이 있으면 제목 바로 아래 배치 (summary.md와 동일한 배치 원칙).
    """
    week_label_human = _humanize_week_label(week_label)

    body_parts = [
        _format_category_comparison_html(category_comparison),
        _format_section_html("국내", domestic_summarized, accent=DOMESTIC_ACCENT),
        _format_category_html("국내", domestic_by_category, accent=DOMESTIC_ACCENT, pill_bg=DOMESTIC_PILL_BG),
        _format_section_html("해외", international_summarized, accent=INTERNATIONAL_ACCENT),
        _format_category_html("해외", international_by_category, accent=INTERNATIONAL_ACCENT, pill_bg=INTERNATIONAL_PILL_BG),
    ]

    if failed_sources:
        body_parts.append(
            f'<p style="margin-top:24px; font-size:12px; color:#c0392b;">'
            f'참고 - 이번 실행에서 실패한 소스: {_escape(", ".join(failed_sources))}</p>'
        )

    # 푸터 - AI 저작 고지 + 자동 발송 안내
    body_parts.append(
        '<p style="margin-top:28px; padding-top:16px; border-top:1px solid #eee; font-size:11px; '
        'color:#aaa; text-align:center;">이 요약은 AI가 자동으로 작성했으며, 실수가 있을 수 있습니다.'
        '<br>이 메일은 AI·IT 뉴스 큐레이션 시스템이 매주 자동으로 발송합니다.</p>'
    )

    return (
        '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Arial, sans-serif; '
        'background:#f2f4f7; padding:24px 0;">'
        '<div style="max-width:640px; margin:0 auto; background:#fff; border-radius:10px; '
        'overflow:hidden; border:1px solid #e5e5e5;">'
        '<div style="background:#0f2f5c; padding:26px 28px; border-radius:10px 10px 0 0;">'
        '<p style="margin:0; font-size:11px; letter-spacing:2px; color:#9fc0ff; font-weight:bold;">NEWSLETTER</p>'
        '<h1 style="margin:6px 0 0 0; font-size:21px; color:#fff; font-weight:bold;">AI·IT 뉴스 큐레이션</h1>'
        f'<p style="margin:5px 0 0 0; font-size:13px; color:#c9dcff;">{_escape(week_label_human)}</p>'
        '</div>'
        '<div style="padding:24px 28px; color:#333;">'
        + "".join(body_parts) +
        '</div>'
        '</div>'
        '</div>'
    )


def send_email(html_content: str, subject: str, recipients: list[str],
               smtp_user: str, smtp_app_password: str) -> bool:
    """Gmail SMTP(587, STARTTLS)로 HTML 이메일 발송. 성공 True, 실패(예외 없이) False."""
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
        print(f"[deploy] 🔴 조치필요 [DP-01] - 이메일 발송 실패: {type(e).__name__} - {e!r}")
        return False

    print(f"[deploy] 이메일 발송 완료 -> {', '.join(recipients)}")
    return True


def send_weekly_email(week_label: str, domestic_summarized: list[dict], international_summarized: list[dict],
                       domestic_by_category: dict[str, list[dict]],
                       international_by_category: dict[str, list[dict]],
                       failed_sources: list[str],
                       category_comparison: dict[str, dict[str, dict]] | None = None) -> bool:
    """
    main.py에서 부르는 단일 진입점. GitHub Secrets에서 인증정보를 읽고, 없으면 안전하게 생략.
    week_label은 main.py의 ISO 연도-주차 형식("2026-31") 그대로 받는다.
    """
    smtp_user = os.environ.get("SMTP_USER")
    smtp_app_password = os.environ.get("SMTP_APP_PASSWORD")
    recipients_raw = os.environ.get("EMAIL_RECIPIENTS")

    if not smtp_user or not smtp_app_password:
        print("[deploy] 🔴 조치필요 [DP-02] - SMTP_USER/SMTP_APP_PASSWORD 없음 - 이메일 발송 생략")
        return False
    if not recipients_raw:
        print("[deploy] 🔴 조치필요 [DP-03] - EMAIL_RECIPIENTS 없음 - 이메일 발송 생략")
        return False

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    if not recipients:
        print("[deploy] 🔴 조치필요 [DP-04] - EMAIL_RECIPIENTS가 비어있음(콤마만 있거나 공백) - 이메일 발송 생략")
        return False

    html_content = render_email_html(week_label, domestic_summarized, international_summarized,
                                      domestic_by_category, international_by_category, failed_sources,
                                      category_comparison)
    week_label_human = _humanize_week_label(week_label)
    subject = f"[AI·IT 뉴스] {week_label_human} 주간 큐레이션"
    return send_email(html_content, subject, recipients, smtp_user, smtp_app_password)