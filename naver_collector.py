"""naver_collector.py - 네이버 뉴스 검색 API 수집 모듈."""

import re
import html
import os
import time
import requests
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

import keyword_source

load_dotenv()

# NAVER Developers Center -> NAVER API HUB(NCP API Gateway) 이관됨. 인증정보도 신규 발급 필요.
NAVER_API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"

KEYWORDS = ["인공지능", "AI", "머신러닝", "딥러닝", "챗GPT", "생성형 AI", "자율주행", "로봇", "드론", "메타버스", "가상현실", "증강현실", "블록체인", "NFT", "핀테크", "빅데이터", "클라우드", "사이버보안", "양자컴퓨팅"]

_KST = timezone(timedelta(hours=9))


def _default_window(reference: datetime | None = None) -> tuple[datetime, datetime]:
    """main.py가 구간을 안 넘겨줬을 때(단독 테스트) 쓰는 기본 롤링 24시간 윈도우."""
    now = reference or datetime.now(timezone.utc)
    return now - timedelta(days=1), now


def _is_in_window(published_at: str, window_start: datetime, window_end: datetime) -> bool:
    pub_dt = datetime.fromisoformat(published_at)
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    return window_start <= pub_dt < window_end


MAX_START = 1000  # 네이버 API가 허용하는 start의 최댓값


def _phrase_present(article: dict, keyword: str) -> bool:
    """네이버 API는 공백 포함 검색어를 단어별 AND로 처리 - 여러 단어 키워드만 인접 등장 재확인."""
    if " " not in keyword.strip():
        return True
    combined = f"{article.get('title', '')} {article.get('description', '')}"
    combined_normalized = " ".join(combined.split())
    keyword_normalized = " ".join(keyword.split())
    return keyword_normalized in combined_normalized


def collect(window_start: datetime | None = None, window_end: datetime | None = None) -> list[dict]:
    """KEYWORDS(또는 시트)를 순회하며 네이버 뉴스를 수집(진입점)."""
    if window_start is None or window_end is None:
        window_start, window_end = _default_window()

    client_id = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]
    target_keywords = keyword_source.get_keywords("ko", KEYWORDS)

    all_results = []
    with requests.Session() as session:
        for keyword in target_keywords:
            keyword_results = []
            start = 1
            try:
                while start <= MAX_START:
                    page = search_naver_news(keyword, client_id, client_secret, start=start, session=session)
                    if not page:
                        break

                    in_window = [r for r in page if _is_in_window(r["published_at"], window_start, window_end)]
                    phrase_ok = [r for r in in_window if _phrase_present(r, keyword)]
                    filtered_out = len(in_window) - len(phrase_ok)
                    if filtered_out:
                        print(f"[naver] '{keyword}' - 문구 인접성 필터로 {filtered_out}건 제외")
                    keyword_results.extend(phrase_ok)

                    oldest_dt = datetime.fromisoformat(page[-1]["published_at"])
                    if oldest_dt < window_start:
                        break
                    if len(page) < 100:
                        break
                    start += 100
                    time.sleep(0.2)
            except requests.exceptions.RequestException as e:
                print(f"[naver] 🔴 조치필요 [NV-01] - '{keyword}' 수집 중 오류(기존 {len(keyword_results)}건 보존): "
                      f"{type(e).__name__} - {e!r}")

            all_results.extend(keyword_results)
            print(f"[naver] '{keyword}' -> 구간 내 {len(keyword_results)}건")
            time.sleep(0.2)

    return all_results


def search_naver_news(keyword: str, client_id: str, client_secret: str, start: int = 1,
                       session: requests.Session | None = None) -> list[dict]:
    """네이버 뉴스 검색 API 호출, 공통 스키마로 반환."""
    headers = {
        "X-NCP-APIGW-API-KEY-ID": client_id,
        "X-NCP-APIGW-API-KEY": client_secret,
    }
    params = {"query": keyword, "display": 100, "start": start, "sort": "date"}

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
            "category": None,
            "body": None,
            "description": _strip_html_tags(item["description"]),
            "press": _extract_press(item["originallink"]),
        })
    return results


def _parse_pub_date(pub_date_str: str) -> str:
    """RFC 822 -> ISO 8601."""
    dt = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %z")
    return dt.isoformat()


def _extract_press(originallink: str) -> str:
    """URL 도메인을 언론사 식별자로 사용."""
    if not originallink:
        return ""
    domain = urlparse(originallink).netloc
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _strip_html_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


if __name__ == "__main__":
    results = collect()
    print(f"\n총 {len(results)}건 수집 완료")
    for r in results[:3]:
        print(r)