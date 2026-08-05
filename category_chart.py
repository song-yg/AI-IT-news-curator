"""category_chart.py - 최근 7일(오늘 포함) 카테고리별 건수 추이 그래프(PNG, base64) 생성."""

import base64
import io
import json
import os
from datetime import datetime, timedelta, timezone

_NANUM_GOTHIC_PATH = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"


def _find_korean_font() -> str | None:
    """나눔고딕 폰트 파일을 matplotlib에 등록. 없으면 None."""
    try:
        import matplotlib.font_manager as fm
    except ImportError:
        return None
    if not os.path.exists(_NANUM_GOTHIC_PATH):
        return None
    try:
        fm.fontManager.addfont(_NANUM_GOTHIC_PATH)
        return fm.FontProperties(fname=_NANUM_GOTHIC_PATH).get_name()
    except Exception:
        return None


def _load_7day_series(today_distribution: dict, base_dir: str = "data",
                       reference: datetime | None = None) -> tuple[list[str], list[dict]]:
    """최근 7일(오늘 포함) 날짜 라벨 + category_distribution 리스트."""
    now = reference or datetime.now(timezone.utc)
    dates = [now - timedelta(days=i) for i in range(6, -1, -1)]
    labels = [d.strftime("%m/%d") for d in dates]

    daily_distributions = []
    for i, d in enumerate(dates):
        if i == len(dates) - 1:
            daily_distributions.append(today_distribution or {})
            continue
        path = os.path.join(base_dir, d.strftime("%Y-%m-%d"), "scored.json")
        try:
            with open(path, encoding="utf-8") as f:
                distribution = json.load(f).get("category_distribution")
            daily_distributions.append(distribution if isinstance(distribution, dict) else {})
        except (OSError, json.JSONDecodeError):
            daily_distributions.append({})

    return labels, daily_distributions


def _render_chart(axis: str, labels: list[str], daily_distributions: list[dict],
                   font_name: str | None) -> str | None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    categories = sorted({c for dist in daily_distributions for c in dist.get(axis, {})})
    if not categories:
        return None

    if font_name:
        plt.rcParams["font.family"] = font_name
    plt.rcParams["axes.unicode_minus"] = False

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
    """국내/해외 7일 추이 그래프를 base64 PNG로 반환. 실패한 축은 결과에서 빠짐."""
    try:
        import matplotlib  # noqa: F401
    except ImportError as e:
        print(f"[category_chart] 🟡 주의 [CC-02] - matplotlib 미설치 - 그래프 생략: {type(e).__name__} - {e!r}")
        return {}

    font_name = _find_korean_font()
    if font_name is None:
        print("[category_chart] 🟡 주의 [CC-03] - 한글 폰트를 못 찾음 - 카테고리명이 깨져 보일 수 있음")

    labels, daily_distributions = _load_7day_series(today_distribution, base_dir, reference)

    charts = {}
    for axis in ("국내", "해외"):
        png_base64 = _render_chart(axis, labels, daily_distributions, font_name)
        if png_base64:
            charts[axis] = png_base64

    if charts:
        print(f"[category_chart] 7일 추이 그래프 생성 완료: {list(charts.keys())}")
    return charts