"""
gkg_probe.py - GDELT GKG 원본 파일(data.gdeltproject.org) 접근/파싱 가능 여부 진단(데이터 저장 안 함).

DOC API가 429/{}로 막혀 있을 때 대체 경로로 쓸 수 있는지 확인:
1) lastupdate.txt / lastupdate-translation.txt 접근
2) 최신 GKG zip 다운로드 -> 행 수, 제목(<PAGE_TITLE>) 보유 비율
3) 어제 구간 중 몇 개 슬롯을 받아 크기/시간 측정 -> 하루치(96슬롯) 추정
4) 키워드 제목 매칭 샘플

사용: python -u gkg_probe.py [키워드 ...]
"""

import io
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone

import requests

BASE = "http://data.gdeltproject.org/gdeltv2"
DEFAULT_KEYWORDS = ["artificial intelligence", "nvidia", "openai", "chatgpt", "semiconductor", "claude"]
SAMPLE_SLOTS = 4  # 어제 구간에서 샘플로 받을 15분 슬롯 수

TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
# GKG 2.1 컬럼: 0 레코드ID, 1 DATE, 3 SourceCommonName, 4 DocumentIdentifier(URL), 26 Extras(<PAGE_TITLE>)
COL_DATE, COL_SOURCE, COL_URL, COL_EXTRAS = 1, 3, 4, 26


def fetch(url: str) -> tuple[requests.Response | None, float]:
    t0 = time.monotonic()
    try:
        resp = requests.get(url, timeout=120)
    except requests.RequestException as e:
        print(f"  요청 실패 {url}: {type(e).__name__}: {e}")
        return None, time.monotonic() - t0
    return resp, time.monotonic() - t0


def parse_gkg(content: bytes) -> list[dict]:
    rows = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        name = zf.namelist()[0]
        text = zf.read(name).decode("utf-8", errors="replace")
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) <= COL_EXTRAS:
            continue
        m = TITLE_RE.search(cols[COL_EXTRAS])
        rows.append({
            "date": cols[COL_DATE],
            "source": cols[COL_SOURCE],
            "url": cols[COL_URL],
            "title": m.group(1).strip() if m else "",
        })
    return rows


def probe_slot(url: str) -> tuple[list[dict], int, float] | None:
    resp, elapsed = fetch(url)
    if resp is None:
        return None
    print(f"  {url.rsplit('/', 1)[-1]}: HTTP {resp.status_code} {len(resp.content) / 1e6:.1f}MB {elapsed:.1f}s")
    if resp.status_code != 200:
        print(f"    body: {resp.text[:200]!r}")
        return None
    rows = parse_gkg(resp.content)
    return rows, len(resp.content), elapsed


def main() -> None:
    keywords = [k.lower() for k in (sys.argv[1:] or DEFAULT_KEYWORDS)]
    now = datetime.now(timezone.utc)
    print(f"[gkg] 실행 시각 {now.isoformat(timespec='seconds')}")

    print("\n=== 1) 매니페스트 ===")
    latest = {}
    for name in ("lastupdate.txt", "lastupdate-translation.txt"):
        resp, elapsed = fetch(f"{BASE}/{name}")
        if resp is None:
            continue
        print(f"  {name}: HTTP {resp.status_code} {elapsed:.1f}s")
        print("    " + resp.text.strip().replace("\n", "\n    "))
        for line in resp.text.split():
            if line.endswith(".gkg.csv.zip"):
                latest[name] = line

    print("\n=== 2) 최신 GKG 파일 ===")
    all_rows: list[dict] = []
    total_bytes, total_time, slots = 0, 0.0, 0
    for name, url in latest.items():
        result = probe_slot(url)
        if result:
            rows, size, elapsed = result
            titled = sum(1 for r in rows if r["title"])
            print(f"    행 {len(rows)}개, 제목 있는 행 {titled}개 ({titled / max(len(rows), 1):.0%})")
            all_rows.extend(rows)
            if name == "lastupdate.txt":
                total_bytes += size; total_time += elapsed; slots += 1

    print(f"\n=== 3) 어제 구간 샘플 {SAMPLE_SLOTS}개 슬롯(영문 GKG) ===")
    day_end = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if day_end > now:
        day_end -= timedelta(days=1)
    day_start = day_end - timedelta(days=1)
    step = timedelta(days=1) / SAMPLE_SLOTS
    for i in range(SAMPLE_SLOTS):
        ts = (day_start + step * i).strftime("%Y%m%d%H%M%S")
        result = probe_slot(f"{BASE}/{ts}.gkg.csv.zip")
        if result:
            rows, size, elapsed = result
            all_rows.extend(rows)
            total_bytes += size; total_time += elapsed; slots += 1
        time.sleep(1)

    if slots:
        print(f"\n  슬롯 평균 {total_bytes / slots / 1e6:.1f}MB, {total_time / slots:.1f}s "
              f"-> 하루(96슬롯) 추정 {total_bytes / slots * 96 / 1e6:.0f}MB, {total_time / slots * 96 / 60:.1f}분(순차)")

    print(f"\n=== 4) 제목 키워드 매칭 (샘플 {len(all_rows)}행) ===")
    for kw in keywords:
        hits = [r for r in all_rows if kw in r["title"].lower()]
        print(f"  '{kw}': {len(hits)}건")
        for r in hits[:2]:
            print(f"    - {r['date']} | {r['source']} | {r['title'][:90]}")


if __name__ == "__main__":
    main()
