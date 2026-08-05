"""
category_chart.py
최근 7일(오늘 포함)치 카테고리별 기사 건수 추이를 꺾은선 그래프로 그려 이메일에 삽입한다.

PDF는 안 만듦 - 이메일 본문에 바로 보이는 게 목적이라 별도 파일로 분리하는 PDF는
불필요한 스텝. matplotlib으로 PNG를 그려서 base64로 인코딩하고, deploy.py가
<img src="data:image/png;base64,...">로 이메일 HTML에 직접 삽입한다(외부 이미지 URL
방식은 이메일 클라이언트가 기본적으로 로드를 차단하는 경우가 많아서 안 씀 - 첨부/인라인
방식이라 클라이언트가 바로 렌더링함).

** 데이터 출처 **
과거 6일치는 storage.py가 저장해둔 data/YYYY-MM-DD/scored.json의 category_distribution을
읽는다(category_aggregator.compare_with_7day_average와 같은 방식). 오늘(7일째) 몫은 아직
파일로 저장되기 *전*이라(이 모듈은 main.py의 [3-보조] 카테고리 집계 직후, [5] 저장 이전에
호출됨) 메모리에 있는 값을 그대로 받는다.

** 한글 폰트 **
GitHub Actions 러너(ubuntu-latest)에는 한글 폰트가 기본 설치돼 있지 않아, matplotlib
기본 폰트로는 카테고리명이 네모(tofu)로 깨져 나온다. run-pipline.yml의 process Job에
fonts-nanum 설치 스텝을 추가해뒀다 - 이 모듈은 그 폰트가 있으면 쓰고, 없으면(로컬 등)
경고만 남기고 기본 폰트로 계속 진행한다(그래프 자체가 아예 안 나오는 것보다 낫다는 판단).
"""

import base64
import io
import json
import os
from datetime import datetime, timedelta, timezone

_KOREAN_FONT_CANDIDATES = [
    "NanumGothic", "Nanum Gothic", "Malgun Gothic", "AppleGothic", "Noto Sans CJK KR",
]


def _find_korean_font() -> str | None:
    """설치된 폰트 중 한글 지원 폰트 이름을 찾는다. 없으면 None(기본 폰트로 fallback)."""
    try:
        import matplotlib.font_manager as fm
    except ImportError:
        return None
    installed = {f.name for f in fm.fontManager.ttflist}
    for candidate in _KOREAN_FONT_CANDIDATES:
        if candidate in installed:
            return candidate
    return None


def _load_7day_series(today_distribution: dict, base_dir: str = "data",
                       reference: datetime | None = None) -> tuple[list[str], list[dict]]:
    """
    최근 7일(오늘 포함, 오래된 날짜 -> 오늘 순) 날짜 라벨 + category_distribution 리스트를 만든다.
    과거 날짜의 scored.json이 없으면(일간 전환 초기 등) 그 날짜는 빈 분포(0건)로 채운다 -
    그래프에 구멍이 나는 대신 평평하게 이어지게 하려는 선택.
    """
    now = reference or datetime.now(timezone.utc)
    dates = [now - timedelta(days=i) for i in range(6, -1, -1)]  # 6일 전 ... 오늘
    labels = [d.strftime("%m/%d") for d in dates]

    daily_distributions = []
    for i, d in enumerate(dates):
        if i == len(dates) - 1:  # 마지막 = 오늘, 아직 파일이 없으므로 메모리 값 사용
            daily_distributions.append(today_distribution or {})
            continue
        path = os.path.join(base_dir, d.strftime("%Y-%m-%d"), "scored.json")
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            distribution = payload.get("category_distribution")
            daily_distributions.append(distribution if isinstance(distribution, dict) else {})
        except (OSError, json.JSONDecodeError):
            daily_distributions.append({})

    return labels, daily_distributions


def _render_chart(axis: str, labels: list[str], daily_distributions: list[dict],
                   font_name: str | None) -> str | None:
    """실제 matplotlib 렌더링. 실패해도 예외를 던지지 않고 None 반환(이메일에서 그 섹션만 생략)."""
    import matplotlib
    matplotlib.use("Agg")  # 서버 환경(디스플레이 없음) - 화면 표시 대신 파일/버퍼로만 출력
    import matplotlib.pyplot as plt

    categories = sorted({c for dist in daily_distributions for c in dist.get(axis, {})})
    if not categories:
        return None

    if font_name:
        plt.rcParams["font.family"] = font_name
    plt.rcParams["axes.unicode_minus"] = False  # 한글 폰트 사용 시 마이너스 기호 깨짐 방지

    try:
        fig, ax = plt.subplots(figsize=(6, 3.2), dpi=120)
        for category in categories:
            values = [dist.get(axis, {}).get(category, 0) for dist in daily_distributions]
            ax.plot(labels, values, marker="o", label=category, linewidth=1.5, markersize=3)
        ax.set_title(f"{axis} 카테고리별 최근 7일 추이", fontsize=10)
        ax.legend(fontsize=6, loc="upper left", ncol=2, framealpha=0.7)
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.3)
        ax.set_ylim(bottom=0)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")
    except Exception as e:
        print(f"[category_chart] 🔴 조치필요 [CC-01] - {axis} 그래프 생성 실패: {type(e).__name__} - {e!r}")
        return None


def generate_charts(today_distribution: dict, base_dir: str = "data",
                     reference: datetime | None = None) -> dict[str, str]:
    """
    국내/해외 두 축의 7일 추이 그래프를 각각 base64 PNG 문자열로 만들어 반환한다.
    matplotlib 미설치, 데이터 없음, 렌더링 실패 등 어떤 이유로든 특정 축을 못 그리면
    그 축은 결과 dict에서 아예 빠진다(deploy.py가 있는 것만 보여줌) - 안전한 기본값.
    """
    try:
        import matplotlib  # noqa: F401 - 설치 여부만 확인
    except ImportError as e:
        print(f"[category_chart] 🟡 주의 [CC-02] - matplotlib 미설치 - 그래프 생략: {type(e).__name__} - {e!r}")
        return {}

    font_name = _find_korean_font()
    if font_name is None:
        print("[category_chart] 🟡 주의 [CC-03] - 한글 폰트를 못 찾음(run-pipline.yml의 fonts-nanum "
              "설치 스텝 확인 필요) - 카테고리명이 깨져 보일 수 있음, 그래도 그래프는 생성함")

    labels, daily_distributions = _load_7day_series(today_distribution, base_dir, reference)

    charts = {}
    for axis in ("국내", "해외"):
        png_base64 = _render_chart(axis, labels, daily_distributions, font_name)
        if png_base64:
            charts[axis] = png_base64

    if charts:
        print(f"[category_chart] 7일 추이 그래프 생성 완료: {list(charts.keys())}")
    return charts