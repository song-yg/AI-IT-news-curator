"""
process_stage.py
Job 체이닝(2-Job 구조) 2단계 - 정규화~배포 전용 진입점.

워크플로(run-pipline.yml)의 "process" Job이 이 스크립트를 실행한다. collect_stage.py가
만든 결과(actions/download-artifact로 내려받은 collect_output/collected.json)를 읽어서
이어받고, main.py의 run_process()(정규화~배포 본체, run()과 공유)를 그대로 호출한다.

** 이 Job의 시간예산 **
main.py.run_process()가 이미 관련성 필터(RELEVANCE_TIME_BUDGET_SECONDS, 4시간)/카테고리
재분류(CATEGORY_RECLASSIFY_TIME_BUDGET_SECONDS, 5시간)만 예산을 두고 나머지(그룹핑/4차
재검토/요약)는 무제한으로 처리한다 - 이 스크립트는 그 기준 시각(process_start)만 "이 Job이
시작된 시각"으로 넘겨준다.

** collect_output/collected.json이 없거나 손상됐을 때 **
collect Job이 실패했거나(process 쪽 워크플로에 if: always()를 걸어둬서 이 Job은 그래도
돌아감) artifact 자체가 안 만들어졌을 수 있다 - 이 경우 빈 articles로 안전하게 시작해서
파이프라인 나머지 단계가 "오늘은 기사가 0건"인 것처럼 정상적으로(요약/저장/배포 각 단계의
기존 안전한 기본값 그대로) 흘러가게 한다.
"""

import json
import os
import time
from datetime import datetime

import main as main_module

INPUT_PATH = os.path.join("collect_output", "collected.json")


def _load_collected() -> tuple[list[dict], dict, list[str], datetime, datetime]:
    """collect_stage.py가 만든 결과를 읽어온다. 없거나 손상됐으면 안전하게 빈 값으로 시작."""
    try:
        with open(INPUT_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        articles = payload["articles"]
        gdelt_timeline = payload.get("gdelt_timeline", {})
        failed_sources = payload.get("failed_sources", [])
        window_start = datetime.fromisoformat(payload["window_start"])
        window_end = datetime.fromisoformat(payload["window_end"])
        print(f"[process_stage] 수집 결과 로드 완료 -> {INPUT_PATH} ({len(articles)}건, "
              f"수집 구간 {window_start.isoformat()} ~ {window_end.isoformat()})")
        return articles, gdelt_timeline, failed_sources, window_start, window_end
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[process_stage] 🔴 조치필요 [PS-01] - collect 결과 로드 실패(collect Job이 "
              f"실패했거나 artifact가 없는 것으로 추정) - 빈 기사로 계속 진행: "
              f"{type(e).__name__} - {e!r}")
        window_start, window_end = main_module._compute_collection_window()
        return [], {}, ["네이버", "GDELT"], window_start, window_end


def run() -> None:
    process_start = time.monotonic()
    articles, gdelt_timeline, failed_sources, window_start, window_end = _load_collected()
    main_module.run_process(articles, gdelt_timeline, failed_sources, window_start, window_end,
                             process_start=process_start)


if __name__ == "__main__":
    run()