"""
deploy.py - 6단계 배포 레이어.

Gmail SMTP로 이번 주 큐레이션 결과를 HTML 이메일로 발송한다. main.py의
[6] 배포 단계에서 이 모듈의 send_weekly_email()을 호출한다.

** Gmail SMTP를 선택한 이유 **
SendGrid/Mailgun 같은 무료 트랜잭션 이메일 API는 도메인 인증(SPF/DKIM)
없이 기본 발신 주소로 보내면 스팸함으로 분류될 확률이 높음 - 이제 막 만든
발신자라 평판이 전혀 없는 상태로 시작하기 때문. Gmail SMTP는 실제 Gmail
계정에서 Gmail 서버를 통해 보내는 거라 SPF/DKIM이 자동으로 유효하고
발신처 신뢰도도 이미 높아 이 문제가 훨씬 덜함 - 특히 지금처럼 수신자가
소수일 때는 도메인 설정 같은 번거로운 과정 없이 바로 쓸 수 있는 쪽이 낫다.

** 인증정보 관리 **
GitHub Secrets(민감정보라 Variables 아님)에서 읽는다:
  SMTP_USER          발신용 Gmail 주소
  SMTP_APP_PASSWORD  Gmail 앱 비밀번호(일반 로그인 비밀번호 아님)
  EMAIL_RECIPIENTS   수신자 이메일, 콤마로 구분

** 콘텐츠 구성 **
storage.py가 이미 만들어둔 domestic_summarized/international_summarized/
domestic_by_category/international_by_category(scored.json과 동일한
데이터)를 그대로 받아서 summary.md와 같은 구조(요약 유무와 무관하게 원문
링크는 항상 같이 노출)로 HTML을 렌더링한다. 이메일 클라이언트는 외부
스타일시트를 지원 안 하는 경우가 많아 인라인 스타일만 사용.

** 안전 실패 원칙 **
storage.py와 같은 방향 - 이메일 발송이 실패해도(SMTP 인증 오류, 네트워크
문제 등) 예외를 그대로 던지지 않고 로그만 남기고 조용히 실패한다. 이 시점엔
이미 수집/스코어링/요약/저장이 다 끝난 뒤라, 배포 하나 실패했다고 전체
실행을 죽이면 안 된다는 판단.
"""

import html
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _escape(value) -> str:
    return html.escape(str(value))


def _format_issue_html(item: dict, rank: int | None = None) -> str:
    """
    이슈 하나 분량의 HTML 블록. summary.md의 _format_issue_section과 같은 정보를 담는다.

    rank: 이 이슈가 속한 목록(전체 Top N 또는 카테고리별 Top N) 안에서 몇 번째인지.
    None이면(순위 개념이 없는 호출부) 번호를 안 붙인다 - 호출부(_format_section_html/
    _format_category_html)가 enumerate로 1부터 매겨서 넘겨준다. 지금까지 이메일에
    순위 숫자가 아예 안 붙어있었던 걸 담당자가 지적해서 추가함(2026-07-28).
    """
    titles = item.get("titles", [])
    rep_title = titles[0] if titles else "(제목 없음)"
    extra = f" (그룹 내 추가 {len(titles) - 1}건 생략)" if len(titles) > 1 else ""
    rank_html = f'<span style="color:#1a73e8;">{rank}.</span> ' if rank is not None else ""

    cross_html = ""
    if item.get("cross_axis_partner"):
        cross_html = (f'<p style="margin:2px 0 4px 0; font-size:12px; color:#1a73e8;">'
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
        link_tags = ", ".join(f'<a href="{_escape(u)}" style="color:#1a73e8; text-decoration:none;">원문</a>' for u in shown)
        links_html = f'<p style="margin:0; font-size:12px; color:#888;">원문 링크: {link_tags}{more}</p>'

    return f"""
    <div style="margin-bottom:16px; padding-bottom:14px; border-bottom:1px solid #eee;">
      <p style="margin:0; font-weight:bold; font-size:14px; color:#111;">{rank_html}{_escape(rep_title)}</p>
      <p style="margin:2px 0 4px 0; font-size:12px; color:#aaa;">점수 {item.get('issue_score', 0):.2f} / 언급 {item.get('mention_count', 0)}건{extra}</p>
      {cross_html}
      {body_html}
      {links_html}
    </div>
    """


def _format_section_html(title: str, items: list[dict]) -> str:
    if not items:
        return f'<h3 style="font-size:16px; color:#222; margin:20px 0 8px 0;">{_escape(title)}</h3><p style="color:#999; font-size:13px;">(이번 주 이슈 없음)</p>'
    # items는 scorer.score_and_rank()가 이미 점수순으로 정렬해둔 상태 - 그 순서
    # 그대로 1부터 번호만 매기면 됨(2026-07-28, 순위 번호 표시 추가).
    body = "".join(_format_issue_html(item, rank=i) for i, item in enumerate(items, start=1))
    return f'<h3 style="font-size:16px; color:#222; margin:20px 0 8px 0;">{_escape(title)}</h3>{body}'


def _format_category_html(label: str, by_category: dict[str, list[dict]]) -> str:
    if not by_category:
        return ""
    blocks = []
    for category, items in by_category.items():
        blocks.append(f'<h4 style="font-size:13px; color:#555; margin:14px 0 6px 0;">[{_escape(category)}]</h4>')
        # 카테고리별로 별도의 Top N이므로, 전체 순위가 아니라 그 카테고리 안에서
        # 1부터 다시 매김(2026-07-28, 순위 번호 표시 추가).
        blocks.append("".join(_format_issue_html(item, rank=i) for i, item in enumerate(items, start=1)))
    return f'<h3 style="font-size:16px; color:#222; margin:24px 0 8px 0;">{_escape(label)} - 카테고리별 Top N</h3>{"".join(blocks)}'


def _format_category_comparison_html(category_comparison: dict[str, dict[str, dict]] | None) -> str:
    """
    category_aggregator.compare_with_last_week()의 결과를 이메일 상단에
    넣을 HTML로 만든다. storage._format_category_comparison_section
    (summary.md용)과 같은 정보, 렌더링 형식만 HTML.
    """
    if not category_comparison:
        return ""
    blocks = ['<h3 style="font-size:16px; color:#222; margin:20px 0 8px 0;">카테고리별 지난주 대비 증감</h3>']
    for axis in ("국내", "해외"):
        axis_data = category_comparison.get(axis, {})
        if not axis_data:
            continue
        rows = []
        for category, values in axis_data.items():
            delta = values["delta"]
            sign = "+" if delta >= 0 else ""
            color = "#1a7f37" if delta > 0 else ("#c0392b" if delta < 0 else "#888")
            rows.append(
                f'<li style="margin:2px 0;">{_escape(category)}: {values["this_week"]}건 '
                f'(지난주 {values["last_week"]}건, <span style="color:{color};">{sign}{delta}</span>)</li>'
            )
        blocks.append(f'<p style="margin:8px 0 2px 0; font-weight:bold; font-size:13px;">{axis}</p>')
        blocks.append(f'<ul style="margin:0 0 8px 0; padding-left:18px; font-size:13px;">{"".join(rows)}</ul>')
    return "".join(blocks)


def render_email_html(week_label: str, domestic_summarized: list[dict], international_summarized: list[dict],
                       domestic_by_category: dict[str, list[dict]],
                       international_by_category: dict[str, list[dict]],
                       failed_sources: list[str],
                       category_comparison: dict[str, dict[str, dict]] | None = None) -> str:
    """
    scored.json과 동일한 데이터를 받아 이메일 본문(HTML 문자열)을 만든다.
    summary.md(storage.py)와 콘텐츠 구성은 같고 렌더링 형식만 HTML로 다르다.

    category_comparison(지난주 대비 증감)이 있으면 제목 바로 아래에 추가 -
    "이번 주 큰 흐름"을 이슈 목록보다 먼저 보여주는 구성(summary.md와
    동일한 배치 원칙).
    """
    parts = [
        '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Arial, sans-serif; '
        'max-width:640px; margin:0 auto; padding:20px; color:#333;">',
        f'<h2 style="font-size:20px; margin:0 0 4px 0;">사료·축산업 뉴스 큐레이션 - {_escape(week_label)}</h2>',
        _format_category_comparison_html(category_comparison),
    ]

    parts.append(_format_section_html("국내", domestic_summarized))
    parts.append(_format_category_html("국내", domestic_by_category))
    parts.append(_format_section_html("해외", international_summarized))
    parts.append(_format_category_html("해외", international_by_category))

    if failed_sources:
        parts.append(
            f'<p style="margin-top:24px; font-size:12px; color:#c0392b;">'
            f'참고 - 이번 실행에서 실패한 소스: {_escape(", ".join(failed_sources))}</p>'
        )

    parts.append("</div>")
    return "".join(parts)


def send_email(html_content: str, subject: str, recipients: list[str],
               smtp_user: str, smtp_app_password: str) -> bool:
    """
    Gmail SMTP(587, STARTTLS)로 HTML 이메일을 보낸다. 성공하면 True, 실패하면
    (예외를 던지지 않고) False를 반환한다 - 호출부가 이 결과로 로그만 남기고
    계속 진행할 수 있게.
    """
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
    main.py에서 부르는 단일 진입점. 환경변수(GitHub Secrets)에서 인증정보를
    읽고, 없으면 요약 모듈들과 같은 패턴으로 안전하게 생략한다.
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
    subject = f"[사료·축산뉴스] {week_label} 주간 큐레이션"
    return send_email(html_content, subject, recipients, smtp_user, smtp_app_password)


if __name__ == "__main__":
    # 자체 점검용 - 실제 SMTP 발송 없이 렌더링/안전 생략 경로만 확인.
    sample_domestic = [{
        "issue_score": 3.5, "mention_count": 2, "titles": ["구제역 확산", "구제역 추가 발생"],
        "urls": ["https://a.com/1", "https://a.com/2"], "summary": "테스트 요약입니다.",
    }]
    sample_category = {"질병명": sample_domestic}

    html_out = render_email_html("2026-30", sample_domestic, [], sample_category, {}, ["GDELT"])
    assert "구제역 확산" in html_out
    assert "테스트 요약입니다" in html_out
    assert "[질병명]" in html_out
    assert "GDELT" in html_out
    print("[deploy] HTML 렌더링 자체 점검 통과")

    # 인증정보 없을 때 안전하게 생략되는지 확인
    for key in ("SMTP_USER", "SMTP_APP_PASSWORD", "EMAIL_RECIPIENTS"):
        os.environ.pop(key, None)
    result = send_weekly_email("2026-30", sample_domestic, [], sample_category, {}, [])
    assert result is False
    print("[deploy] 인증정보 없을 때 안전 생략 확인 - 통과")