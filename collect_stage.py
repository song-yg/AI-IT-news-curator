"""collect_stage.py - Job 체이닝 1단계: 수집 전용 진입점. 결과를 collected.json에 저장."""

import json
import os
import time
import traceback

import error_log
import main as main_module

OUTPUT_DIR = "collect_output"
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "collected.json")

COLLECT_TIME_BUDGET_SECONDS = 5 * 60 * 60 + 50 * 60  # 350분(5시간50분), 나머지 10분은 설치/커밋 버퍼


def run() -> None:
    collect_start = time.monotonic()
    deadline_gdelt = collect_start + COLLECT_TIME_BUDGET_SECONDS

    window_start, window_end = main_module._compute_collection_window()
    print(f"[collect_stage] 이번 실행 수집 구간: {window_start.isoformat()} ~ {window_end.isoformat()} (UTC)")

    error_log.start_capture()
    try:
        articles, gdelt_timeline, failed_sources = main_module.run_collectors(
            window_start, window_end, deadline=deadline_gdelt)
    except Exception as e:
        print(f"[collect_stage] 🔴 조치필요 [CS-01] - 수집 단계에서 예상 못 한 오류 발생 - 빈 결과로 계속 진행: "
              f"{type(e).__name__} - {e!r}")
        traceback.print_exc()
        articles, gdelt_timeline, failed_sources = [], {}, ["네이버", "GDELT"]
    error_codes = error_log.stop_capture()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {
        "articles": articles,
        "gdelt_timeline": gdelt_timeline,
        "failed_sources": failed_sources,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "error_codes": error_codes,
    }
    try:
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[collect_stage] 수집 결과 저장 완료 -> {OUTPUT_PATH} ({len(articles)}건, 실패 소스: {failed_sources or '없음'})")
    except OSError as e:
        print(f"[collect_stage] 🔴 조치필요 [CS-02] - 수집 결과 파일 저장 실패: {type(e).__name__} - {e!r}")


if __name__ == "__main__":
    run()