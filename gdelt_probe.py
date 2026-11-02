"""
gdelt_probe.py - GDELT 수집 진단용 테스트 스크립트(파이프라인과 무관, 데이터 저장 안 함).

1단계: GDELT DOC API를 직접 호출해서 응답 원본(상태코드/Content-Type/본문 길이/앞부분)을 그대로 출력
       -> 429인지, 200 + `{}`(조절)인지, 200 + `{"articles": [...]}`(정상)인지 판별
2단계: 실제 수집 코드(gdelt_collector._collect_articles_for_keyword)로 키워드 1개 수집

사용: python -u gdelt_probe.py [키워드 ...]   (기본: Artificial intelligence, nvidia, claude)
"""

import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import gdelt_collector  # import 시 User-Agent 패치가 적용됨(실제 수집과 같은 조건)
from gdeltdoc import GdeltDoc

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
INTERVAL = 10  # 요청 간 대기(초) - GDELT 권장 5초보다 넉넉하게
DEFAULT_KEYWORDS = ["Artificial intelligence", "nvidia", "claude"]


def classify(resp: requests.Response) -> str:
    if resp.status_code == 429:
        return "429 레이트리밋"
    if resp.status_code != 200:
        return f"HTTP {resp.status_code} 오류"
    if "text/html" in resp.headers.get("content-type", ""):
        return "200 + HTML(쿼리 거부/오류 메시지)"
    body = resp.text.strip()
    if body in ("", "{}"):
        return "200 + 빈 응답({}) -> 조절(throttle) 의심"
    try:
        data = resp.json()
    except ValueError:
        return "200 + JSON 파싱 실패"
    if "articles" not in data:
        return f"200 + articles 키 없음 (키: {list(data)[:5]})"
    return f"정상 - 기사 {len(data['articles'])}건"


def raw_probe(keyword: str, start: datetime, end: datetime, use_window: bool) -> None:
    query = f'"{keyword}"' if " " in keyword else keyword
    params = {"query": query, "mode": "artlist", "format": "json", "maxrecords": "250"}
    if use_window:
        params["startdatetime"] = start.strftime("%Y%m%d%H%M%S")
        params["enddatetime"] = end.strftime("%Y%m%d%H%M%S")
        tag = "구간 지정"
    else:
        params["timespan"] = "1d"
        tag = "timespan=1d"

    t0 = time.monotonic()
    try:
        resp = requests.get(API_URL, params=params, timeout=60)
    except requests.RequestException as e:
        print(f"  [{tag}] 요청 실패: {type(e).__name__}: {e}")
        return
    elapsed = time.monotonic() - t0
    print(f"  [{tag}] {classify(resp)}")
    print(f"      status={resp.status_code} type={resp.headers.get('content-type')} "
          f"len={len(resp.content)}B {elapsed:.1f}s retry-after={resp.headers.get('Retry-After')}")
    print(f"      body: {resp.text.strip()[:200]!r}")


def main() -> None:
    keywords = sys.argv[1:] or DEFAULT_KEYWORDS
    now = datetime.now(timezone.utc)
    # 실제 파이프라인과 같은 구간: 어제 15:00 UTC ~ 오늘 15:00 UTC(KST 자정 기준) - 아직 안 끝났으면 하루 앞당김
    end = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if end > now:
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    print(f"[probe] 실행 시각 {now.isoformat(timespec='seconds')} / 구간 {start.isoformat()} ~ {end.isoformat()}")
    try:
        print(f"[probe] 러너 공인 IP: {requests.get('https://api.ipify.org', timeout=10).text}")
    except requests.RequestException:
        pass

    print("\n=== 1단계: API 원본 응답 ===")
    for kw in keywords:
        print(f"\n- '{kw}'")
        for use_window in (True, False):
            raw_probe(kw, start, end, use_window)
            time.sleep(INTERVAL)

    print("\n=== 2단계: 실제 수집 코드로 1개 키워드 ===")
    kw = keywords[0]
    gdelt_collector.MAX_RETRIES = 2  # 테스트라 대기를 짧게(60->120초)
    ok, articles, reason = gdelt_collector._collect_articles_for_keyword(GdeltDoc(), kw, start, end)
    print(f"[probe] 결과: 성공={ok} 기사={len(articles)}건 사유={reason}")
    for a in articles[:3]:
        print(f"  - {a['published_at']} | {a['press']} | {a['title'][:80]}")


if __name__ == "__main__":
    main()
