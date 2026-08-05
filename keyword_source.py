"""
keyword_source.py - 구글 시트(웹에 게시 CSV)에서 키워드/카테고리를 읽어오는 공용 모듈.
컬럼: keyword, lang(ko/en), active(TRUE/FALSE), category(선택, keyword_tagger용), note.
실패 시(URL 없음/네트워크 실패/형식 깨짐 등) 호출부가 넘긴 fallback으로 안전하게 대체.
"""

import csv
import io
import os
import requests

_cache: dict[str, list[dict] | None] = {}  # 프로세스 내 캐시 - 같은 URL 재요청 방지


def _fetch_csv_rows(csv_url: str) -> list[dict] | None:
    """CSV URL을 가져와 dict 리스트로 파싱. 실패 시 None."""
    if csv_url in _cache:
        print("[keyword_source] 캐시된 CSV 재사용")
        return _cache[csv_url]

    try:
        resp = requests.get(csv_url, timeout=30)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            print("[keyword_source] 🔴 조치필요 [KS-01] - 시트가 비어있음 - fallback 사용")
            _cache[csv_url] = None
            return None
        _cache[csv_url] = rows
        return rows
    except Exception as e:
        print(f"[keyword_source] 🔴 조치필요 [KS-02] - 시트 읽기 실패: {type(e).__name__} - {e!r} - fallback 사용")
        _cache[csv_url] = None
        return None


def _is_active(row: dict) -> bool:
    return str(row.get("active", "")).strip().upper() == "TRUE"


def _detect_keyword_lang(keyword: str) -> str:
    """키워드 글자 구성(한글 비율)으로 실제 언어 판별 - 시트 lang 오기재 보정용."""
    if not keyword:
        return "en"
    hangul_count = sum(1 for ch in keyword if "\uac00" <= ch <= "\ud7a3")
    return "ko" if (hangul_count / len(keyword)) >= 0.2 else "en"


def _is_valid_keyword(keyword: str) -> bool:
    return any(ch.isalnum() for ch in keyword)


def get_keywords(lang: str, fallback: list[str]) -> list[str]:
    """lang("ko"/"en")의 활성 키워드를 시트에서 읽는다. 실패/0건이면 fallback."""
    csv_url = os.environ.get("KEYWORD_SHEET_CSV_URL")
    if not csv_url:
        print(f"[keyword_source] 🔴 조치필요 [KS-03] - KEYWORD_SHEET_CSV_URL 없음 - {lang} fallback 사용: {fallback}")
        return fallback

    rows = _fetch_csv_rows(csv_url)
    if rows is None:
        return fallback

    keywords = []
    mismatches = []
    invalid_keywords = []
    for row in rows:
        keyword = row.get("keyword", "").strip()
        declared_lang = row.get("lang", "").strip().lower()
        if not keyword or declared_lang not in ("ko", "en") or not _is_active(row):
            continue
        if not _is_valid_keyword(keyword):
            invalid_keywords.append(keyword)
            continue
        actual_lang = _detect_keyword_lang(keyword)
        if actual_lang != declared_lang:
            mismatches.append((keyword, declared_lang, actual_lang))
        if actual_lang == lang:
            keywords.append(keyword)

    if invalid_keywords:
        print(f"[keyword_source] 🟡 주의 [KS-04] - 특수문자/공백뿐인 키워드 {len(invalid_keywords)}건 제외: {invalid_keywords!r}")

    if mismatches:
        detail = ", ".join(f"'{kw}'(시트={declared} -> 실제={actual})" for kw, declared, actual in mismatches)
        print(f"[keyword_source] 🟡 주의 [KS-05] - lang 컬럼 오기재 {len(mismatches)}건, 실제 언어로 자동 보정: {detail}")

    seen_normalized = set()
    deduped_keywords = []
    duplicates = []
    for kw in keywords:
        normalized = " ".join(kw.split()).lower()
        if normalized in seen_normalized:
            duplicates.append(kw)
            continue
        seen_normalized.add(normalized)
        deduped_keywords.append(kw)
    keywords = deduped_keywords

    if duplicates:
        print(f"[keyword_source] 🟡 주의 [KS-06] - 중복 등록된 {lang} 키워드 {len(duplicates)}건 제외: {duplicates}")

    if not keywords:
        print(f"[keyword_source] 🔴 조치필요 [KS-07] - lang={lang} 활성 키워드 0건 - fallback 사용")
        return fallback

    print(f"[keyword_source] 구글 시트에서 {lang} 키워드 {len(keywords)}개 로드: {keywords}")
    return keywords


def get_category_keywords(fallback: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, list[str]]]:
    """시트의 category 컬럼으로 {카테고리: {"kr": [...], "en": [...]}} 생성. 실패/0건이면 fallback."""
    csv_url = os.environ.get("KEYWORD_SHEET_CSV_URL")
    if not csv_url:
        print("[keyword_source] 🔴 조치필요 [KS-08] - KEYWORD_SHEET_CSV_URL 없음 - 기본 카테고리 사전 사용")
        return fallback

    rows = _fetch_csv_rows(csv_url)
    if rows is None:
        return fallback

    result: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        keyword = row.get("keyword", "").strip()
        category = row.get("category", "").strip()
        if not keyword or not category or not _is_active(row) or not _is_valid_keyword(keyword):
            continue
        lang_key = "kr" if _detect_keyword_lang(keyword) == "ko" else "en"
        result.setdefault(category, {"kr": [], "en": []})
        if keyword not in result[category][lang_key]:
            result[category][lang_key].append(keyword)

    if not result:
        print("[keyword_source] 🟡 주의 [KS-09] - category 값 있는 활성 키워드 0건 - 기본 카테고리 사전 사용")
        return fallback

    print(f"[keyword_source] 구글 시트에서 카테고리 {len(result)}개 로드: {list(result.keys())}")
    return result