"""llm_rate_limiter.py - OpenRouter 분당 20회 제한 대응, 실제 호출 직전 최소 간격 강제."""

import threading
import time

MIN_INTERVAL_SECONDS = 3.5

_lock = threading.Lock()
_last_call_time = 0.0


def wait_for_openrouter_slot() -> None:
    """직전 호출로부터 MIN_INTERVAL_SECONDS 안 지났으면 대기. 여러 모듈이 공유해서 호출."""
    global _last_call_time
    with _lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.monotonic()