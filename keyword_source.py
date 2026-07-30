"""
keyword_source.py
구글 시트에서 키워드 목록을 읽어오는 공용 모듈.

시트를 "웹에 게시(CSV)"해두면 인증 없이 GET으로 읽을 수 있어서,
코드 배포/GitHub 권한 없이 구글 시트 편집 권한만으로 키워드를 추가/수정할 수 있다.

시트 형식 (컬럼: keyword, lang, active, note):
  - keyword: 검색어 (naver_collector는 ko 행, gdelt_collector는 en 행을 사용)
  - lang: "ko" 또는 "en"
  - active: TRUE/FALSE (행 삭제 대신 켜고 끄기용 - 실수로 지워서 이력이 사라지는 것 방지)
  - note: 사람이 보는 메모, 코드는 안 읽음

실패 시(URL 없음/네트워크 실패/형식 깨짐/해당 lang 활성 키워드 없음) 호출부가 넘긴
fallback(각 collector의 기존 하드코딩 리스트)으로 안전하게 대체한다.
"""

import csv
import io
import os
import requests

# 프로세스 내 캐시. naver/gdelt collector가 각각 "ko"/"en"으로 호출해도 같은 CSV를 두 번 요청하지 않도록 함.
# 한 실행 안에서만 유효 (주 1회 실행, 실행 도중 시트 변경 반영 요구사항 없음).
_cache: dict[str, list[dict] | None] = {}


def _fetch_csv_rows(csv_url: str) -> list[dict] | None:
    """CSV URL을 가져와 dict 리스트로 파싱. 실패 시 None(호출부가 fallback). 같은 URL은 캐시 재사용."""
    if csv_url in _cache:
        cached = _cache[csv_url]
        print(f"[keyword_source] 캐시된 CSV 재사용 (이번 실행에서 이미 가져온 적 있음)")
        return cached

    try:
        resp = requests.get(csv_url, timeout=30)
        resp.raise_for_status()
        # 구글 시트 게시 CSV는 UTF-8 BOM이 붙어오는 경우가 많아 utf-8-sig로 디코딩
        text = resp.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            print("[keyword_source] 🔴 조치필요 [KS-01] - 시트가 비어있음(CSV 대신 엉뚱한 내용이 왔을 가능성) - fallback 사용")
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
    """
    키워드 글자 구성으로 실제 언어 판별 ("ko"/"en")
      - 시트 lang 컬럼은 사람이 입력하는 값이라 실수로 뒤바뀔 수 있어서, 한글 비율 기준(scorer._is_korean_title()과 동일 방식)으로 재확인.
    """
    if not keyword:
        return "en"
    hangul_count = sum(1 for ch in keyword if "\uac00" <= ch <= "\ud7a3")
    return "ko" if (hangul_count / len(keyword)) >= 0.2 else "en"


def _is_valid_keyword(keyword: str) -> bool:
    """문자/숫자를 하나라도 포함하는지 확인 (특수문자·공백만 있는 행 제거용)."""
    return any(ch.isalnum() for ch in keyword)


def get_keywords(lang: str, fallback: list[str]) -> list[str]:
    """
    lang("ko"/"en")의 활성 키워드를 구글 시트에서 읽어온다.
    URL 없음/읽기 실패/해당 lang 활성 키워드 없음 -> fallback 반환. 예외를 던지지 않음.
    """
    csv_url = os.environ.get("KEYWORD_SHEET_CSV_URL")
    if not csv_url:
        print(f"[keyword_source] 🔴 조치필요 [KS-03] - KEYWORD_SHEET_CSV_URL 없음 - {lang} 기본(하드코딩) 키워드 리스트 사용: {fallback}")
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
        print(f"[keyword_source] 🟡 주의 [KS-04] - 시트에 문자/숫자가 하나도 없는(특수문자·공백뿐인) "
              f"키워드 {len(invalid_keywords)}건 제외: {invalid_keywords!r}")

    if mismatches:
        detail = ", ".join(f"'{kw}'(시트={declared} -> 실제={actual})" for kw, declared, actual in mismatches)
        print(f"[keyword_source] 🟡 주의 [KS-05] - 시트 lang 컬럼과 실제 키워드 언어가 다른 항목 {len(mismatches)}건 "
              f"발견 - 실제 언어 기준으로 자동 보정해서 사용(시트도 고쳐두는 걸 권장): {detail}")

    # 중복 제거 - 공백/대소문자 차이는 정규화해서 비교, 표기는 먼저 등장한 쪽 유지
    # (중복 방치 시 GDELT 429/호출량만 늘고 얻는 게 없음)
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
        print(f"[keyword_source] 🟡 주의 [KS-06] - 시트에 중복 등록된 {lang} 키워드 {len(duplicates)}건 제외(첫 등장만 유지): "
              f"{duplicates}")

    if not keywords:
        print(f"[keyword_source] 🔴 조치필요 [KS-07] - 구글 시트에 lang={lang} 활성 키워드가 하나도 없음"
              f"(CSV 대신 엉뚱한 내용이 왔거나, 시트에서 실수로 전부 비활성화했을 수 있음) - fallback 사용")
        return fallback

    print(f"[keyword_source] 구글 시트에서 {lang} 키워드 {len(keywords)}개 로드: {keywords}")
    return keywords