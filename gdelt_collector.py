"""
gdelt_collector.py - GDELT DOC 2.0 API(gdeltdoc)로 키워드별 해외 언급 데이터 수집.
naver_collector와 달리 tuple(articles, timeline)을 반환(timeline은 현재 항상 빈 dict).
"""

import json
import os
import re as _re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
import requests.sessions
import requests.utils as _requests_utils

import keyword_source
from gdeltdoc import GdeltDoc, Filters
from gdeltdoc.errors import RateLimitError


# gdeltdoc이 헤더 주입을 지원 안 해서 requests 기본 헤더를 오버라이드(User-Agent 없으면 429
# 유발 이슈 확인됨). 프로세스 전역 부작용 - naver_collector의 요청에도 이 UA가 섞여 들어감
# (인증은 별도 헤더 기반이라 영향 없음).
_GDELT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

if not getattr(requests.utils, "_gdelt_ua_patched", False):
    _original_default_headers = _requests_utils.default_headers

    def _default_headers_with_gdelt_ua():
        headers = _original_default_headers()
        headers["User-Agent"] = _GDELT_USER_AGENT
        return headers

    requests.utils.default_headers = _default_headers_with_gdelt_ua
    requests.sessions.default_headers = _default_headers_with_gdelt_ua
    requests.utils._gdelt_ua_patched = True


# fallback 키워드(구글 시트 우선, 실패 시 이거). GDELT는 영문 검색 기본이라 영문으로 구성.
KEYWORDS_EN = [
    "artificial intelligence",
    "nvidia",
    "semiconductor",
    "chatgpt",
    "openai",
]

# --- 학습형 스킵 목록: 같은 키워드에서 ValueError 누적 2회면 자동 스킵 ---
SKIP_STATE_PATH = "state/gdelt_skip_keywords.json"
SKIP_STATE_FAILURE_THRESHOLD = 2

_value_error_keywords_this_run: list[str] = []


def _load_skip_state(path: str = SKIP_STATE_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            print(f"[gdelt] 🟡 주의 [GD-01] - 학습된 스킵 상태 파일 구조 이상 - 빈 상태로 시작: {path}")
            return {}
        return state
    except (OSError, json.JSONDecodeError) as e:
        print(f"[gdelt] 학습된 스킵 상태 파일 읽기 실패(정상, 빈 상태로 시작): {path} - {type(e).__name__} - {e!r}")
        return {}


def _save_skip_state(state: dict, path: str = SKIP_STATE_PATH) -> None:
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"[gdelt] 학습된 스킵 상태 저장 완료 -> {path}")
    except OSError as e:
        print(f"[gdelt] 🟡 주의 [GD-02] - 학습된 스킵 상태 저장 실패: {path} - {type(e).__name__} - {e!r}")


def _update_skip_state_after_run() -> None:
    """collect() 끝에서 호출 - 이번 실행 중 ValueError 발생 횟수만큼 fail_count 누적."""
    if not _value_error_keywords_this_run:
        return

    from collections import Counter
    occurrence_counts = Counter(_value_error_keywords_this_run)

    state = _load_skip_state()
    now_str = datetime.now(timezone.utc).isoformat()
    for keyword, occurrences in occurrence_counts.items():
        entry = state.get(keyword)
        if not isinstance(entry, dict):
            entry = {"fail_count": 0}
        entry["fail_count"] = entry.get("fail_count", 0) + occurrences
        entry["reason"] = "GDELT API가 'phrase too short' 등으로 쿼리 자체를 거부함 (자동 학습됨)"
        entry["last_seen"] = now_str
        state[keyword] = entry
        if occurrences > 1:
            print(f"[gdelt] '{keyword}' - 이번 실행에서 ValueError {occurrences}회 발생 확인")
        if entry["fail_count"] >= SKIP_STATE_FAILURE_THRESHOLD:
            print(f"[gdelt] 🟡 주의 [GD-03] - '{keyword}' - 누적 ValueError {entry['fail_count']}회 - 다음 실행부터 자동 스킵")

    _save_skip_state(state)


# --- 학습형 크라우딩 목록: 250건 상한 근처가 2회 연속이면 배치 없이 바로 개별 요청 ---
CROWDING_STATE_PATH = "state/gdelt_crowding_keywords.json"
CROWDING_STATE_LEARN_THRESHOLD = 2

_crowded_keywords_this_run: list[str] = []


def _load_crowding_state(path: str = CROWDING_STATE_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            print(f"[gdelt] 🟡 주의 - 학습된 크라우딩 상태 파일 구조 이상 - 빈 상태로 시작: {path}")
            return {}
        return state
    except (OSError, json.JSONDecodeError) as e:
        print(f"[gdelt] 학습된 크라우딩 상태 파일 읽기 실패(정상, 빈 상태로 시작): {path} - {type(e).__name__} - {e!r}")
        return {}


def _save_crowding_state(state: dict, path: str = CROWDING_STATE_PATH) -> None:
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"[gdelt] 학습된 크라우딩 상태 저장 완료 -> {path}")
    except OSError as e:
        print(f"[gdelt] 🟡 주의 - 학습된 크라우딩 상태 저장 실패: {path} - {type(e).__name__} - {e!r}")


def _update_crowding_state_after_run() -> None:
    """이미 임계값 도달한 키워드는 더 이상 카운트 안 올림(last_seen만 갱신)."""
    if not _crowded_keywords_this_run:
        return

    state = _load_crowding_state()
    now_str = datetime.now(timezone.utc).isoformat()
    for keyword in dict.fromkeys(_crowded_keywords_this_run):
        entry = state.get(keyword)
        if not isinstance(entry, dict):
            entry = {"crowd_count": 0}
        if entry.get("crowd_count", 0) < CROWDING_STATE_LEARN_THRESHOLD:
            entry["crowd_count"] = entry.get("crowd_count", 0) + 1
            if entry["crowd_count"] >= CROWDING_STATE_LEARN_THRESHOLD:
                print(f"[gdelt] '{keyword}' - {entry['crowd_count']}회 연속 상한 근처 - 다음 실행부터 배치 없이 개별 요청")
        entry["last_seen"] = now_str
        state[keyword] = entry

    _save_crowding_state(state)


# --- 키워드 오매칭(false positive) 필터: 실제 확인된 것만 명시적 등록 ---
FALSE_POSITIVE_FILTERS = {}

_SPACE_BEFORE_PUNCT = _re.compile(r"\s+([,.;:!?])")


def _normalize_spacing(text: str) -> str:
    """구두점 앞 공백 제거(GDELT 제목이 "hand , foot" 형태로 옴)."""
    return _SPACE_BEFORE_PUNCT.sub(r"\1", text)


def _is_false_positive(keyword: str, title: str) -> bool:
    patterns = FALSE_POSITIVE_FILTERS.get(keyword, [])
    if not patterns or not title:
        return False
    title_normalized = _normalize_spacing(title.lower())
    return any(_normalize_spacing(p.lower()) in title_normalized for p in patterns)


DAYS_BACK = 1  # standalone 기본값(단독 테스트용) - 정상 실행은 main.py의 window_start/end
MAX_RECORDS = 250  # GDELT article_search 1회 호출 한도(API 레벨 한계, 페이지네이션 없음)


def _default_window(reference: datetime | None = None) -> tuple[datetime, datetime]:
    """window_start/end를 못 받았을 때 쓰는 롤링 24시간 윈도우(단독 테스트용)."""
    now = reference or datetime.now(timezone.utc)
    return now - timedelta(days=DAYS_BACK), now


def _is_in_window(dt: datetime, window_start: datetime, window_end: datetime) -> bool:
    return window_start <= dt < window_end


# GH 러너 Job 하드 캡 360분 중 collect Job 자체 몫(350분)과 동일 - standalone 기본값,
# 정상 실행은 호출부의 deadline이 우선.
TIME_BUDGET_SECONDS = 5 * 60 * 60 + 50 * 60

# 배치 크기 - 작을수록 크라우딩 적지만 요청 많아짐(잠정값).
BATCH_SIZE = 5

# 키워드 사이 요청 간격(짧으면 429 누적).
REQUEST_INTERVAL = 15.0

# 429 재시도: 60->120->240->480초(누적 900초).
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 60

# 네트워크 에러 재시도(429보다 짧게).
NETWORK_ERROR_MAX_RETRIES = 2
NETWORK_ERROR_WAIT_SECONDS = 10

# 외부 재시도(outer retry): 실패 키워드만 별도 라운드로 재시도. 총 시도 = 1 + OUTER_RETRY_PASSES.
OUTER_RETRY_PASSES = 2
OUTER_RETRY_WAIT_SECONDS = 90

# 전역(프로세스 공유) 쿨다운 - 429 만나면 이후 모든 호출이 같은 차단 구간을 공유해서 대기.
_cooldown_until = 0.0
_cooldown_lock = threading.Lock()


def _wait_for_cooldown():
    with _cooldown_lock:
        remaining = _cooldown_until - time.time()
    if remaining > 0:
        print(f"[gdelt] 전역 쿨다운 중 - {remaining:.0f}초 남음, 대기")
        time.sleep(remaining)


def _trigger_cooldown(seconds: float):
    """쿨다운 세팅(연장만, 단축 안 함)."""
    global _cooldown_until
    with _cooldown_lock:
        new_until = time.time() + seconds
        if new_until > _cooldown_until:
            _cooldown_until = new_until


def _parse_retry_after(response) -> float | None:
    """429 응답의 Retry-After 헤더 파싱. 없으면 None(호출부가 지수 백오프로 fallback)."""
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if not value:
        return None

    try:
        return float(value)
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime
        retry_dt = parsedate_to_datetime(value)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        seconds = (retry_dt - datetime.now(timezone.utc)).total_seconds()
        return max(seconds, 0.0)
    except (TypeError, ValueError):
        return None


def _call_with_retry(func, *args, label: str = "", **kwargs):
    """429/네트워크 에러를 서로 다른 정책으로 재시도. 그 외 예외는 바로 올려보냄."""
    rate_limit_attempt = 0
    network_attempt = 0

    while True:
        _wait_for_cooldown()
        try:
            return func(*args, **kwargs)
        except RateLimitError as e:
            if rate_limit_attempt >= MAX_RETRIES:
                raise
            server_wait = _parse_retry_after(getattr(e, "response", None))
            wait = server_wait if server_wait is not None else BACKOFF_BASE_SECONDS * (2 ** rate_limit_attempt)
            rate_limit_attempt += 1
            now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"[gdelt] {now_str} - {label} - 429 rate limit - {wait:.0f}초 전역 쿨다운 설정 "
                  f"({rate_limit_attempt}/{MAX_RETRIES})")
            _trigger_cooldown(wait)
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError):
            if network_attempt >= NETWORK_ERROR_MAX_RETRIES:
                raise
            network_attempt += 1
            print(f"[gdelt] {label} - 접속 실패 - {NETWORK_ERROR_WAIT_SECONDS}초 대기 후 재시도 "
                  f"({network_attempt}/{NETWORK_ERROR_MAX_RETRIES})")
            time.sleep(NETWORK_ERROR_WAIT_SECONDS)


def _parse_seendate(raw: str) -> datetime:
    """"YYYYMMDDHHMMSS"/"...Z"/ISO 8601 순서로 시도."""
    raw = raw.strip()

    for fmt in ("%Y%m%d%H%M%S", "%Y%m%dT%H%M%SZ"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass

    raise ValueError(f"seendate 파싱 실패, 형식 확인 필요: {raw!r}")


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    domain = urlparse(url).netloc
    return domain.replace("www.", "")


def _collect_articles_for_keyword(gd: "GdeltDoc", keyword: str,
                                   window_start: datetime, window_end: datetime) -> tuple[bool, list[dict], str | None]:
    """한 키워드의 article_search. 반환: (성공 여부, 기사 리스트, 실패 사유)."""
    f = Filters(keyword=keyword, start_date=window_start, end_date=window_end, num_records=MAX_RECORDS)
    keyword_articles = []
    false_positive_count = 0

    try:
        articles_df = _call_with_retry(gd.article_search, f, label=f"{keyword} / article_search")

        if articles_df is not None and not articles_df.empty:
            for _, row in articles_df.iterrows():
                if _is_false_positive(keyword, str(row["title"])):
                    false_positive_count += 1
                    continue

                try:
                    published_at = _parse_seendate(str(row["seendate"]))
                except ValueError as e:
                    print(f"[gdelt] 🟡 주의 [GD-04] - '{keyword}' 기사 스킵 - {type(e).__name__} - {e!r}")
                    continue

                if not _is_in_window(published_at, window_start, window_end):
                    continue

                keyword_articles.append({
                    "source": "GDELT",
                    "title": row["title"],
                    "url": row["url"],
                    "published_at": published_at.isoformat(),
                    "category": None,
                    "body": None,
                    "press": _extract_domain(row["url"]),
                    "language": row.get("language", ""),
                    "sourcecountry": row.get("sourcecountry", ""),
                })

        fp_note = f" (오매칭 필터로 {false_positive_count}건 제외)" if false_positive_count else ""
        print(f"[gdelt] '{keyword}' article_search -> 구간 내 {len(keyword_articles)}건 수집 완료{fp_note}")
        return True, keyword_articles, None

    except ValueError as e:
        print(f"[gdelt] 🟡 주의 [GD-05] - '{keyword}' article_search 실패(쿼리 자체 거부로 추정 - {type(e).__name__}: {e})")
        _value_error_keywords_this_run.append(keyword)
        return False, [], f"{type(e).__name__}: {e}"

    except Exception as e:
        print(f"[gdelt] 🟡 주의 [GD-06] - '{keyword}' article_search 실패: {type(e).__name__} - {e!r}")
        return False, [], f"{type(e).__name__}: {e}"


def _collect_articles_for_keywords(gd: "GdeltDoc", keywords: list[str],
                                    window_start: datetime, window_end: datetime) -> tuple[bool, list[dict]]:
    """여러 키워드를 OR로 묶어 article_search를 한 번만 호출."""
    label = " OR ".join(keywords)
    f = Filters(keyword=keywords, start_date=window_start, end_date=window_end, num_records=MAX_RECORDS)
    combined_articles = []
    false_positive_count = 0

    try:
        articles_df = _call_with_retry(gd.article_search, f, label=f"{label} / article_search")

        if articles_df is not None and not articles_df.empty:
            for _, row in articles_df.iterrows():
                title = str(row["title"])
                if any(_is_false_positive(kw, title) for kw in keywords):
                    false_positive_count += 1
                    continue

                try:
                    published_at = _parse_seendate(str(row["seendate"]))
                except ValueError as e:
                    print(f"[gdelt] 🟡 주의 [GD-07] - '{label}' 기사 스킵 - {type(e).__name__} - {e!r}")
                    continue

                if not _is_in_window(published_at, window_start, window_end):
                    continue

                combined_articles.append({
                    "source": "GDELT",
                    "title": row["title"],
                    "url": row["url"],
                    "published_at": published_at.isoformat(),
                    "category": None,
                    "body": None,
                    "press": _extract_domain(row["url"]),
                    "language": row.get("language", ""),
                    "sourcecountry": row.get("sourcecountry", ""),
                })

        fp_note = f" (오매칭 필터로 {false_positive_count}건 제외)" if false_positive_count else ""
        print(f"[gdelt] '{label}' article_search -> 구간 내 {len(combined_articles)}건 수집 완료{fp_note}")

        # 키워드별 매칭 현황(제목 기준 근사치 - 본문 매칭은 집계에서 빠짐, 추가 API 호출 없이 사후 대조)
        print(f"[gdelt] 키워드별 매칭 현황:")
        for kw in keywords:
            kw_lower = kw.lower()
            count = sum(1 for a in combined_articles if kw_lower in a["title"].lower())
            print(f"  - '{kw}': {count}건")
        return True, combined_articles

    except ValueError as e:
        # "phrase too short" 등 쿼리 자체 거부는 재시도해도 100% 재실패 - 즉시 개별 요청으로 전환
        print(f"[gdelt] 🟡 주의 [GD-08] - '{label}' article_search 실패(쿼리 자체 거부로 추정 - "
              f"{type(e).__name__}: {e}) - 재시도 대신 키워드별 개별 요청으로 즉시 전환")
        return _collect_articles_individually(gd, keywords, window_start, window_end)

    except Exception as e:
        print(f"[gdelt] 🟡 주의 [GD-09] - '{label}' article_search 실패: {type(e).__name__} - {e!r}")
        return False, []


def _collect_articles_individually(gd: "GdeltDoc", keywords: list[str],
                                    window_start: datetime, window_end: datetime) -> tuple[bool, list[dict]]:
    """결합 쿼리가 확정적으로 실패했을 때 키워드 하나씩 개별 요청하는 격리 폴백."""
    all_articles = []
    any_success = False
    for keyword in keywords:
        success, keyword_articles, _reason = _collect_articles_for_keyword(gd, keyword, window_start, window_end)
        if success:
            any_success = True
            all_articles.extend(keyword_articles)
        time.sleep(REQUEST_INTERVAL)
    return any_success, all_articles


def collect(keywords: list[str] | None = None, deadline: float | None = None,
            window_start: datetime | None = None, window_end: datetime | None = None) -> tuple[list[dict], dict]:
    """
    GDELT에서 기사 메타데이터 수집(진입점).

    적응형 배치 수집: 키워드를 BATCH_SIZE개씩 OR로 묶어 요청, 결과가 정확히 MAX_RECORDS에
    도달하면 크라우딩으로 보고 그 배치 전체를 개별 재요청으로 보충. 배치 요청 자체가
    실패하면(429 등) 같은 배치로 외부 재시도, 그래도 안 되면 최후 수단으로 개별 전환.

    반환값: articles(공통 스키마 리스트), timeline(현재 항상 빈 dict).
    """
    if window_start is None or window_end is None:
        window_start, window_end = _default_window()

    gd = GdeltDoc()
    target_keywords = keywords if keywords is not None else keyword_source.get_keywords("en", KEYWORDS_EN)

    _value_error_keywords_this_run.clear()
    _crowded_keywords_this_run.clear()
    skip_state = _load_skip_state()
    crowding_state = _load_crowding_state()

    active_keywords = []
    known_crowders = []
    for keyword in target_keywords:
        learned_entry = skip_state.get(keyword)
        if isinstance(learned_entry, dict) and learned_entry.get("fail_count", 0) >= SKIP_STATE_FAILURE_THRESHOLD:
            print(f"[gdelt] '{keyword}' 스킵 - 학습형 스킵 목록 등재됨 "
                  f"({learned_entry.get('fail_count')}회 연속 ValueError 확인)")
            continue
        crowding_entry = crowding_state.get(keyword)
        if isinstance(crowding_entry, dict) and crowding_entry.get("crowd_count", 0) >= CROWDING_STATE_LEARN_THRESHOLD:
            print(f"[gdelt] '{keyword}' - 학습된 크라우딩 키워드 - 배치 없이 바로 개별 요청으로 처리")
            known_crowders.append(keyword)
            continue
        active_keywords.append(keyword)

    all_articles = []
    timeline_by_keyword = {}

    if not active_keywords and not known_crowders:
        return all_articles, timeline_by_keyword

    effective_deadline = deadline if deadline is not None else time.monotonic() + TIME_BUDGET_SECONDS
    budget_exceeded = False
    skipped_due_to_budget: list[str] = []

    def _over_budget() -> bool:
        return time.monotonic() >= effective_deadline

    # --- 1단계: 배치 단위 수집 + 크라우딩 감지 ---
    batches = [active_keywords[i:i + BATCH_SIZE] for i in range(0, len(active_keywords), BATCH_SIZE)]
    pending_individual: list[str] = list(known_crowders)
    pending_batches: list[list[str]] = []

    for batch_idx, batch in enumerate(batches):
        if _over_budget():
            remaining = [kw for b in batches[batch_idx:] for kw in b]
            skipped_due_to_budget.extend(remaining)
            budget_exceeded = True
            print(f"[gdelt] 🟡 주의 - 시간 예산 소진 - 남은 배치 {len(batches) - batch_idx}개는 건너뜀")
            break

        if len(batch) == 1:
            pending_individual.append(batch[0])
            continue

        success, batch_articles = _collect_articles_for_keywords(gd, batch, window_start, window_end)
        if not success:
            print(f"[gdelt] 배치 {batch} 요청 실패 - 같은 배치로 재시도 예정(개별 전환 아님)")
            pending_batches.append(batch)
        else:
            all_articles.extend(batch_articles)
            if len(batch_articles) >= MAX_RECORDS:
                print(f"[gdelt] 배치 {batch} 결과가 상한({MAX_RECORDS}건)에 도달함 - 배치 전체 개별 재요청으로 보충 예정")
                pending_individual.extend(batch)
        time.sleep(REQUEST_INTERVAL)

    # --- 1-보조단계: 배치 요청 실패분을 같은 배치로 재시도 ---
    if budget_exceeded:
        skipped_due_to_budget.extend(kw for b in pending_batches for kw in b)
        pending_batches = []

    batch_round = pending_batches
    for round_num in range(1, OUTER_RETRY_PASSES + 1):
        if not batch_round:
            break
        if _over_budget():
            remaining = [kw for b in batch_round for kw in b]
            skipped_due_to_budget.extend(remaining)
            budget_exceeded = True
            print(f"[gdelt] 🟡 주의 - 시간 예산 소진 - 배치 재시도 중단(키워드 {len(remaining)}개는 건너뜀)")
            batch_round = []
            break
        print(f"[gdelt] --- 배치 재시도 라운드 {round_num}/{OUTER_RETRY_PASSES} - 이전 라운드 실패 배치 {len(batch_round)}개 ---")
        print(f"[gdelt] 라운드 간 안전 대기 {OUTER_RETRY_WAIT_SECONDS}초")
        time.sleep(OUTER_RETRY_WAIT_SECONDS)

        still_failed_batches = []
        for batch_idx2, batch in enumerate(batch_round):
            if _over_budget():
                remaining = [kw for b in batch_round[batch_idx2:] for kw in b]
                skipped_due_to_budget.extend(remaining)
                budget_exceeded = True
                print(f"[gdelt] 🟡 주의 - 시간 예산 소진 - 배치 재시도 라운드 도중 중단(키워드 {len(remaining)}개는 건너뜀)")
                still_failed_batches = []
                break
            success, batch_articles = _collect_articles_for_keywords(gd, batch, window_start, window_end)
            if success:
                all_articles.extend(batch_articles)
                if len(batch_articles) >= MAX_RECORDS:
                    pending_individual.extend(batch)
            else:
                still_failed_batches.append(batch)
            time.sleep(REQUEST_INTERVAL)
        batch_round = still_failed_batches

    if batch_round:
        print(f"[gdelt] 🟡 주의 [GD-10] - 배치 재시도 {OUTER_RETRY_PASSES}회 소진 - 개별 요청 전환: {batch_round}")
        for batch in batch_round:
            pending_individual.extend(batch)

    # --- 2단계: 개별 보충 요청 - 실패한 것만 외부 재시도 라운드 ---
    if budget_exceeded:
        skipped_due_to_budget.extend(pending_individual)
        pending_individual = []

    round_keywords = list(dict.fromkeys(pending_individual))
    failed_keywords: list[str] = []
    failure_reasons: dict[str, str] = {}

    for round_num in range(OUTER_RETRY_PASSES + 1):
        if round_num > 0:
            if not failed_keywords:
                break
            print(f"[gdelt] --- 기사 수집 외부 재시도 라운드 {round_num}/{OUTER_RETRY_PASSES} - "
                  f"이전 라운드 실패 키워드 {len(failed_keywords)}개: {failed_keywords} ---")
            print(f"[gdelt] 라운드 간 안전 대기 {OUTER_RETRY_WAIT_SECONDS}초")
            time.sleep(OUTER_RETRY_WAIT_SECONDS)
            round_keywords = failed_keywords

        if not round_keywords:
            break

        failed_keywords = []

        for kw_idx, keyword in enumerate(round_keywords):
            if _over_budget():
                remaining = round_keywords[kw_idx:]
                skipped_due_to_budget.extend(remaining)
                print(f"[gdelt] 🟡 주의 - 시간 예산 소진 - 개별 요청 중단(키워드 {len(remaining)}개는 건너뜀)")
                failed_keywords = []
                round_keywords = []
                break

            success, keyword_articles, reason = _collect_articles_for_keyword(gd, keyword, window_start, window_end)
            if success:
                all_articles.extend(keyword_articles)
                failure_reasons.pop(keyword, None)
                if len(keyword_articles) >= MAX_RECORDS:
                    _crowded_keywords_this_run.append(keyword)
            else:
                failed_keywords.append(keyword)
                failure_reasons[keyword] = reason or "사유 불명"
            time.sleep(REQUEST_INTERVAL)

        if not round_keywords:
            break

    if failed_keywords:
        detail = ", ".join(f"{kw} ({failure_reasons.get(kw, '사유 불명')})" for kw in failed_keywords)
        print(f"[gdelt] 🔴 조치필요 [GD-11] - 최종 실패 키워드(기사 0건으로 처리됨): {detail}")

    if skipped_due_to_budget:
        unique_skipped = list(dict.fromkeys(skipped_due_to_budget))
        print(f"[gdelt] 🟡 주의 - 시간 예산 초과로 미시도 키워드 {len(unique_skipped)}개: {unique_skipped}")

    _update_skip_state_after_run()
    _update_crowding_state_after_run()

    return all_articles, timeline_by_keyword