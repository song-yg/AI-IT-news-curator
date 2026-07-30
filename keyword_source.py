"""
keyword_source.py
구글 시트에서 키워드 목록을 읽어오는 공용 모듈.

GitHub 직접 편집(git push)은 진입장벽이 있어서, 구글 시트를 "웹에 게시(CSV)"해두고 실행할 때마다 그 URL을 읽어오는 방식.

- 서비스 계정/OAuth 인증이 전혀 필요 없음 (시트를 "파일 > 공유 > 웹에 게시"
  로 CSV 형식으로 게시하면 인증 없이 GET으로 읽을 수 있는 공개 URL이 생김)
- 키워드 등록에 GitHub 계정조차 필요 없어짐 (구글 시트 편집 권한만 있으면 됨)
- 코드 배포 없이 실행할 때마다 최신 시트 내용을 읽으므로, 키워드 추가에
  git push/PR/배포 절차가 전혀 필요 없음

** 시트 형식 (한 시트, 아래 4개 컬럼 - 첫 행은 헤더) **

  keyword                  | lang | active | note
  --------------------------|------|--------|------------------------
  깃허브                     | ko   | TRUE   |
  AI                        | en   | FALSE  | GDELT가 너무 짧다고 거부함

  - keyword: 실제 검색어 (naver_collector는 "ko" 행을, gdelt_collector는 "en" 행을 가져다 씀)
  - lang: "ko" 또는 "en" (대소문자 무관)
  - active: TRUE/FALSE - 사람이 행을 삭제하지 않고 켜고 끌 수 있게 함
    (실수로 지워서 이력이 사라지는 것보다, 꺼두고 note에 이유를 남기는 쪽이 안전)
  - note: 자유 메모(왜 껐는지 등) - 코드는 안 읽음, 사람이 보는 용도

** 실패 시 fallback **
KEYWORD_SHEET_CSV_URL 환경변수가 없거나, 네트워크 실패, 시트 형식이
깨졌거나, 특정 lang에 활성 키워드가 하나도 없는 경우 등
  - 어떤 이유로든 정상적으로 못 읽으면 호출부가 넘겨준 fallback(각 collector에 원래 있던 하드코딩 리스트)으로 안전하게 대체한다. 즉 이 기능을 아예 설정 안 해도,  설정했다가 시트가 일시적으로 안 열려도 파이프라인 자체는 죽지 않는다.
"""

import csv
import io
import os
import requests

# 프로세스 내 캐시. get_keywords가 "ko"/"en" 각각을 위해 독립적으로 호출되는데(naver_collector가 ko용, gdelt_collector가 en용), 기존엔 매번 완전히 같은 CSV를 처음부터 다시 요청+파싱했음(한 실행에 총 2번).
# 이 프로세스는 한 번 실행되고 끝나는 구조(매번 새 GitHub Actions 러너)라 "캐시가 오래돼서 stale해지는" 걱정 없이, 같은 실행 안에서만 재사용하면 충분함.
#  - 실행 중간에 시트 내용이 바뀌는 경우까지 반영할 필요는 이 프로젝트 성격상 없다고 판단(주 1회 실행, 실행 도중 편집 반영을 요구하는 요구사항 없음).
_cache: dict[str, list[dict] | None] = {}


def _fetch_csv_rows(csv_url: str) -> list[dict] | None:
    """
    구글 시트 게시 CSV URL을 가져와서 dict 리스트로 파싱한다.
    실패하면(네트워크 오류, 형식 이상 등) None을 반환 - 호출부가 fallback.

    같은 csv_url에 대해 이 프로세스 안에서 이미 한 번 가져온 적 있으면, 네트워크 요청/파싱을 다시 하지 않고 캐시된 결과를 그대로 돌려준다.
    """
    if csv_url in _cache:
        cached = _cache[csv_url]
        print(f"[keyword_source] 캐시된 CSV 재사용 (이번 실행에서 이미 가져온 적 있음)")
        return cached

    try:
        resp = requests.get(csv_url, timeout=30)
        resp.raise_for_status()
        # 구글 시트가 게시하는 CSV는 UTF-8 BOM이 붙어서 오는 경우가 많아 utf-8-sig로 디코딩해야 헤더 첫 컬럼명 앞에 BOM이 안 섞여 들어옴
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
    키워드 문자열 자체의 글자 구성으로 실제 언어를 판별한다("ko" 또는 "en").
    시트의 lang 컬럼은 사람이 손으로 입력하는 값이라 실수로 뒤바뀔 수 있음 방지.

    시트의 lang 컬럼을 그대로 믿는 대신, 키워드 안에 한글(가-힣) 비율이 일정 수준 이상이면 "ko", 아니면 "en"으로 판정해서 실제 내용 기준으로 올바른 수집기(naver_collector/gdelt_collector)로 가도록 한다
      - scorer._is_korean_title()과 같은 방식(한글 유니코드 비율).
    """
    if not keyword:
        return "en"
    hangul_count = sum(1 for ch in keyword if "\uac00" <= ch <= "\ud7a3")
    return "ko" if (hangul_count / len(keyword)) >= 0.2 else "en"


def _is_valid_keyword(keyword: str) -> bool:
    """
    키워드가 실제 검색어로 쓸 만한지(문자/숫자를 하나라도 포함하는지) 확인한다.
    즉, 특수문자만 존재하는 경우를 제거한다. (예: ";", "   ", "---" 등)

    `str.isalnum()`은 한글 음절도 True로 판정하므로(예: '가'.isalnum() == True) 한글/영문/숫자 키워드는 전부 정상 통과하고, 구두점·기호·공백만 있는 경우만 걸러진다.
    """
    return any(ch.isalnum() for ch in keyword)


def get_keywords(lang: str, fallback: list[str]) -> list[str]:
    """
    lang("ko" 또는 "en")에 해당하는 활성(active=TRUE) 키워드 리스트를 구글 시트에서 읽어온다.

    KEYWORD_SHEET_CSV_URL 환경변수가 없거나 읽기/파싱에 실패하거나 해당 lang의 활성 키워드가 하나도 없으면, fallback(호출부가 원래 갖고 있던 하드코딩 리스트)을 그대로 반환한다.
      - 이 함수가 예외를 던지는 경우는 없음.
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

    # 중복 키워드 제거
    #  - 시트에 같은 키워드가 실수로 두 번 이상 등록되면 naver_collector/gdelt_collector가 그 키워드로 API를 중복 호출하게 돼서(운영 중 쌓이면 GDELT 429/일일 호출량 부담만 늘어나고 얻는 건 없음), 여기서 한 번만 걸러내면 모든 호출부가 자동으로 혜택을 본다.
    # 공백 차이("사료 가격" vs "사료  가격")나 대소문자 차이("Feed Price" vs "feed price")도 실질적으로 같은 검색어이므로 정규화해서 비교하고, 실제 검색에 쓰는 표기는 시트에 먼저 등장한 쪽을 그대로 유지한다.
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