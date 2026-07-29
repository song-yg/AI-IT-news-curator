"""
naver_collector.py
네이버 뉴스 검색 API를 호출해서 뉴스를 수집하는 모듈 (수집 레이어).
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

# 실행 시점에 .env 파일을 찾아서 그 안의 값들을 환경변수로 등록해준다.
# .env 파일이 없어도 에러 없이 그냥 넘어감 (예: GitHub Actions에서는
# .env 없이 Secrets가 이미 환경변수로 주입돼 있으므로 이 줄은 그냥 무시됨)
load_dotenv()

NAVER_API_URL = "https://openapi.naver.com/v1/search/news.json"

# 이 프로젝트는 주 1회 실행이므로, 최근 7일 이내 기사만 남긴다.
DAYS_BACK = 7

# 예시 키워드(fallback). 구글 시트(KEYWORD_SHEET_CSV_URL)가 설정돼 있으면 그쪽을 우선 쓰고, 없거나 읽기 실패하면 이 리스트로 대체된다.
# (keyword_source.py 참고 - 시트 편집만으로 키워드 추가 가능)
#
# 시트가 나중에 바뀌면 이 fallback도 수동으로 같이 갱신해줘야 한다.
# (자동 동기화 아님 - keyword_source.py는 시트 실패 시 이 하드코딩 값을 그대로 쓸 뿐, 시트 변경을 감지해 여기로 역으로 반영하는 기능은 없음).
KEYWORDS = ["인공지능", "AI", "머신러닝", "딥러닝", "챗GPT", "생성형 AI", "자율주행", "로봇", "드론", "메타버스", "가상현실", "증강현실", "블록체인", "NFT", "핀테크", "빅데이터", "클라우드", "사이버보안", "양자컴퓨팅"]


def _is_recent(published_at: str, days: int) -> bool:
    """
    published_at(ISO 8601 문자열)이 오늘 기준 최근 N일 이내인지 확인한다.

    예: DAYS_BACK=7이면, 오늘이 7/13일 때 7/6일 이전 기사는 False가 됨.
    """
    pub_dt = datetime.fromisoformat(published_at)
    # pub_dt에 이미 +09:00 같은 시간대 정보가 들어있으므로, 비교 기준(now)도 같은 시간대로 맞춰줘야 에러 없이 비교 가능하다.
    cutoff = datetime.now(pub_dt.tzinfo) - timedelta(days=days)
    return pub_dt >= cutoff


# 네이버 API가 허용하는 start의 최댓값 (그 이상은 요청해도 에러남)
MAX_START = 1000


def _phrase_present(article: dict, keyword: str) -> bool:
    """
    네이버 뉴스 검색 API는 공백으로 구분된 검색어를 "정확한 문구"가 아니라 "각 단어의 AND"로 처리한다
    - 예를 들어 "축산물 수급"을 검색하면 "축산물"이라는 단어와 "수급"이라는 단어가 같은 기사 안 어디에나 각각 등장하기만 해도 매칭된다.
    "수급"처럼 극도로 범용적인 단어(인력 수급/물량 수급/배우 수급 등 축산과 무관한 맥락에서도 흔히 쓰임)가 섞인 키워드는 이 방식 때문에 무관한 기사(프랜차이즈 기사, 영화 리뷰 등에 "축산물"과 "수급"이 각각 다른 맥락으로 우연히 같이 등장)가 섞여 들어올 수 있다.

    검색어에 공백이 있는(여러 단어로 구성된) 키워드만, 실제로 그 문구가 제목+요약에 붙어서(인접해서) 등장하는지 재확인해서 걸러낸다.
    단어 하나짜리 키워드는 애초에 AND로 인한 오탐 여지가 없으므로 항상 통과.
    """
    if " " not in keyword.strip():
        return True
    combined = f"{article.get('title', '')} {article.get('description', '')}"
    combined_normalized = " ".join(combined.split())  # 연속 공백/줄바꿈 정리
    keyword_normalized = " ".join(keyword.split())
    return keyword_normalized in combined_normalized


def collect() -> list[dict]:
    """
    KEYWORDS 리스트를 순서대로 돌면서 네이버 뉴스를 전부 수집한다.
    이 함수가 naver_collector의 '진입점'(다른 모듈에서 이걸 호출해서 씀).
    """
    # API 키는 코드에 절대 직접 안 적는다. 환경변수로 읽어온다.
    client_id = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]

    # 구글 시트에 등록된 활성 키워드를 우선 사용하고, 시트 미설정/읽기 실패 시 위 KEYWORDS(하드코딩)로 안전하게 대체(keyword_source.py 참고 - 이 함수는 예외를 던지지 않음)
    target_keywords = keyword_source.get_keywords("ko", KEYWORDS)

    all_results = []
    # 세션 재사용: 키워드마다 여러 페이지를 반복 호출하는 구조라(최대 10페이지 x 키워드 수), 세션을 하나만 만들어 재사용하면 커넥션이 유지돼 반복 호출의 오버헤드가 줄어든다.
    with requests.Session() as session:
        for keyword in target_keywords:
            # try 범위는 "네트워크 호출이 실제로 일어나는 부분"으로 좁히고, extend는 항상(성공이든 부분 실패든) 실행되도록 밖에 둔다.
            # - 페이지네이션 도중 예외가 나도 이미 모은 결과는 보존하기 위함.
            keyword_results = []
            start = 1
            try:
                while start <= MAX_START:
                    page = search_naver_news(keyword, client_id, client_secret, start=start, session=session)

                    if not page:
                        # 더 가져올 결과 자체가 없음 (검색어에 대한 기사가 소진됨)
                        break

                    recent_in_page = [r for r in page if _is_recent(r["published_at"], DAYS_BACK)]
                    # AND 매칭 오탐 방지 필터 (_phrase_present docstring 참고) - 여러 단어로 된 키워드만 대상.
                    phrase_ok = [r for r in recent_in_page if _phrase_present(r, keyword)]
                    filtered_out = len(recent_in_page) - len(phrase_ok)
                    if filtered_out:
                        print(f"[naver] '{keyword}' - 문구 인접성 필터로 {filtered_out}건 제외"
                              f"(AND 매칭 오탐 방지)")
                    keyword_results.extend(phrase_ok)

                    # 이 페이지의 마지막 항목 = 이 페이지 안에서 가장 오래된 기사
                    # (sort=date로 최신순 정렬돼 있으므로 항상 마지막이 제일 오래됨)
                    oldest_in_page = page[-1]
                    if not _is_recent(oldest_in_page["published_at"], DAYS_BACK):
                        # 이 페이지 끝에서 이미 기간을 벗어났다 -> 다음 페이지는 이보다 더 오래된 기사만 있을 게 뻔하므로 더 안 가져와도 됨
                        break

                    if len(page) < 100:
                        # 네이버가 100건 미만을 줬다는 건 더 이상 결과가 없다는 뜻
                        break

                    start += 100
                    time.sleep(0.2)  # 페이지 사이에도 간격을 둠
            except requests.exceptions.RequestException as e:
                # 이 키워드의 나머지 페이지는 못 가져왔지만, 지금까지 모은 keyword_results는 아래에서 그대로 살려서 반영한다.
                print(f"[naver] 🔴 조치필요 [NV-01] - '{keyword}' 수집 중 오류 발생(지금까지 모은 "
                      f"{len(keyword_results)}건은 보존하고 다음 키워드로 진행): {type(e).__name__} - {e!r}")

            all_results.extend(keyword_results)
            print(f"[naver] '{keyword}' -> 최근 {DAYS_BACK}일 이내 {len(keyword_results)}건 "
                  f"({start if start <= MAX_START else MAX_START}건째까지 확인)")

            time.sleep(0.2)  # 키워드 사이에도 간격을 둬서 짧은 시간에 몰아치지 않도록 함

    return all_results


def search_naver_news(keyword: str, client_id: str, client_secret: str, start: int = 1,
                       session: requests.Session | None = None) -> list[dict]:
    """
    네이버 뉴스 검색 API를 호출해서, 결과를 우리 공통 스키마 형태로 정리해 돌려준다.

    keyword       : 검색어 (예: "조류독감")
    client_id     : 네이버 개발자센터에서 발급받은 애플리케이션 ID
    client_secret : 위와 함께 발급받은 비밀키
    start         : 몇 번째 결과부터 가져올지 (1, 101, 201 ... 페이지네이션용)
    session       : 재사용할 requests.Session. 안 넘기면(예: 이 함수를 단독으로 테스트할 때) requests 모듈 자체를 그대로 써서 매번 새 연결을 맺는 방식으로 안전하게 동작함.
                    - 세션 재사용은 순전히 성능 최적화라 없어도 기능은 동일함.
    """
    # 1) 헤더: "나 누구인지" 증명하는 정보. API 키가 여기 들어간다.
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    # 2) 파라미터: "무엇을 달라"는 요청 내용
    params = {
        "query": keyword,
        "display": 100,   # 한 번에 최대 100건까지 요청 가능 (API 하드 리밋)
        "start": start,
        "sort": "date",   # 최신순 정렬 (기본값 sim=정확도순 대신 date 사용)
    }

    # 3) 실제 GET 요청. timeout은 응답이 안 올 때 무한정 기다리지 않도록 하는 안전장치
    requester = session if session is not None else requests
    response = requester.get(NAVER_API_URL, headers=headers, params=params, timeout=10)
    response.raise_for_status()  # 200번대가 아니면(401, 429, 500 등) 여기서 에러를 던짐

    data = response.json()  # 응답 body(JSON 문자열)를 파이썬 딕셔너리로 변환

    results = []
    for item in data.get("items", []):
        results.append({
            "source": "네이버",
            "title": _strip_html_tags(item["title"]),
            "url": item["originallink"] or item["link"],
            "published_at": _parse_pub_date(item["pubDate"]),
            "category": None,   # 카테고리는 정제(normalizer) 단계에서 채움
            "body": None,       # 네이버는 본문을 안 줌
            "description": _strip_html_tags(item["description"]),
            "press": _extract_press(item["originallink"]),
        })
    return results


def _parse_pub_date(pub_date_str: str) -> str:
    """
    네이버 API가 주는 pubDate는 이런 형식이다:
        "Mon, 13 Jul 2026 09:00:00 +0900"
    (RFC 822 형식, 이메일/RSS에서 흔히 쓰는 날짜 표기법)

    이걸 우리 시스템 공통 스키마에서 쓰기로 한 ISO 8601 형식으로 바꾼다:
        "2026-07-13T09:00:00+09:00"
    """
    dt = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %z")
    return dt.isoformat()


def _extract_press(originallink: str) -> str:
    """
    네이버 API 응답에는 언론사명 필드가 따로 없다.
    originallink(원문 URL)의 도메인을 언론사 식별자로 대신 사용한다.

    예: "https://www.yna.co.kr/view/AKR2026..." -> "yna.co.kr"

    www. 제거는 .replace("www.", "") 대신 startswith 체크 후 슬라이싱을 쓴다
    - .replace()는 문자열 어디에 있든 매칭돼서, 서브도메인 이름 중간에 우연히 "www."가 들어간 경우(드물지만 가능) 의도치 않게 지워질 수 있다.
      startswith는 맨 앞에 있을 때만 제거하므로 더 안전.

    서브도메인 통합(예: biz.yna.co.kr / www.yna.co.kr을 같은 언론사로
    인식)은 하지 않는다 - 정확히 하려면 한국 도메인 특유의 복합
    접미사(.co.kr, .or.kr, .go.kr 등)를 다뤄야 해서 간단한 규칙으로는
    안 되고 tldextract 같은 라이브러리가 필요함. 실제로 문제(scorer.py의
    PRESS_DEDUP_CAP 정확도 왜곡)가 확인되면 그때 대응한다.
    """
    if not originallink:
        return ""
    domain = urlparse(originallink).netloc
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _strip_html_tags(text: str) -> str:
    """
    네이버 API 응답의 title/description에는 검색어 강조를 위해
    <b>태그</b>가 섞여서 온다. 이걸 제거하고 순수 텍스트만 남긴다.

    예: "고병원성 <b>조류독감</b> 국내 첫 발생" -> "고병원성 조류독감 국내 첫 발생"
    """
    # 1) <...> 형태의 태그를 전부 빈 문자열로 치환
    text = re.sub(r"<[^>]+>", "", text)
    # 2) &amp; , &quot; 같은 HTML 엔티티도 원래 문자로 복원 (& , " 등)
    text = html.unescape(text)
    return text


if __name__ == "__main__":
    # 터미널에서 python naver_collector.py 로 직접 실행했을 때만 동작.
    # 다른 파일에서 import naver_collector 로 불러쓸 땐 이 블록은 안 돌아감.
    results = collect()
    print(f"\n총 {len(results)}건 수집 완료")
    for r in results[:3]:
        print(r)