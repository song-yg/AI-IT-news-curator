"""
naver_collector.py
네이버 뉴스 검색 API로 뉴스를 수집하는 모듈 (수집 레이어).
"""

import re
import html
import os
import time
import requests
from urllib.parse import urlparse
from datetime import datetime, timedelta
from dotenv import load_dotenv

import keyword_source

# .env 파일이 있으면 환경변수로 등록 (없어도 에러 없음 - GitHub Actions는 Secrets가 이미 주입돼 있어 무시됨)
load_dotenv()

NAVER_API_URL = "https://openapi.naver.com/v1/search/news.json"

# 매일 새벽 실행이므로 전날(최근 1일) 이내 기사만 남긴다.
# (예전엔 주 1회 실행이라 7일이었음 - 일간 전환하면서 변경)
DAYS_BACK = 1

# fallback 키워드. 구글 시트(KEYWORD_SHEET_CSV_URL)가 있으면 그쪽 우선, 없거나 실패 시 이 리스트 사용.
# 시트가 바뀌면 이것도 수동으로 같이 갱신해야 함 (자동 동기화 아님).
KEYWORDS = ["인공지능", "AI", "머신러닝", "딥러닝", "챗GPT", "생성형 AI", "자율주행", "로봇", "드론", "메타버스", "가상현실", "증강현실", "블록체인", "NFT", "핀테크", "빅데이터", "클라우드", "사이버보안", "양자컴퓨팅"]


def _is_recent(published_at: str, days: int) -> bool:
    """published_at(ISO 8601)이 최근 N일 이내인지 확인."""
    pub_dt = datetime.fromisoformat(published_at)
    cutoff = datetime.now(pub_dt.tzinfo) - timedelta(days=days)
    return pub_dt >= cutoff


# 네이버 API가 허용하는 start의 최댓값
MAX_START = 1000


def _phrase_present(article: dict, keyword: str) -> bool:
    """
    네이버 검색 API는 공백 포함 검색어를 "정확한 문구"가 아니라 "단어별 AND"로 처리한다.
    "수급"처럼 범용적인 단어가 섞인 키워드는 무관한 기사가 섞여 들어올 수 있어서,
    공백이 있는(여러 단어) 키워드만 제목+요약에 실제로 인접해서 등장하는지 재확인한다.
    단어 하나짜리는 오탐 여지가 없으므로 항상 통과.
    """
    if " " not in keyword.strip():
        return True
    combined = f"{article.get('title', '')} {article.get('description', '')}"
    combined_normalized = " ".join(combined.split())
    keyword_normalized = " ".join(keyword.split())
    return keyword_normalized in combined_normalized


def collect() -> list[dict]:
    """KEYWORDS를 순서대로 돌며 네이버 뉴스를 전부 수집 (진입점)."""
    client_id = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]

    # 구글 시트 활성 키워드 우선, 실패 시 하드코딩 KEYWORDS로 안전하게 대체
    target_keywords = keyword_source.get_keywords("ko", KEYWORDS)

    all_results = []
    # 세션 재사용 - 키워드마다 여러 페이지를 반복 호출하므로 커넥션 유지로 오버헤드 절감
    with requests.Session() as session:
        for keyword in target_keywords:
            # try 범위는 네트워크 호출 부분만 - 페이지네이션 도중 예외가 나도 이미 모은 결과는 보존
            keyword_results = []
            start = 1
            try:
                while start <= MAX_START:
                    page = search_naver_news(keyword, client_id, client_secret, start=start, session=session)

                    if not page:
                        break  # 더 가져올 결과 없음

                    recent_in_page = [r for r in page if _is_recent(r["published_at"], DAYS_BACK)]
                    phrase_ok = [r for r in recent_in_page if _phrase_present(r, keyword)]
                    filtered_out = len(recent_in_page) - len(phrase_ok)
                    if filtered_out:
                        print(f"[naver] '{keyword}' - 문구 인접성 필터로 {filtered_out}건 제외"
                              f"(AND 매칭 오탐 방지)")
                    keyword_results.extend(phrase_ok)

                    # 마지막 항목 = 이 페이지에서 가장 오래된 기사 (sort=date라 항상 마지막이 제일 오래됨)
                    oldest_in_page = page[-1]
                    if not _is_recent(oldest_in_page["published_at"], DAYS_BACK):
                        break  # 기간을 벗어났으니 다음 페이지는 더 볼 필요 없음

                    if len(page) < 100:
                        break  # 100건 미만 = 더 이상 결과 없음

                    start += 100
                    time.sleep(0.2)
            except requests.exceptions.RequestException as e:
                print(f"[naver] 🔴 조치필요 [NV-01] - '{keyword}' 수집 중 오류 발생(지금까지 모은 "
                      f"{len(keyword_results)}건은 보존하고 다음 키워드로 진행): {type(e).__name__} - {e!r}")

            all_results.extend(keyword_results)
            print(f"[naver] '{keyword}' -> 최근 {DAYS_BACK}일 이내 {len(keyword_results)}건 "
                  f"({start if start <= MAX_START else MAX_START}건째까지 확인)")

            time.sleep(0.2)

    return all_results


def search_naver_news(keyword: str, client_id: str, client_secret: str, start: int = 1,
                       session: requests.Session | None = None) -> list[dict]:
    """
    네이버 뉴스 검색 API 호출, 공통 스키마로 정리해서 반환.

    session을 안 넘기면 requests 모듈을 그대로 써서 매번 새 연결을 맺는다.
    (세션 재사용은 순전히 성능 최적화, 없어도 기능은 동일)
    """
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    params = {
        "query": keyword,
        "display": 100,   # 한 번에 최대 100건 (API 하드 리밋)
        "start": start,
        "sort": "date",   # 기본값 sim(정확도순) 대신 최신순
    }

    requester = session if session is not None else requests
    response = requester.get(NAVER_API_URL, headers=headers, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()

    results = []
    for item in data.get("items", []):
        results.append({
            "source": "네이버",
            "title": _strip_html_tags(item["title"]),
            "url": item["originallink"] or item["link"],
            "published_at": _parse_pub_date(item["pubDate"]),
            "category": None,   # 정규화 단계에서 채움
            "body": None,       # 네이버는 본문을 안 줌
            "description": _strip_html_tags(item["description"]),
            "press": _extract_press(item["originallink"]),
        })
    return results


def _parse_pub_date(pub_date_str: str) -> str:
    """네이버 pubDate(RFC 822, "Mon, 13 Jul 2026 09:00:00 +0900")를 ISO 8601로 변환."""
    dt = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %z")
    return dt.isoformat()


def _extract_press(originallink: str) -> str:
    """
    네이버 API에 언론사명 필드가 없어서 원문 URL 도메인을 언론사 식별자로 대신 사용.
    예: "https://www.yna.co.kr/..." -> "yna.co.kr"

    www. 제거는 startswith 체크 후 슬라이싱 (replace는 서브도메인 중간에 우연히 "www."가 있어도 지워버릴 수 있어서 위험).

    biz.yna.co.kr / www.yna.co.kr 같은 서브도메인 통합은 안 함 - 정확히 하려면 tldextract 등이 필요함.
    실제 문제(scorer PRESS_DEDUP_CAP 왜곡) 확인되면 대응.
    """
    if not originallink:
        return ""
    domain = urlparse(originallink).netloc
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _strip_html_tags(text: str) -> str:
    """네이버 API 응답의 title/description에 섞인 <b>강조 태그</b>와 HTML 엔티티 제거."""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text


if __name__ == "__main__":
    results = collect()
    print(f"\n총 {len(results)}건 수집 완료")
    for r in results[:3]:
        print(r)