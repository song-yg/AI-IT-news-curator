"""
error_log.py
"🔴 조치필요 [XX-NN]" 등급 로그의 코드만 모아서 이메일 하단에 조용히 노출하기 위한 수집기.

모든 모듈이 각자 print()로 로그를 남기는 기존 방식은 그대로 두고, stdout을 가로채서
"🔴 조치필요 [XX-NN]" 패턴만 정규식으로 뽑아 리스트에 모은다 - 수십 곳의 print 호출부를
전부 손대는 대신 표준출력 지점 하나만 감싸면 되는 방식을 택함.
"""

import re
import sys
from contextlib import contextmanager

_CODE_PATTERN = re.compile(r"🔴 조치필요 \[([A-Z]{2}-\d+)\]")

_collected_codes: list[str] = []
_original_stdout = None


class _TeeStdout:
    """원래 stdout에 그대로 쓰면서, 지나가는 텍스트에서 코드만 추가로 뽑아낸다."""

    def __init__(self, original):
        self._original = original

    def write(self, text: str) -> int:
        written = self._original.write(text)
        for code in _CODE_PATTERN.findall(text):
            _collected_codes.append(code)
        return written

    def flush(self) -> None:
        self._original.flush()


@contextmanager
def capture():
    """이 블록 안에서 발생한 "🔴 조치필요 [XX-NN]" 로그의 코드를 전부 수집한다."""
    start_capture()
    try:
        yield
    finally:
        stop_capture()


def start_capture() -> None:
    """
    capture()의 들여쓰기(with 블록) 없이 쓰고 싶을 때(예: 함수 본문 대부분을 감싸야 해서
    전체를 재들여쓰기하기 번거로운 경우) 쓰는 짝 함수. 반드시 stop_capture()와 짝을 맞춰야
    하고, 그 사이 예외가 나도 stdout이 원래대로 복구되도록 호출부가 try/finally로 감싸야 한다.
    """
    global _original_stdout
    _collected_codes.clear()
    _original_stdout = sys.stdout
    sys.stdout = _TeeStdout(_original_stdout)


def stop_capture() -> list[str]:
    """start_capture()로 가로챈 stdout을 원래대로 되돌리고, 그동안 모인 코드를 반환한다."""
    global _original_stdout
    if _original_stdout is not None:
        sys.stdout = _original_stdout
        _original_stdout = None
    return get_collected_codes()


def get_collected_codes() -> list[str]:
    """capture() 블록 안에서 지금까지 모인 코드(중복 포함, 등장 순서)."""
    return list(_collected_codes)


def merge_unique(*code_lists: list[str]) -> list[str]:
    """여러 코드 리스트(예: collect Job 몫 + process Job 몫)를 합쳐 중복 없이, 첫 등장 순서로 반환."""
    seen: set[str] = set()
    merged: list[str] = []
    for codes in code_lists:
        for code in codes:
            if code not in seen:
                seen.add(code)
                merged.append(code)
    return merged