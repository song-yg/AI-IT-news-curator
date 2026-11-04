"""llm_rate_limiter.py - OpenRouter 분당 20회 제한 대응, 실제 호출 직전 최소 간격 강제."""

import os
import threading
import time

MIN_INTERVAL_SECONDS = 3.5

# LLM_ENABLED=off 또는 0이면 모든 LLM 호출을 스킵(테스트용 시간 절약). 기본값 on.
LLM_ENABLED = (os.environ.get("LLM_ENABLED") or "on").strip().lower() not in ("off", "0")

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

# --- 실행 중 '죽은 모델' 기록(issue_grouper/relevance_filter/llm_summarizer 공유) ---
# OpenRouter에서 내려간 모델(404 등)을 배치마다 다시 호출하면 배치당 수 초씩 낭비되고,
# 전부 최종 안전망으로 넘어가 처리 단계가 6시간 제한을 넘김(2026-11-02/03 회차 #105·#106).
# 영구 오류로 판단되는 모델은 이번 실행 동안 체인에서 건너뜀.
_PERMANENT_STATUS = {400, 401, 402, 403, 404}
_dead_models: dict[str, str] = {}


def is_dead(model_name: str) -> bool:
    return model_name in _dead_models


def report_failure(model_name: str, error: Exception, module: str) -> None:
    """HTTP 4xx(429 제외) 같은 영구 오류면 이번 실행 동안 모델 사용 중단. 429/5xx/타임아웃은 일시 오류로 보고 유지."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status in _PERMANENT_STATUS and model_name not in _dead_models:
        _dead_models[model_name] = f"HTTP {status}"
        print(f"[{module}] 🔴 조치필요 [LM-01] - 모델('{model_name}') HTTP {status} - 서비스 종료/권한 문제로 판단, "
              f"이번 실행 동안 체인에서 제외 (OPENROUTER_MODEL* 변수 교체 필요)")


def live_chain(chain: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """죽은 모델을 뺀 체인. 다 죽었으면 마지막(최종 안전망) 하나는 남겨 시도는 하게 함."""
    alive = [(role, m) for role, m in chain if m not in _dead_models]
    return alive or chain[-1:]


def dead_models_summary() -> dict[str, str]:
    return dict(_dead_models)


def extract_content(data: dict) -> str:
    """OpenRouter 응답에서 본문 추출. 추론형 모델은 content가 None으로 오기도 함 -> 명시적 오류로."""
    content = data["choices"][0]["message"].get("content")
    if not content or not content.strip():
        raise ValueError(f"빈 응답(content={content!r}, model={data.get('model', '?')})")
    return content.strip()
