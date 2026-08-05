"""process_stage.py - Job 체이닝 2단계: 정규화~배포 전용 진입점. collected.json을 읽어 main.run_process() 호출."""

import json
import os
import time
from datetime import datetime

import main as main_module
import error_log

INPUT_PATH = os.path.join("collect_output", "collected.json")


def _load_collected() -> tuple[list[dict], dict, list[str], datetime, datetime, list[str]]:
    """collect_stage.py 결과를 읽어온다. 없거나 손상됐으면 빈 값으로 시작."""
    try:
        with open(INPUT_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        articles = payload["articles"]
        gdelt_timeline = payload.get("gdelt_timeline", {})
        failed_sources = payload.get("failed_sources", [])
        error_codes = payload.get("error_codes", [])
        window_start = datetime.fromisoformat(payload["window_start"])
        window_end = datetime.fromisoformat(payload["window_end"])
        print(f"[process_stage] 수집 결과 로드 완료 -> {INPUT_PATH} ({len(articles)}건, "
              f"수집 구간 {window_start.isoformat()} ~ {window_end.isoformat()})")
        return articles, gdelt_timeline, failed_sources, window_start, window_end, error_codes
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[process_stage] 🔴 조치필요 [PS-01] - collect 결과 로드 실패 - 빈 기사로 계속 진행: "
              f"{type(e).__name__} - {e!r}")
        window_start, window_end = main_module._compute_collection_window()
        return [], {}, ["네이버", "GDELT"], window_start, window_end, []


def run() -> None:
    process_start = time.monotonic()
    error_log.start_capture()
    articles, gdelt_timeline, failed_sources, window_start, window_end, collect_error_codes = _load_collected()
    load_error_codes = error_log.stop_capture()
    prior_error_codes = error_log.merge_unique(collect_error_codes, load_error_codes)

    main_module.run_process(articles, gdelt_timeline, failed_sources, window_start, window_end,
                             process_start=process_start, prior_error_codes=prior_error_codes)


if __name__ == "__main__":
    run()