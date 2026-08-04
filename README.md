# 🤖 AI·IT 뉴스 큐레이션 시스템

네이버 뉴스와 GDELT를 매일 새벽 자동으로 수집·정제·요약해서 AI/IT 업계 일간 이슈를
`data/YYYY-MM-DD/`에 아카이브하고 전날 기사를 이메일로 발송하는 파이프라인입니다.

```
[1. 수집] → [2. 정규화+태깅+관련성필터+카테고리 재분류] → [2.1 이슈 그룹핑] →
[3. 스코어링(국내/해외 × 카테고리)] → [4. LLM 요약] → [5. 저장] → [6. 배포(이메일)]
```

---

## 1. 📡 수집 레이어

두 collector 모두 `main.py`가 파이프라인 시작 시점에 계산하는 고정 절대 구간
([window_start, window_end), "어제 00:00 KST ~ 오늘 00:00 KST")을 그대로 받아서 수집한다
(`main.py`의 `_compute_collection_window()` 참고). 예전엔 각자 "지금부터 24시간 전까지"를
호출 시점마다 따로 계산했는데, GDELT 수집이 최대 220분에 걸쳐 배치 단위로 순차 요청되다
보니 같은 실행 안에서도 먼저 처리된 키워드와 나중에 처리된 키워드가 서로 다른 절대 구간을
보게 되는 드리프트(최대 3~4시간)가 있었다 - 고정 구간을 한 번만 계산해서 전부에 넘기면
몇 시에 처리되든 정확히 같은 구간을 보게 되어 이 드리프트가 사라진다.

### 🇰🇷 네이버 (국내 전담) — `naver_collector.py`
- 네이버 뉴스 검색 API, `requests.Session()` 재사용
- `window_start`/`window_end` 절대 구간 안의 기사만 남김(`_is_in_window`), 키워드당 최대
  1000건(`MAX_START`). window 인자를 안 받으면(단독 테스트 등) `_default_window()`로
  "지금부터 24시간 전까지" 롤링 윈도우 사용
- 공백 포함(여러 단어) 키워드는 네이버 API가 AND로 처리해 무관한 기사가 섞일 수 있어,
  제목+요약에 실제로 인접해서 등장하는지(`_phrase_present`) 재확인
- 페이지네이션 중 일부 실패해도 이미 모은 결과는 보존(부분 실패 안전)
- 기본(fallback) 키워드: 인공지능, AI, 머신러닝, 딥러닝, 챗GPT, 생성형 AI, 자율주행,
  로봇, 드론, 메타버스, 가상현실, 증강현실, 블록체인, NFT, 핀테크, 빅데이터, 클라우드,
  사이버보안, 양자컴퓨팅

### 🌍 GDELT (해외 전담) — `gdelt_collector.py`
- `gdeltdoc` 라이브러리로 영문 키워드 OR 검색. `Filters`에 상대값 `timespan` 대신
  `start_date`/`end_date`(절대 날짜, 초 단위 정밀도)로 `window_start`/`window_end`를 직접
  넘김 - 일간 전환 부수효과로 구간이 좁아져 크라우딩도 예전 7일 창보다 완화됨
- 기본(fallback) 키워드: artificial intelligence, nvidia, semiconductor, chatgpt, openai
- **적응형 배치 수집**: `BATCH_SIZE=5`로 묶어서 요청, 배치 결과가 정확히 상한
  (`MAX_RECORDS=250`)에 도달했을 때만 크라우딩으로 보고 그 배치 **전체**(원인 키워드 포함)를
  개별 재요청 — "근처(예: 90%)"나 "특정 키워드가 몇 % 차지" 같은 근사 기준은 안 씀. 상한
  미만이면 애초에 밀려난 게 없다고 보고, 상한에 걸렸으면 배치 안 어떤 키워드든 자기 몫이
  잘렸을 수 있다고 봐서 전부 다시 확인함
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

---

## 2. 🧹 정규화 레이어

- URL 기준 완전 동일 기사 제거(소스/키워드 무관하게 전역 dedup, `main.py normalize()`)
- 수집 구간(`window_start`~`window_end`) 밖 기사 필터(`scorer.is_in_window()`) — 상세는
  3번 섹션 "수집 구간 필터" 참고. collector가 이미 이 구간으로 정확히 걸러서 수집하므로
  정상 흐름에선 걸러질 게 없어야 정상 - 방어선(defense in depth) 역할

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
`CATEGORY_KEYWORDS` 기준으로 카테고리를 부여합니다:

| 카테고리 | 국문 키워드 | 영문 키워드 |
|---|---|---|
| 인공지능 | 인공지능, AI, 머신러닝, 딥러닝, 챗GPT, 생성형 AI, 빅데이터 | Artificial intelligence |
| 기업 단위 | 엔비디아 | nvidia |
| 로봇공학 | 자율주행, 로봇, 드론, 메타버스 | — |
| 기타 IT 기술 | 가상현실, 증강현실, 블록체인, NFT, 클라우드, 사이버보안, 양자컴퓨팅 | — |

- 매칭 안 되면 "기타"
- 부분 문자열 중복 매칭(예: "corn"/"corn futures" 같은 포함 관계)은 집계 단계에서 하나로 셈
- 소스 자체 카테고리가 있는 기사는 `site_category`로 별도 보존하고, `category`는 항상
  이 사전 기준으로 덮어씀

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
  축에서도 다뤄짐"으로 표시. 이 부착은 그룹 형성 시점(Top N 선정 *이전*)에 이뤄지므로,
  국내/해외가 완전히 독립적으로 순위를 매기는 특성상 한쪽만 Top N에 들고 반대쪽은
  못 드는 경우가 흔함 — `score()` 끝에서 양쪽 축의 최종 노출 목록(메인+카테고리별
  Top N)을 다시 확인해서, 반대 축에 실제로 안 보이는 상대는 `cross_axis_partner`를
  지운다(`_scrub_unshown_cross_axis_partner`) - 안 그러면 이메일에 없는 기사 제목이
  걸려있는 상태가 됨
- **카테고리 축**: 국내/해외 축과 독립적으로 유지. 그룹 내 다수결로 대표 카테고리 결정,
  "기타"는 제외, 이슈 없는 카테고리는 결과에서 생략. `CATEGORY_TOP_N=1`(환경변수로 조정 가능)
- 스코어링 방식: 언급수(`mention_count`, 언론사당 그룹 내 카운트 상한 `PRESS_DEDUP_CAP=3`
  적용) 그대로가 `issue_score` — 예전엔 여기에 `RECENCY_WEIGHT_TABLE`(시간대별 계단형
  가중치)을 곱했었는데, 일간 배치 구조에서 되짚어보니 가중치가 오히려 왜곡을 만들었음:
  실제 스코어링은 수집보다 한참 뒤(GDELT만 최대 220분)에 일어나서, "얼마나 오래된
  뉴스냐"가 아니라 "하루 중 몇 시에 터졌냐"에 더 가깝게 작동했음 - 늦게 터진 속보는
  아직 언론사들이 못 받아써서 `mention_count`가 원래 낮은데 가중치까지 높게 받고,
  일찍 터져서 하루 종일 보도된(그래서 `mention_count`가 높은) 이슈는 가중치가 깎이는
  이중 왜곡. 그래서 가중치는 없애고 `mention_count`를 그대로 씀 — 신선도는 아래
  "수집 구간 필터"로 스코어링 이전에 걸러내는 쪽으로 분리
- **수집 구간 필터** — `main.py normalize()`가 `scorer.is_in_window()`로 걸러냄. naver/gdelt가
  이미 정확히 이 구간(`window_start`~`window_end`, "어제 00:00 KST ~ 오늘 00:00 KST")으로
  걸러서 수집하므로 정상 흐름에선 여기서 걸러질 게 없어야 정상 - collector 로직 결함이나
  향후 이 구간 필터를 안 지키는 새 소스가 추가되는 경우에 대비한 방어선(defense in depth).
  (예전엔 "24시간 경과 여부"를 매번 계산하는 방식(`is_stale`)이었는데, naver/gdelt 각
  collector가 호출 시점마다 따로 "지금부터 24시간 전"을 계산하다 보니 같은 실행 안에서도
  키워드마다 절대 구간이 최대 몇 시간씩 밀리는 드리프트가 있었음 - 지금은 `main.py`가
  파이프라인 시작 시점에 계산한 고정 구간을 collector/필터 양쪽에 동일하게 넘겨서 이
  드리프트 자체가 사라짐. 1번 섹션 "수집 레이어" 도입부 참고)
- `TOP_N=5`(국내/해외 각 축), `CATEGORY_TOP_N=1` — 둘 다 환경변수로 조정 가능
- **4차 Top N 사후 재검토** — `issue_grouper.stage4_dedupe_and_promote()`. 최종 순위권
  후보끼리만(top_n개, 국내/해외 + 카테고리별 최대 18개 리스트 전부) 같은 사건인지 LLM으로
  한 번 더 확인해 병합하고, 빈 자리는 다음 순위 후보로 채움. 회차당 병합 1건, 최대 3회

### 📊 카테고리 전체 집계 — `category_aggregator.py`
- 이슈 그룹핑(동일 사건만 묶는 좁은 정의)이 놓치는 공백을 메우는 거친 보조 지표.
  단순 건수 집계, 랭킹용 아님
- **전일 대비 + 7일 평균 대비 증감**: 전일 `scored.json`의 `category_distribution`과 비교하고,
  같이 최근 7일(오늘 제외) 평균과도 비교. 요일별 뉴스량 편차 때문에 전일 대비 하나만 보면
  노이즈가 커질 수 있어 두 지표를 함께 보여줌. 과거 데이터 없음(일간 전환 직후 등)이면
  안전하게 생략

---

## 4. ✍️ LLM 요약 — `llm_summarizer.py`

- (A) 자체 요약, (A-1) 단독 기사(그룹 크기 1)는 재료가 얇으면 요약 생략하고 원문 제목만 노출
- **제목 한국어 번역**: 요약과 같은 LLM 호출 안에서 `{"title": "...", "summary": "..."}`
  JSON으로 같이 받아옴 - 원문이 영어 등 외국어면 한국어로 번역, 이미 한국어면 다듬어서
  그대로. summary.md/이메일 모두 이 번역 제목을 헤딩으로 쓰고, 번역과 원문이 다르면 원문도
  작게 병기(검색/원문 대조용). 별도 API 호출을 안 늘리려고 요약 프롬프트에 얹은 것 - 응답이
  JSON 형식을 안 지키면(무료 모델에서 가끔 발생) 제목 번역만 포기하고 요약은 그대로 살림.
  단독 기사가 재료 부족으로 요약 자체가 생략되는 경로에서는 제목 번역도 같이 생략됨(원문
  제목 그대로 노출)
- 프로바이더: 코드 기본값은 Anthropic(`claude-haiku-4-5-20251001`)이지만, 현재 운영은
  `LLM_PROVIDER=openrouter`로 전환해서 사용 중 — OpenRouter 계정에 $10 크레딧을 선결제해
  무료 모델 하루 요청 한도를 1000건까지 올려둔 상태(아래 참고)
- OpenRouter 경로는 `OPENROUTER_MODEL`(1순위) → `OPENROUTER_MODEL_2`(2순위, 선택) →
  `OPENROUTER_MODEL_3`(3순위, 선택) → `openrouter/free`(최종 안전망) 순으로 시도
  (`issue_grouper.py`/`relevance_filter.py`/`llm_summarizer.py` 세 곳 동일 구조 공유)
- API 호출 실패 시 요약 없이 원문 제목 + 링크만 노출(파이프라인은 죽지 않음)
- 카테고리별 Top N도 같은 방식으로 요약(국내/해외 각각 세션 하나로 묶어 처리)
- 네이버/GDELT는 본문을 못 주기 때문에 재료 부족으로 요약이 생략되는 경우가 많아,
  Top N으로 추려진 기사에 한해 `trafilatura`로 본문 추가 수집을 시도(실패해도 기존
  fallback으로 안전하게 처리)

### 💳 프로바이더 결정 — OpenRouter $10 선결제
OpenRouter 무료 티어는 기본적으로 분당 20건 + 하루 50건으로 제한되는데, 관련성 필터+
카테고리 재분류+3차 그룹핑+요약을 합치면 한 번 실행에 수십 건의 LLM 호출이 발생해
크레딧 없는 계정은 실행 1회로 하루 상한을 넘길 수 있었습니다. 이를 해결하기 위해
OpenRouter 계정에 $10 크레딧을 1회 선결제해서 무료 모델 하루 요청 한도를 1000건으로
올려두는 것으로 확정했습니다(`OPENROUTER_API_KEY` 사용, `ANTHROPIC_API_KEY`는 미사용).

---

## 5. 💾 저장 레이어 — `storage.py`

- `data/YYYY-MM-DD/`(날짜)에 세 파일 저장:
  - `raw.json`: 관련성 필터까지 통과한 최종 기사 전체
  - `scored.json`: 국내/해외 Top N + 카테고리별 Top N + 카테고리 집계 + 실패 소스 +
    GDELT 참고 지표
  - `summary.md`: 사람이 바로 읽는 배포용 요약(카테고리별 전일/7일 평균 대비 증감 포함 가능)
- 파일별로 쓰기 실패를 개별 흡수 — 하나 실패해도 나머지는 계속 저장 시도, 부분 성공 지원
- **저장 방식은 repo 커밋** (GitHub Actions 아티팩트 아님) — "전일/7일 평균 대비 증감" 비교가
  다음 실행 시점에도 checkout된 리포에서 과거 데이터를 읽어야 하기 때문. git
  commit/push는 워크플로가 책임짐(이 모듈은 파일 생성까지만)
- `state/gdelt_skip_keywords.json`(GDELT 학습형 스킵 목록)도 같은 방식으로 git commit —
  날짜별 아카이브인 `data/`와 성격이 달라 별도 디렉토리

---

## 6. 📬 배포 레이어 — `deploy.py`

- Gmail SMTP(587, STARTTLS)로 국내/해외 Top N + 카테고리별 Top N을 HTML 이메일로 발송
- 인증정보(`SMTP_USER`/`SMTP_APP_PASSWORD`/`EMAIL_RECIPIENTS`)는 GitHub Secrets에서 읽음
- 인증정보 미설정 시 발송만 안전하게 생략(파이프라인은 안 죽음)
- 이슈별 🔗 교차 매칭 표시도 이메일 본문에 포함
- 이메일 클라이언트(특히 아웃룩)의 렌더링 제약을 고려해 인라인 스타일 + `<table>` 위주로 구성

---

## 🎛️ 오케스트레이션 — Job 체이닝(2-Job 구조)

GitHub Actions Job 하나(최대 6시간, GitHub 인프라 자체의 캡)에 수집~배포를 전부 우겨넣던
구조에서, **Job을 둘로 쪼개 각자 자기 몫의 6시간을 따로 쓰는 구조**로 전환했다
(`run-pipline.yml` 참고):

- **collect Job** (`collect_stage.py`): `main.py`의 `_compute_collection_window()` +
  `run_collectors()`를 그대로 재사용해 네이버+GDELT만 수집. 결과를 `collect_output/collected.json`
  으로 저장하고 `actions/upload-artifact`로 올린 뒤 종료. `state/*.json`(GDELT 학습 상태)도
  이 Job이 직접 커밋
- **process Job** (`process_stage.py`): `needs: collect` + `if: always()`로 collect Job이
  실패해도 항상 시도한다. `actions/download-artifact`로 결과를 이어받아 `main.py`의
  `normalize()`/`score()`/`step4_llm_summary()` 등을 재사용해 정규화~배포를 실행하고
  `data/`를 커밋

`main.py`의 `run()` 자체는 그대로 남아있다 — 로컬에서 전체 파이프라인을 한 번에 테스트할
땐 여전히 `python main.py`로 단일 프로세스 실행 가능(이 경우 아래 "단일 실행 시간예산" 표
그대로 적용).

- 수동 실행(`workflow_dispatch`)은 저장을 건너뜁니다 — "전일/7일 평균 대비 증감" 비교 기준이
  테스트 데이터로 오염되는 것을 방지하기 위함(콘솔 출력은 정상적으로 확인 가능)
- `TOP_N`, `CATEGORY_TOP_N` 환경변수로 조정 가능(기본 5 / 1)

### ⏱️ Job 체이닝 시간예산

| Job | 대상 | 예산 |
|---|---|---|
| collect | GDELT 수집(네이버 포함) | 이 Job 전체(360분, `COLLECT_TIME_BUDGET_SECONDS`) |
| process | 관련성 필터 + 카테고리 재분류 | 이 Job 시작 후 240분(4시간, `RELEVANCE_TIME_BUDGET_SECONDS`) |
| process | 이슈 그룹핑 1~3차 / 4차 재검토 / LLM 요약 | 무제한(`deadline=float("inf")`) |

그룹핑~요약을 무제한으로 둔 건 `TOP_N`이 고정값이라 수집량과 무관하게 규모가 안 커져서
상대적으로 안전하다고 판단했기 때문 - 관련성 필터/재분류만 수집량(최대 어제 하루치 전체)에
비례해서 배치 수가 늘 수 있어 유일하게 예산을 남겨뒀다. `process_stage.py`가 각 단계에
`deadline=None`이 아니라 `float("inf")`를 넘기는 이유: `None`을 넘기면 각 함수가
"deadline 없을 때 쓰는 자기 모듈 기본값"(standalone 테스트용, 90분/120분 등)으로 폴백해
사실상 예산이 생겨버리기 때문 - `inf`를 명시하면 그 폴백 없이 정말 무제한으로 돈다(각
모듈 코드는 안 건드리고 호출부 값만 다르게 넘겨서 배분을 조정한 것).

**collect Job이 실패해도 그날 발송이 통째로 없어지지 않는다** — `process` Job에
`if: always()`가 걸려있어 collect Job의 성공 여부와 무관하게 항상 시도하고,
`process_stage.py`가 `collected.json`이 없거나 손상된 경우 빈 기사로 안전하게 시작해서
"오늘은 기사 0건"처럼 정상적으로 흘러간다(요약/저장/배포 각 단계의 기존 안전한 기본값이
그대로 적용됨).

### ⏱️ 단일 실행(`python main.py`) 시간예산 — 단계별 누적 체크포인트

`main.py`를 로컬에서 통째로 한 번에 돌릴 때는 예전 방식 그대로, `run()` 시작 시점(0분)부터
아래 5개 구간이 끝나야 하는 누적 마감 시각(분)을 한 번에 계산해서 해당 단계에 절대 시각
(`deadline`, `time.monotonic()` 기준)으로 넘겨준다:

| 구간 | 누적 체크포인트 | 구간 자체 소요 | 대상 |
|---|---|---|---|
| GDELT 수집 | 220분 | 220분(네이버 수집 포함) | `gdelt_collector.collect()` — 네이버는 이 구간 *안*에서 먼저 도는 거라 별도 예산 없이 여기 포함됨 |
| 관련성 필터 + 카테고리 재분류 | 270분 | 50분 | `relevance_filter.filter_articles()` / `recategorize_uncategorized()` (같은 deadline 공유) |
| 이슈 그룹핑 1~3차 | 305분 | 35분 | `issue_grouper.group_issues()`(2차 임베딩은 로컬 CPU라 예산 체크 의미 없음, 3차 LLM 보조만 실질적으로 체크) |
| 4차 Top N 사후 재검토 | 310분 | 5분 | `issue_grouper.stage4_dedupe_and_promote()` |
| LLM 요약 | 355분 | 45분 | `llm_summarizer.summarize_top_issues()`(국내/해외 + 카테고리별) |

각 단계는 "이 구간에 배정된 시간"이 아니라 "이 구간이 끝나야 하는 절대 시각"만 체크하므로,
앞 구간이 일찍 끝나면 그만큼이 자동으로 뒤 구간에 남고 늦게 끝나면 뒤 구간이 그만큼 줄어든다
(재조정 불필요). 예산을 넘긴 단계는 각자의 안전한 기본값으로 처리 — GDELT/관련성필터는
아직 못 본 나머지를 스킵(관련성필터는 "통과"로 살림), 그룹핑 3차는 애매 구간을 "안 묶음"
유지, 4차 재검토는 그 라운드부터 병합 중단하고 현재 순위 그대로, LLM 요약은 남은 이슈를
"요약 생략, 원문 제목만 노출"로 처리.

각 모듈의 자체 `TIME_BUDGET_SECONDS`류 상수는 `deadline`을 못 받았을 때(모듈 단독 테스트
등)만 쓰는 fallback으로, 실제 운영(Job 체이닝이든 단일 실행이든)에서는 호출부가 넘기는
`deadline` 값이 항상 우선한다.

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
| `ANTHROPIC_API_KEY` | Secret | Anthropic API 키(`LLM_PROVIDER=anthropic`일 때 사용) | 현재 운영에서는 미사용(아래 `LLM_PROVIDER` 참고) |
| `LLM_PROVIDER` | Variable | `anthropic`(코드 기본값) 또는 `openrouter` | 현재는 `openrouter`로 설정해서 운영 중 |
| `OPENROUTER_API_KEY` | Secret | OpenRouter API 키 | $10 선결제 크레딧이 연결된 계정 키(하루 요청 한도 1000건) |
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