# 🤖 AI·IT 뉴스 큐레이션 시스템

네이버 뉴스와 GDELT를 매주 자동으로 수집·정제·요약해서 AI/IT 업계 주간 이슈를
`data/YYYY-WW/`에 아카이브하고 이메일로 발송하는 파이프라인입니다.

```
[1. 수집] → [2. 정규화+태깅+관련성필터+카테고리 재분류] → [2.1 이슈 그룹핑] →
[3. 스코어링(국내/해외 × 카테고리)] → [4. LLM 요약] → [5. 저장] → [6. 배포(이메일)]
```

---

## 1. 📡 수집 레이어

### 🇰🇷 네이버 (국내 전담) — `naver_collector.py`
- 네이버 뉴스 검색 API, `requests.Session()` 재사용
- 최근 7일(`DAYS_BACK=7`), 키워드당 최대 1000건(`MAX_START`)
- 공백 포함(여러 단어) 키워드는 네이버 API가 AND로 처리해 무관한 기사가 섞일 수 있어,
  제목+요약에 실제로 인접해서 등장하는지(`_phrase_present`) 재확인
- 페이지네이션 중 일부 실패해도 이미 모은 결과는 보존(부분 실패 안전)
- 기본(fallback) 키워드: 인공지능, AI, 머신러닝, 딥러닝, 챗GPT, 생성형 AI, 자율주행,
  로봇, 드론, 메타버스, 가상현실, 증강현실, 블록체인, NFT, 핀테크, 빅데이터, 클라우드,
  사이버보안, 양자컴퓨팅

### 🌍 GDELT (해외 전담) — `gdelt_collector.py`
- `gdeltdoc` 라이브러리로 영문 키워드 OR 검색, 최근 7일
- 기본(fallback) 키워드: artificial intelligence, nvidia, semiconductor, chatgpt, openai
- **적응형 배치 수집**: `BATCH_SIZE=5`로 묶어서 요청, 크라우딩(`CROWDING_SHARE_THRESHOLD=40%`)
  감지 시 해당 배치 키워드만 개별 재요청. 크라우딩 검사 자체는 배치가 상한
  (`MAX_RECORDS=250`)의 `CROWDING_CAP_TRIGGER_RATIO=90%` 이상 찼을 때만 실행
- 429 rate limit은 전역 쿨다운(60→120→240→480초) + 외부 재시도 라운드로 대응 —
  GDELT 자체의 구조적 제약(전 세계 트래픽 총량 기준으로 추정)이라 근본 해결은 안 되며,
  실행 시간이 몇 시간까지 늘어날 수 있음
- 짧은 영문 키워드는 GDELT가 "phrase too short"로 거부 — 같은 키워드에서 ValueError가
  누적 2회 발생하면 자동으로 스킵 목록에 등재, `state/gdelt_skip_keywords.json`에
  영속화·git commit
- `FALSE_POSITIVE_FILTERS`: 특정 키워드의 알려진 오탐 패턴을 제목 문자열 매칭으로 제외
- 응답의 `language` 필드로 언어 판별(국내/해외 재분류에 활용, 3번 섹션 참고)
- import 시점에 `requests` 기본 헤더(User-Agent)를 프로세스 전역으로 오버라이드

### 🔑 키워드 관리 — `keyword_source.py`
- 구글 시트를 "웹에 게시 → CSV" 링크로 공유, 실행마다 그 URL에서 읽어옴
  (`KEYWORD_SHEET_CSV_URL`, 같은 프로세스 안에서 ko/en 두 번 호출돼도 캐싱으로 실제
  요청은 1번만)
- 시트 형식: `keyword, lang(ko/en), active(TRUE/FALSE), note` — 행 삭제 대신 켜고 끄는
  방식으로 이력 보존
- 실제 문자 구성(한글 비율)으로 lang 오기재를 자동 보정하고, 중복 키워드는 제거
- 시트 URL 없음/읽기 실패/해당 lang 활성 키워드 없음 → 각 collector의 하드코딩
  fallback 키워드로 안전하게 대체(자동 동기화 아님 — 시트가 바뀌면 fallback도 같이
  갱신 필요)

> ✂️ **참고**: 업계 전문지 스크레이핑 소스(WATT)는 의도적으로 제외했습니다. 현재
> 수집 소스는 네이버·GDELT 두 곳입니다.

---

## 2. 🧹 정규화 레이어

- URL 기준 완전 동일 기사 제거(소스/키워드 무관하게 전역 dedup, `main.py normalize()`)

### 2.1 🧩 이슈 그룹핑 — `issue_grouper.py`
- **1차**: KR↔EN 키워드 사전 매칭(`ISSUE_SYNONYM_GROUPS`, 현재는 비워둠 — 국가가 다른
  동일 사안 기사가 잘못 묶이는 걸 막기 위함)
- **2차**: BGE-M3 임베딩 코사인 유사도. `THRESHOLD=0.7`, `BORDERLINE_MARGIN=0.06`
  (0.64~0.70이 애매 구간)
- **3차**: 애매 구간 쌍을 LLM에 물어 최종 확정. 판단 기준: 국가/지역이 다르면 별개,
  단신 기사와 여러 사안을 종합한 기사는 겹치는 부분이 있어도 별개, 애매하면 안 묶음이
  기본값
- 연쇄 병합 방지: 클리크(완전그래프) 검증이 안 되면 빠진 쌍만 LLM에 한 번 더 확인하는
  라운드를 거친 뒤에도 안 되면 개별 유지
- 임베딩 모델 로드 실패 시 1차 결과만으로 안전하게 fallback

### 2.2 🏷️ 키워드 태깅 — `keyword_tagger.py`
`CATEGORY_KEYWORDS` 기준으로 카테고리를 부여합니다(AI·IT 도메인으로 교체된 부분):

| 카테고리 | 국문 키워드 | 영문 키워드 |
|---|---|---|
| 인공지능 | 인공지능, AI, 머신러닝, 딥러닝, 챗GPT, 생성형 AI, 빅데이터 | Artificial intelligence |
| 기업 단위 | 엔비디아 | nvidia |
| 로봇공학 | 자율주행, 로봇, 드론, 메타버스 | — |
| 기타 IT 기술 | 가상현실, 증강현실, 블록체인, NFT, 클라우드, 사이버보안, 양자컴퓨팅 | — |

- 매칭 안 되면 "기타"
- 부분 문자열 중복 매칭(예: "corn"/"corn futures" 같은 포함 관계)은 집계 단계에서 하나로 셈
- WATT 등 소스 자체 카테고리가 있는 기사는 `site_category`로 보존(현재 소스 구성상
  네이버/GDELT만 있어 실질적으로는 항상 이 사전 기준으로 분류됨)

### 2.5 🔍 관련성 필터 — `relevance_filter.py` (LLM 기반)
- 키워드 매칭만으론 못 거르는 오매칭(동음이의어, 기관명 일부로만 등장, 각주성 언급 등)을
  LLM이 판단해 제외
- 배치 크기 20, id 기반 부분 복구 — **애매하면 "통과"가 기본값**(이슈그룹핑 3차와 반대 방향)
- 시스템/사용자 프롬프트는 영어(요약 생성 프롬프트만 예외, 결과물이 한국어여야 해서 한국어 유지)

### 2.6 🔄 카테고리 재분류 — `relevance_filter.recategorize_uncategorized()` (LLM 기반)
- 사전 매칭(`keyword_tagger`)엔 안 걸려 `category="기타"`로 남았지만 관련성 필터는
  통과한 기사만 추려서, 정의된 카테고리 중 가장 맞는 것을 LLM에 다시 물음
- 목록에 없는 답/실패 시 안전하게 "기타" 유지

---

## 3. 🏆 스코어링 — `scorer.py` + `main.py`의 `score()`

- **국내/해외 축**: 네이버=국내, GDELT=해외가 기본. 단, GDELT 기사가 `language` 필드
  (또는 제목 한글 비율)로 한국어 판정되면 국내로 재분류
- **국내-해외 교차 매칭(🔗)**: 같은 이슈가 국내/해외 양쪽에 걸쳐 있으면 각 축 대표 기사에
  반대 축 대표 제목을 `cross_axis_partner`로 부착 — 콘솔/summary.md/이메일에 "🔗 반대
  축에서도 다뤄짐"으로 표시
- **카테고리 축**: 국내/해외 축과 독립적으로 유지. 그룹 내 다수결로 대표 카테고리 결정,
  "기타"는 제외, 이슈 없는 카테고리는 결과에서 생략. `CATEGORY_TOP_N=1`(환경변수로 조정 가능)
- 스코어링 방식: 언급수 × 최신가중치. `RECENCY_WEIGHT_TABLE`(2일 이내 1.0, 5일 이내 0.7,
  7일 이내 0.4, 8일 이상 `RECENCY_WEIGHT_DEFAULT=0.1`), 언론사당 그룹 내 카운트 상한
  `PRESS_DEDUP_CAP=3`
- `TOP_N=5`(국내/해외 각 축), `CATEGORY_TOP_N=1` — 둘 다 환경변수로 조정 가능

### 📊 카테고리 전체 집계 — `category_aggregator.py`
- 이슈 그룹핑(동일 사건만 묶는 좁은 정의)이 놓치는 공백을 메우는 거친 보조 지표.
  단순 건수 집계(recency_weight 미적용), 랭킹용 아님
- **지난주 대비 증감**: 지난주 `scored.json`의 `category_distribution`과 비교. 지난주
  데이터 없음(첫 실행 등)이면 안전하게 생략

---

## 4. ✍️ LLM 요약 — `llm_summarizer.py`

- (A) 자체 요약, (A-1) 단독 기사(그룹 크기 1)는 재료가 얇으면 요약 생략하고 원문 제목만 노출
- 프로바이더: Anthropic(`claude-haiku-4-5-20251001`) 기본, `LLM_PROVIDER=openrouter`로
  전환 시 OpenRouter로 대체
- OpenRouter 경로는 `OPENROUTER_MODEL`(1순위) → `OPENROUTER_MODEL_2`(2순위, 선택) →
  `OPENROUTER_MODEL_3`(3순위, 선택) → `openrouter/free`(최종 안전망) 순으로 시도
  (`issue_grouper.py`/`relevance_filter.py`/`llm_summarizer.py` 세 곳 동일 구조 공유)
- API 호출 실패 시 요약 없이 원문 제목 + 링크만 노출(파이프라인은 죽지 않음)
- 카테고리별 Top N도 같은 방식으로 요약(국내/해외 각각 세션 하나로 묶어 처리)
- 네이버/GDELT는 본문을 못 주기 때문에 재료 부족으로 요약이 생략되는 경우가 많아,
  Top N으로 추려진 기사에 한해 `trafilatura`로 본문 추가 수집을 시도(실패해도 기존
  fallback으로 안전하게 처리)

### ⚠️ 프로바이더 관련 알려진 이슈
OpenRouter 무료 티어는 분당 20건 + (크레딧 구매 이력 없으면) 하루 50건으로 제한되는데,
관련성 필터+카테고리 재분류+3차 그룹핑+요약을 합치면 한 번 실행에 수십 건의 LLM 호출이
발생해 크레딧 없는 계정은 실행 1회로 하루 상한을 넘길 수 있습니다. `ANTHROPIC_API_KEY`가
설정되어 있으면 기본 경로(Anthropic)를 쓰므로 이 문제와 무관합니다.

---

## 5. 💾 저장 레이어 — `storage.py`

- `data/YYYY-WW/`(ISO 주차)에 세 파일 저장:
  - `raw.json`: 관련성 필터까지 통과한 최종 기사 전체
  - `scored.json`: 국내/해외 Top N + 카테고리별 Top N + 카테고리 집계 + 실패 소스 +
    GDELT 참고 지표
  - `summary.md`: 사람이 바로 읽는 배포용 요약(카테고리별 지난주 대비 증감 포함 가능)
- 파일별로 쓰기 실패를 개별 흡수 — 하나 실패해도 나머지는 계속 저장 시도, 부분 성공 지원
- **저장 방식은 repo 커밋** (GitHub Actions 아티팩트 아님) — "지난주 대비 증감" 비교가
  다음 실행 시점에도 checkout된 리포에서 지난주 데이터를 읽어야 하기 때문. git
  commit/push는 워크플로가 책임짐(이 모듈은 파일 생성까지만)
- `state/gdelt_skip_keywords.json`(GDELT 학습형 스킵 목록)도 같은 방식으로 git commit —
  주차별 아카이브인 `data/`와 성격이 달라 별도 디렉토리

---

## 6. 📬 배포 레이어 — `deploy.py`

- Gmail SMTP(587, STARTTLS)로 국내/해외 Top N + 카테고리별 Top N을 HTML 이메일로 발송
- 인증정보(`SMTP_USER`/`SMTP_APP_PASSWORD`/`EMAIL_RECIPIENTS`)는 GitHub Secrets에서 읽음
- 인증정보 미설정 시 발송만 안전하게 생략(파이프라인은 안 죽음)
- 이슈별 🔗 교차 매칭 표시도 이메일 본문에 포함
- 이메일 클라이언트(특히 아웃룩)의 렌더링 제약을 고려해 인라인 스타일 + `<table>` 위주로 구성

---

## 🎛️ 오케스트레이션 — `main.py`

`run()`이 아래 순서로 각 단계를 개별 try/except로 감싸 실행합니다(한 단계가 예외로
죽어도 이미 모은 데이터는 살려서 다음 단계로 진행):

1. 수집(`run_collectors`) → 2. 정규화+태깅 → 2.5 관련성 필터 → 2.6 카테고리 재분류 →
2.1 임베딩 모델 로드 → 3. 스코어링 → 3-보조 카테고리 집계 → 4. LLM 요약 →
4-보조 카테고리별 LLM 요약 → 5. 저장 → 6. 배포

- 수동 실행(`workflow_dispatch`)은 저장을 건너뜁니다 — "지난주 대비 증감" 비교 기준이
  테스트 데이터로 오염되는 것을 방지하기 위함(콘솔 출력은 정상적으로 확인 가능)
- `TOP_N`, `CATEGORY_TOP_N` 환경변수로 조정 가능(기본 5 / 1)

---

## 🔐 GitHub Secrets / Variables 전체 목록

리포 Settings → Secrets and variables → Actions에 등록해야 하는 값들.
**Secrets**(암호화, 로그 자동 마스킹)와 **Variables**(평문)를 구분해서 등록해야 하며,
반대로 등록하면 워크플로는 돌아가지만 값이 빈 문자열로 처리됩니다.

| 이름 | 종류 | 용도 | 비고 |
|---|---|---|---|
| `NAVER_CLIENT_ID` | Secret | 네이버 뉴스 검색 API 인증 | `naver_collector.py` |
| `NAVER_CLIENT_SECRET` | Secret | 위 ID와 짝을 이루는 비밀키 | `naver_collector.py` |
| `KEYWORD_SHEET_CSV_URL` | Variable | 구글 시트(공개 게시 CSV)에서 검색 키워드를 읽어오는 URL | 없으면 각 collector의 하드코딩 fallback 키워드 사용 |
| `ANTHROPIC_API_KEY` | Secret | Anthropic API 키(`LLM_PROVIDER=anthropic`, 기본 경로) | 없으면 LLM 호출이 안전하게 생략됨 |
| `LLM_PROVIDER` | Variable | `anthropic`(기본값) 또는 `openrouter` | 미등록 시 기본값 `anthropic` |
| `OPENROUTER_API_KEY` | Secret | OpenRouter API 키(`LLM_PROVIDER=openrouter`) | |
| `OPENROUTER_MODEL` | Variable | OpenRouter 1순위 모델명 | 비우면 자동 라우터(`openrouter/free`) |
| `OPENROUTER_MODEL_2` | Variable | 1순위 실패 시 2순위 모델 | 선택 |
| `OPENROUTER_MODEL_3` | Variable | 2순위도 실패 시 3순위 모델 | 선택, 이후 항상 `openrouter/free`가 최종 안전망 |
| `SIMILARITY_DEBUG_CSV` | Variable | 이슈 그룹핑 2차 임계값 튜닝용 디버그 CSV 저장 스위치 | `1`/`true`/`yes`/`on`일 때만 켜짐, 기본 꺼짐 |
| `SMTP_USER` | Secret | 이메일 발송용 Gmail 계정 | `deploy.py`, Gmail SMTP(587, STARTTLS) |
| `SMTP_APP_PASSWORD` | Secret | Gmail 앱 비밀번호 | 2단계 인증 켠 뒤 발급 |
| `EMAIL_RECIPIENTS` | Secret | 수신자 이메일 주소, 콤마 구분 | 개인정보로 취급해 Variable 대신 Secret |

---

## 🚀 설치 및 실행

```bash
pip install -r requirements.txt
python main.py
```

로컬 실행 시 `.env` 파일에 `NAVER_CLIENT_ID`/`NAVER_CLIENT_SECRET` 등을 넣어두면
`naver_collector.py`가 자동으로 읽습니다(`python-dotenv`). GitHub Actions에서는 Secrets가
이미 주입되어 있어 `.env` 없이도 동작합니다.

---

## 🚧 알려진 미해결 사항

- GDELT 429로 인한 긴 실행 시간 — 구조적 한계, 근본 해결책 없음
- `THRESHOLD`/`BORDERLINE_MARGIN`/`PRESS_DEDUP_CAP`/`CATEGORY_TOP_N` 등 여러 잠정값은
  실측 기반 조정이 계속 필요
- 이메일 수신자 확대 여부는 추후 결정