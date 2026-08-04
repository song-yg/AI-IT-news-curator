"""
collect_stage.py
Job 체이닝(2-Job 구조) 1단계 - 수집 전용 진입점.

워크플로(run-pipline.yml)의 "collect" Job이 이 스크립트를 실행한다. main.py의
run_collectors()/_compute_collection_window()를 그대로 재사용해서 네이버+GDELT를
수집하고, 결과를 JSON 파일로 저장한다 - 이 Job은 여기서 끝나고, 워크플로가 이 파일을
actions/upload-artifact로 올려서 다음 Job("process", process_stage.py)이
actions/download-artifact로 이어받는다.

** 이 Job의 시간예산 **
GDELT 수집이 이 Job을 혼자 쓴다 - "process" Job(관련성필터~배포)과 시간을 나눠 쓰지
않아도 되므로, GitHub Actions Job 하드 캡 그대로 6시간(360분) 전부를 GDELT에 준다
(COLLECT_TIME_BUDGET_SECONDS). main.py.run()의 5단계 체크포인트 체계(_CHECKPOINT_*_MIN,
355분을 여러 단계가 나눠 씀)와는 완전히 다른 예산이다 - 그건 모든 단계가 한 Job 안에
있던 시절의 설계이고, 지금은 Job이 갈라져서 GDELT가 온전히 자기 몫의 6시간을 가진다.
(main.py.run() 자체는 안 건드렸음 - 로컬에서 전체 파이프라인을 한 번에 테스트하고 싶을
때는 여전히 `python main.py`로 그대로 쓸 수 있다.)

** 실패해도 절대 죽지 않는다 **
이 Job이 예외로 죽으면 "process" Job이 아예 못 돈다(needs: collect가 기본적으로 그 Job을
skip시킴 - process 쪽 워크플로에 if: always()를 걸어두지만, 그래도 이 스크립트 자체가
가능한 한 죽지 않는 게 우선이다). run_collectors()가 이미 naver/gdelt 각각을 개별
try/except로 감싸서 어느 한쪽이 실패해도 나머지는 살리는데, 이 스크립트는 그 바깥을
한 번 더 감싸서 정말 예상 못 한 오류(예: 두 collector 호출 사이의 코드 자체가 깨짐)가
나도 최소한 "failed_sources에 원인 기록 + 빈 articles"로 결과 파일 자체는 반드시
만든다 - process_stage.py가 파일이 아예 없는 경우까지 방어하긴 하지만, 있는 편이 더 안전.
"""

import json
import os
import time
import traceback

import main as main_module  # run_collectors, _compute_collection_window 재사용

OUTPUT_DIR = "collect_output"
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "collected.json")

# 이 Job이 혼자 쓸 수 있는 최대 시간 - GitHub Actions Job 하드 캡(6시간)과 동일.
# 그 이상 잡아봐야 어차피 Job 자체가 강제 종료되므로 의미가 없어 캡에 맞춤.
COLLECT_TIME_BUDGET_SECONDS = 6 * 60 * 60  # 360분(6시간)


def run() -> None:
    collect_start = time.monotonic()
    deadline_gdelt = collect_start + COLLECT_TIME_BUDGET_SECONDS

    window_start, window_end = main_module._compute_collection_window()
    print(f"[collect_stage] 이번 실행 수집 구간: {window_start.isoformat()} ~ {window_end.isoformat()} (UTC)")

    try:
        articles, gdelt_timeline, failed_sources = main_module.run_collectors(
            window_start, window_end, deadline=deadline_gdelt)
    except Exception as e:
        # run_collectors() 내부는 이미 naver/gdelt 각각 try/except로 감싸져 있어 여기까지
        # 예외가 올라올 일이 거의 없어야 정상이다 - 그래도 정말 예상 못 한 오류에 대비해
        # 한 번 더 감싼다.
        print(f"[collect_stage] 🔴 조치필요 [CS-01] - 수집 단계에서 예상 못 한 오류 발생 - "
              f"빈 결과로 계속 진행(process 단계가 빈 articles로 안전하게 처리함): "
              f"{type(e).__name__} - {e!r}")
        traceback.print_exc()
        articles, gdelt_timeline, failed_sources = [], {}, ["네이버", "GDELT"]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {
        "articles": articles,
        "gdelt_timeline": gdelt_timeline,
        "failed_sources": failed_sources,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }
    try:
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[collect_stage] 수집 결과 저장 완료 -> {OUTPUT_PATH} ({len(articles)}건, "
              f"실패 소스: {failed_sources or '없음'})")
    except OSError as e:
        # 이것마저 실패하면 정말 손 쓸 방법이 없다(디스크 문제 등) - 로그만 남기고 종료.
        # upload-artifact 스텝이 if-no-files-found: warn으로 이 상황을 조용히 넘기고,
        # process Job 쪽은 파일이 아예 없는 경우를 스스로 감지해서 방어한다.
        print(f"[collect_stage] 🔴 조치필요 [CS-02] - 수집 결과 파일 저장 실패 - 이번 실행은 "
              f"여기서 끝남(process Job이 결과 없음으로 안전하게 처리): {type(e).__name__} - {e!r}")


if __name__ == "__main__":
    run()