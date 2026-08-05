"""error_log.py - stdout에서 "🔴 조치필요 [XX-NN]" 코드만 정규식으로 수집해 이메일 하단에 표시."""

import re
import sys
from contextlib import contextmanager

_CODE_PATTERN = re.compile(r"🔴 조치필요 \[([A-Z]{2}-\d+)\]")

_collected_codes: list[str] = []
_original_stdout = None


class _TeeStdout:
    """원래 stdout에 쓰면서 코드만 추가로 뽑아낸다."""

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
    start_capture()
    try:
        yield
    finally:
        stop_capture()


def start_capture() -> None:
    """capture()의 짝 함수(들여쓰기 없이 쓰고 싶을 때). stop_capture()와 반드시 짝을 맞출 것."""
    global _original_stdout
    _collected_codes.clear()
    _original_stdout = sys.stdout
    sys.stdout = _TeeStdout(_original_stdout)


def stop_capture() -> list[str]:
    """stdout을 원복하고 모인 코드를 반환."""
    global _original_stdout
    if _original_stdout is not None:
        sys.stdout = _original_stdout
        _original_stdout = None
    return get_collected_codes()


def get_collected_codes() -> list[str]:
    return list(_collected_codes)


def merge_unique(*code_lists: list[str]) -> list[str]:
    """여러 코드 리스트를 중복 없이, 첫 등장 순서로 합친다."""
    seen: set[str] = set()
    merged: list[str] = []
    for codes in code_lists:
        for code in codes:
            if code not in seen:
                seen.add(code)
                merged.append(code)
    return merged