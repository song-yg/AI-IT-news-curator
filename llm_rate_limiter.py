"""
llm_rate_limiter.py
OpenRouter 무료 티어 모델의 분당 요청 한도(20회/분, 사용자 확인) 대응 - 실제 HTTP 요청
직전에 최소 간격을 강제하는 전역 쓰로틀러.

issue_grouper.py/relevance_filter.py/llm_summarizer.py가 각자 독립적으로
_request_openrouter()를 갖고 있다(이 프로젝트의 기존 방침 - 모듈마다 필요한 만큼만
가져다 쓰고 공통 라이브러리로 무리하게 묶지 않음). 그런데 "마지막 호출 시각"까지
모듈마다 따로 추적하면, 파이프라인이 순차 실행(동시성 없음)이라 하더라도 단계 경계
(예: 관련성 필터의 마지막 호출 직후 이슈 그룹핑의 첫 호출)에서는 두 모듈이 서로의 마지막
호출 시각을 모르니 간격이 비어버리는 사각지대가 생긴다 - 그래서 "마지막 호출 시각" 하나만
프로세스 전체가 공유하도록 이 모듈로 뽑아냈다. 세 모듈 다 이 모듈의 wait_for_openrouter_slot()
하나만 가져다 쓴다.

모델 체인 폴백(1순위 실패 -> 2순위 시도 -> ...)으로 한 배치 안에서 실제 HTTP 요청이 여러 번
나가는 경우도 있는데, 그것도 전부 실제 요청이므로 매번 이 함수를 거쳐야 함 - 그래서
각 모듈의 _request_openrouter()(배치 루프가 아니라 실제 요청을 보내는 가장 안쪽 함수)에
붙여야 빠짐없이 적용된다.
"""

import threading
import time

# 60초 / 20회 = 3초가 이론상 한계치라, 약간의 여유를 두고 3.5초로 잡음
# (네트워크 지연 등으로 요청 타임스탬프가 살짝 밀리는 경우까지 감안).
MIN_INTERVAL_SECONDS = 3.5

_lock = threading.Lock()
_last_call_time = 0.0


def wait_for_openrouter_slot() -> None:
    """
    직전 OpenRouter 실제 호출로부터 MIN_INTERVAL_SECONDS가 지나지 않았으면 그만큼 대기한다.
    Lock으로 감싼 이유: 이 프로젝트는 현재 단일 스레드로만 도는 파이프라인이라 엄밀히는
    불필요하지만, "여러 모듈이 이 함수를 공유해서 부른다"는 특성상 나중에 실수로 병렬화가
    들어와도 이 함수 자체는 안전하게 동작하도록 방어적으로 넣어둠(비용 거의 없음).
    """
    global _last_call_time
    with _lock:
        elapsed = time.monotonic() - _last_call_time
        wait = MIN_INTERVAL_SECONDS - elapsed
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.monotonic()