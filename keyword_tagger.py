"""
keyword_tagger.py
키워드 태깅 담당 모듈. 정규화(normalizer) 단계에서 각 기사의 제목을
KEYWORDS_KR/EN 사전과 매칭해 이슈 카테고리를 라벨링한다.

주의 - 이슈 그룹핑(issue_grouper.py)과는 완전히 별개 기능이다:
  - 이슈 그룹핑: "이 기사와 저 기사가 같은 사건을 다루는가" (BGE-M3 임베딩)
  - 키워드 태깅(이 모듈): "이 기사가 어떤 카테고리(질병/가격/제도 등)에 속하는가"
  둘은 입력도 출력도 다르고 서로 의존하지 않는다 - 이 모듈은 임베딩/LLM 없이
  순수 문자열 매칭만 쓴다.

CATEGORY_KEYWORDS는 `키워드표_20260714.md`의 1~9번 카테고리 표를 파싱해서
그대로 코드화한 것이다 (10번 "후보 확장 카테고리"는 문서에 "미확정 - 다음
논의 필요"라고 명시돼 있어 이번엔 제외했다). 파싱 규칙:
  - 셀 안의 "," (그리고 한국어 셀은 "/") 를 동의어 구분자로 보고 분리
  - 괄호 안의 설명 문구("(주 표현)", "(3km, HPAI 확진지 주변)" 등)는 매칭에
    쓸 문자열이 아니라 사람이 읽는 주석이라 제거하고, 괄호 앞부분만 남김
  - 예외 수동 보정 2건:
    1) "Ministry of Agriculture, Food and Rural Affairs, MAFRA" - 정식 명칭 자체에
       콤마가 들어있어서 자동 분리하면 셋으로 잘못 쪼개짐 -> 수동으로
       ["Ministry of Agriculture, Food and Rural Affairs", "MAFRA"] 두 개로 보정
    2) "grain price(s), global/international grain market" - "/"가 EN 셀 안에서
       구분자가 아니라 "global grain market 또는 international grain market"이라는
       뜻으로 쓰인 유일한 경우라, 자동 분리 시 "global"만 단독으로 남아 지나치게
       포괄적인 매칭어가 될 위험이 있어 ["global grain market", "international
       grain market"] 두 개로 수동 보정
  이 두 건 외에는 표 내용을 그대로 기계적으로 옮긴 것이라, 원본
  키워드표(🟢/🟡/🔴 확신도 포함)를 최종 근거로 삼는다.
"""

import re
from collections import Counter

CATEGORY_KEYWORDS = {
    "질병명": {
        "kr": ['조류독감', 'AI', '구제역', '아프리카돼지열병', '돼지열병', '럼피스킨병', '브루셀라병', '우결핵', '돼지유행성설사병', '돼지생식기호흡기증후군', '뉴캣슬병', '광우병', '살처분'],
        "en": ['avian influenza', 'avian flu', 'bird flu', 'HPAI', 'LPAI', 'foot-and-mouth disease', 'FMD', 'FMD outbreak', 'African swine fever', 'ASF', 'ASF outbreak', 'classical swine fever', 'hog cholera', 'CSF', 'lumpy skin disease', 'LSD', 'brucellosis', 'bovine tuberculosis', 'bTB', 'porcine epidemic diarrhea', 'PED', 'PEDv', 'porcine reproductive and respiratory syndrome', 'PRRS', 'PRRS virus', 'Newcastle disease', 'ND', 'bovine spongiform encephalopathy', 'BSE', 'mad cow disease', 'culling', 'stamping out', 'depopulation', 'mass depopulation'],
    },
    "시장/가격 용어": {
        "kr": ['사료가격', '사료값', '배합사료', '곡물가격', '국제곡물가', '옥수수', '대두박', '소맥', '사료 원료', '축산물 수급', '산지가격', '도매가격', '소비자가격', '사료자급률', '곡물자급률', '축산물 수출입'],
        "en": ['feed price', 'feed cost', 'feed costs', 'compound feed', 'mixed feed', 'formula feed', 'grain price', 'global grain market', 'international grain market', 'corn', 'maize', 'corn futures', 'CBOT corn', 'soybean meal', 'SBM', 'soybean futures', 'wheat', 'wheat futures', 'feed ingredients', 'livestock supply and demand', 'farm-gate price', 'wholesale price', 'retail price', 'consumer price', 'feed self-sufficiency rate', 'grain self-sufficiency rate', 'livestock exports', 'livestock imports', 'livestock trade'],
    },
    "정부·제도 용어": {
        "kr": ['농림축산식품부', '농림축산검역본부', '가축전염병예방법', '방역', '방역대', '이동제한', '축산물 이력제', '무항생제 축산물', '동물복지 인증', '가축분뇨', '예찰', '감시', '발생조사', '긴급대응'],
        "en": ['Ministry of Agriculture, Food and Rural Affairs', 'MAFRA', 'Animal and Plant Quarantine Agency', 'APQA', 'Act on the Prevention of Contagious Animal Diseases', 'biosecurity', 'quarantine', 'disease control', 'Protection Zone', 'Surveillance Zone', 'movement restriction', 'standstill order', 'movement ban', 'livestock traceability system', 'antibiotic-free livestock products', 'animal welfare certification', 'livestock manure', 'surveillance', 'disease surveillance', 'outbreak investigation', 'emergency response'],
    },
    "축종별 용어": {
        "kr": ['한우', '육우', '젖소', '낙농', '양돈', '산란계', '육계', '오리', '계란 수급', '가금 전반', '축산업 전반'],
        "en": ['Korean native cattle', 'Hanwoo', 'beef cattle', 'dairy cattle', 'dairy farming', 'hog farming', 'pig farming', 'swine industry', 'swine sector', 'laying hens', 'layers', 'broiler', 'broiler chicken', 'broiler industry', 'duck', 'egg supply', 'poultry sector', 'poultry industry', 'livestock sector', 'livestock industry'],
    },
    "사료업계 특화 용어": {
        "kr": ['배합사료업체', '사료공장', '조사료', 'TMR', '곡물엘리베이터', '사료', '프리믹스', '영양첨가업체'],
        "en": ['feed manufacturer', 'feed mill', 'feed producer', 'roughage', 'forage', 'Total Mixed Ration', 'TMR', 'grain elevator', 'animal feed', 'livestock feed', 'premix', 'premix manufacturer', 'nutrition company'],
    },
    "사료첨가제/항생제 규제": {
        "kr": ['사료첨가제', '항생제 성장촉진제', '항생제 내성', '동물용의약품', '항생제 사용 저감', '무항생제 사료', '사료관리법', '사료효소', '프로바이오틱스', '아미노산', '사료보충제', '피타아제'],
        "en": ['feed additive', 'antibiotic growth promoter', 'AGP', 'antimicrobial resistance', 'AMR', 'veterinary medicinal products', 'veterinary drugs', 'reduction of antibiotic use', 'antibiotic-free feed', 'Control of Livestock and Fish Feed Act', 'feed enzyme', 'probiotics', 'amino acid', 'lysine', 'methionine', 'feed supplement', 'phytase'],
    },
    "무역/관세 이슈": {
        # '할당관세'는 실제 축산물 관세 기사에서 압도적으로 많이 쓰이는
        # 용어라 포함한다. 기존 '저율관세할당'은 어순이 달라(저율+관세+
        # 할당) 부분 문자열로 '할당관세'를 못 잡는다.
        "kr": ['수입관세', '할당관세', '저율관세할당', '자유무역협정', '세이프가드', '원산지 표시', '검역협상', '무역분쟁', '수출금지', '수입금지', '시장접근', '교역제한'],
        "en": ['import tariff', 'tariff-rate quota', 'TRQ', 'Free Trade Agreement', 'FTA', 'safeguard measures', 'country-of-origin labeling', 'quarantine negotiations', 'trade dispute', 'export ban', 'import ban', 'market access', 'trade restriction'],
    },
    "가금 계열화/수직계열화": {
        "kr": ['계열화', '계열화사업법', '계열주체', '계약사육', '계열화 기업', '생산비 보장'],
        "en": ['vertical integration', 'Act on Livestock Farm Alliance Systems', 'vertical integrator', 'integrator', 'contract farmer', 'farmer raising livestock under contract', 'contract grower', 'integrated poultry company', 'guarantee of production cost', 'production cost compensation'],
    },
    "스마트팜/축산 기술": {
        "kr": ['정밀축산', '스마트축사', 'ICT 축산', '자동급이시스템', '축산 빅데이터', '축산 자동화', '센서기술'],
        "en": ['precision livestock farming', 'PLF', 'precision farming', 'precision feeding', 'smart livestock barn', 'smart farming', 'ICT-based livestock farming', 'digital agriculture', 'agtech', 'automatic feeding system', 'automated feeding system', 'livestock big data', 'data analytics in livestock', 'livestock automation', 'sensor technology', 'livestock monitoring', 'digital livestock'],
    },
}

# --- 매칭에서 제외하는 토큰 ---
#
# "AI"는 키워드표에 "조류독감"의 영문 축약형으로 등록돼 있지만, 요즘 뉴스에서
# "AI"는 인공지능(Artificial Intelligence) 의미로 압도적으로 더 많이 쓰인다.
# 다른 영문 표현("avian influenza", "avian flu", "bird flu", "HPAI", "LPAI")과
# 한글 표현("조류독감")이 이미 충분히 커버하고 있어서, 애매한 2글자 약어 하나
# 때문에 생기는 오매칭(가짜 "질병명" 태깅) 위험이 얻는 이득보다 크다고 판단해
# 매칭 대상에서만 제외한다 (표 자체는 그대로 두고, 여기서 런타임에만 제외 -
# gdelt_collector.py의 FALSE_POSITIVE_FILTERS와 같은 철학: 원본 사전은 안
# 건드리고 실행 시점에 알려진 위험 토큰만 걸러냄).
#
# "ND"도 같은 이유로 제외한다 - "nd"가 "underway"/"Uganda"/"Andy"/
# "neighbourhoods"처럼 영어에서 아주 흔한 2글자 조합이라, 축산/사료와
# 전혀 무관한 기사 다수가 "질병명" 카테고리로 잘못 태깅되는 게 확인됨
# (예: "National racing gets back underway at the GR Legends Rally" ->
# ['ND'] 매칭). "Newcastle disease"(풀네임)는 CATEGORY_KEYWORDS에 별도로
# 이미 등록돼 있어, "ND"를 빼도 뉴캣슬병 자체에 대한 매칭 능력은 유지된다
# - 풀네임으로 언급되는 기사만 못 잡게 될 뿐. LSD/TMR 등 나머지 약어는
# 실제 오매칭이 확인된 바 없어 그대로 둔다(확인된 것만 대응, 일반화된
# 규칙은 안 만드는 원칙).
EXCLUDED_TERMS = {"AI", "ND"}


def _build_flat_index():
    """
    {카테고리: {kr, en}} 구조를 {카테고리: [(매칭용 소문자 토큰, 원본 토큰), ...]}
    형태로 한 번만 평탄화해둔다 (매 기사마다 다시 만들지 않도록 모듈 로드
    시점에 1회만 계산).
    """
    index = {}
    for category, terms in CATEGORY_KEYWORDS.items():
        flat = []
        for term in terms["kr"] + terms["en"]:
            if term in EXCLUDED_TERMS:
                continue
            flat.append((term.lower(), term))
        index[category] = flat
    return index


_FLAT_INDEX = _build_flat_index()


def _dedupe_contained(terms: list[str]) -> list[str]:
    """
    리스트 안에서 다른 항목의 부분 문자열인 항목은 제거한다(대소문자 무시,
    더 구체적인/긴 쪽을 남김).

    배경: CATEGORY_KEYWORDS에 "feed cost"/"feed costs"처럼 한쪽이 다른 쪽의
    부분 문자열인 쌍이 다수 존재한다(corn/corn futures, wheat/wheat
    futures, 사료/배합사료업체, 계열화/계열화사업법 등). 이런 쌍은 같은
    개념을 사실상 두 번 세는 것이라, 제목에 "feed costs"가 있으면 "feed
    cost"와 "feed costs" 둘 다 매칭돼 그 카테고리의 매칭 개수가 인위적으로
    부풀려진다 - tag_title()의 "가장 많이 매칭된 카테고리 채택" 동점 처리
    공정성에 직접 영향을 준다(부풀려진 카테고리가 실제로는 안 이겨야 할
    경합에서 이기는 경우가 생길 수 있음).

    사전(CATEGORY_KEYWORDS) 자체는 원본 키워드표를 그대로 옮긴 것이라는
    이 모듈의 원칙(모듈 docstring 참고)을 지키기 위해, 사전을 고치는 대신
    이 집계 단계에서 구조적으로 해결한다 - 사전에 두 표현이 다 남아있어도
    "같은 언급"으로 인식되면 카운트는 1로만 잡힌다(더 구체적인 표현 쪽을
    matched_terms에 남겨서 정보 손실은 없음).

    예: ["corn", "corn futures", "CBOT corn"] -> ["corn futures", "CBOT corn"]
    """
    result = []
    for term in terms:
        term_lower = term.lower()
        if any(term != other and term_lower in other.lower() for other in terms):
            continue  # 리스트 안의 다른 항목에 완전히 포함되면 제외
        result.append(term)
    return result


def tag_title(title: str) -> tuple[str, list[str]]:
    """
    제목 하나를 카테고리에 매칭한다.

    매칭 방식: 대소문자 무시 부분 문자열(substring) 매칭. 카테고리별로 매칭된
    키워드 개수를 세서, 가장 많이 매칭된 카테고리를 채택한다 (여러 카테고리에
    걸치는 제목도 있을 수 있어서 - 예: "구제역으로 한우 수출 금지" 같은 제목은
    질병명/축종별/무역 세 카테고리 다 걸릴 수 있음). 동점이면 CATEGORY_KEYWORDS
    사전에 정의된 순서(= 키워드표 카테고리 번호 순) 상 먼저 나오는 쪽을 채택 -
    사전 순서 자체에 우선순위 의미는 없지만, 결과가 실행마다 흔들리지 않도록
    결정적(deterministic) 규칙 하나는 필요해서 정함.

    카테고리별 원 매칭 리스트에 _dedupe_contained를 적용해서, "corn"/"corn
    futures"처럼 한쪽이 다른 쪽에 포함되는 매칭은 하나로 센다(위
    _dedupe_contained 참고 - 매칭 개수 인위적 부풀림이 동점 처리 공정성을
    해치는 걸 방지).

    반환값: (category, matched_terms)
      - 아무 카테고리에도 안 걸리면 ("기타", []) - 2.2 스펙대로 기사를 탈락시키지
        않고 라벨만 "기타"로 붙인다.
    """
    if not title:
        return "기타", []

    title_lower = title.lower()
    best_category = None
    best_matches: list[str] = []

    for category, flat_terms in _FLAT_INDEX.items():
        raw_matches = [orig for lower, orig in flat_terms if lower in title_lower]
        matches = _dedupe_contained(raw_matches)
        if len(matches) > len(best_matches):
            best_category = category
            best_matches = matches

    if best_category is None:
        return "기타", []
    return best_category, best_matches


def tag_articles(articles: list[dict]) -> list[dict]:
    """
    기사 리스트 전체에 카테고리를 매긴다 (in-place로 "category" 필드를 채움).

    주의 - WATT 소스와의 관계: WATT_collector는 사이트에서 직접 긁어온
    카테고리(예: "Poultry", "Nutrition" 등 WATT 자체 분류 체계)를 이미
    "category" 필드에 채워서 넘긴다. 반면 naver/gdelt는 이 정규화 단계에서
    채우도록 None으로 비워둔 채 넘어온다.

    이 함수는 소스에 상관없이 모든 기사에 "우리 시스템의 통일된 카테고리
    체계"(질병명/시장가격/정부제도 등, 이 모듈의 CATEGORY_KEYWORDS 기준)를
    새로 매겨서 "category" 필드를 덮어쓴다 - WATT의 원래 사이트 카테고리는
    체계가 다르고(사이트마다 자체 기준) 이 프로젝트의 카테고리별 집계에는
    안 맞아서다. 다만 정보 손실을 막기 위해 WATT의 원래 값은 "site_category"
    필드에 별도로 보존한다.
    """
    other_count = 0
    for article in articles:
        original_category = article.get("category")
        if original_category and article.get("source") not in ("네이버", "GDELT"):
            # WATT 계열만 원래 category가 사이트 자체 분류였을 수 있음
            article["site_category"] = original_category

        category, matched_terms = tag_title(article.get("title", ""))
        article["category"] = category
        article["matched_keywords"] = matched_terms  # 디버깅/검수용, 저장 스펙 확정 시 유지 여부 재검토

        if category == "기타":
            other_count += 1

    total = len(articles)
    other_ratio = (other_count / total * 100) if total else 0.0
    print(f"[keyword_tagger] {total}건 중 '기타' {other_count}건 ({other_ratio:.1f}%) "
          f"- 비율이 높으면 사전에 신규 키워드 추가 검토")

    return articles


def print_category_distribution(articles: list[dict]) -> None:
    """카테고리별 건수 분포를 눈으로 확인하기 위한 진단용 함수."""
    counter = Counter(a.get("category", "기타") for a in articles)
    total = len(articles)
    print(f"\n=== 카테고리 분포 (전체 {total}건) ===")
    for category, count in counter.most_common():
        pct = count / total * 100 if total else 0
        print(f"  {category:15s} {count:4d}건 ({pct:.1f}%)")


def print_uncategorized_sample(articles: list[dict], sample_size: int = 30) -> None:
    """
    '기타'로 분류된 기사 제목 샘플을 출력한다.

    '기타' 비율이 높게 나올 때, 숫자(비율)만으로는 실제로 뭐가 몰리는지
    감이 안 잡히므로 - 키워드 사전을 어떻게 보강할지 판단하려면 실제
    제목을 눈으로 봐야 한다.

    무작위 추출이 아니라 리스트 앞에서부터 sample_size개만 그대로 보여준다
    - 표본 대표성을 엄밀히 따지는 통계용이 아니라 "감 잡기용" 진단
    도구라 단순하게 유지 (print_category_distribution과 같은 성격).

    ** main.py의 정규 실행 경로에서는 더 이상 호출하지 않음 **: 매 실행마다
    기사 제목을 최대 30건까지 나열해서 운영 로그가 불필요하게 길어짐 -
    사전 보강이 필요할 때 직접 이 함수를 불러써서 확인하는 진단 도구로만
    남겨둔다.
    """
    uncategorized = [a for a in articles if a.get("category", "기타") == "기타"]
    total = len(uncategorized)
    print(f"\n=== '기타' 분류 기사 샘플 (전체 {total}건 중 최대 {sample_size}건) ===")
    if total == 0:
        print("  (해당 없음)")
        return
    for i, article in enumerate(uncategorized[:sample_size], start=1):
        source = article.get("source", "?")
        title = article.get("title", "(제목 없음)")
        print(f"  {i:2d}. [{source}] {title}")
    if total > sample_size:
        print(f"  ... 외 {total - sample_size}건 생략")


if __name__ == "__main__":
    # 간단한 자체 점검용 - 실제 기사 없이도 매칭 로직이 도는지 확인
    sample_titles = [
        "전북서 고병원성 조류독감(AI) 추가 발생",
        "구제역 확산에 한우 수출 잠정 중단",
        "옥수수 국제가격 상승, 배합사료 원가 부담 커져",
        "농림축산식품부, 방역대 내 이동제한 조치 연장",
        "스마트축사 보급 확대... ICT 축산 기술 지원 예산 편성",
        "오늘의 날씨는 맑음",  # 매칭 안 되는 경우 -> 기타
    ]
    for t in sample_titles:
        category, matched = tag_title(t)
        print(f"[{category:15s}] {t}  <- {matched}")

    # 부분 문자열 포함 관계인 매칭이 중복 집계되지 않는지 검증
    # ("feed cost"가 "feed costs"의 부분 문자열이라 실제로 겹치는 게 확인된 사례)
    _, matched_dup = tag_title("Global feed costs surge amid grain shortage")
    assert not ("feed cost" in matched_dup and "feed costs" in matched_dup), \
        "feed cost/feed costs가 중복 집계됨 - _dedupe_contained 회귀"
    print(f"\n[검증] 'feed costs' 제목 매칭 결과 (중복 해소 확인): {matched_dup}")

    _, matched_corn = tag_title("CBOT corn futures rally on export demand")
    assert "corn" not in matched_corn, "일반형 'corn'이 안 걸러지고 남아있음"
    print(f"[검증] 'CBOT corn futures' 제목 매칭 결과: {matched_corn}")

    print("\n[keyword_tagger] 자체 점검 통과 (매칭 로직 + 부분문자열 중복 해소 검증)")