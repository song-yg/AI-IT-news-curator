"""
issue_grouper.py - 이슈 그룹핑.
1차 KR<->EN 사전 매칭 -> 2차 BGE-M3 임베딩 코사인 유사도 -> 3차 LLM 그룹핑 보조(애매 구간) ->
4차 Top N 사후 재검토(stage4_dedupe_and_promote, 스코어링 이후 단계라 group_issues()엔 없음).
"""

import csv
import json
import os
import time
from itertools import combinations

import requests

import scorer
import llm_rate_limiter

from keyword_tagger import EXCLUDED_TERMS


# --- 1차: KR<->EN 키워드 사전 매칭 ---
# keyword_tagger.CATEGORY_KEYWORDS(넓은 카테고리 분류)와 다르게, 여기는 "완전히 같은 사건"
# 단위 매칭. 지금은 빈 리스트라 모든 기사가 2차(임베딩)로 넘어감 - 인프라만 남겨둠.
ISSUE_SYNONYM_GROUPS: list[set[str]] = []


def _stage1_match_keys(title: str) -> set[int]:
    """제목이 ISSUE_SYNONYM_GROUPS의 몇 번 묶음에 걸리는지."""
    title_lower = title.lower()
    matched = set()
    for idx, synonyms in enumerate(ISSUE_SYNONYM_GROUPS):
        usable_synonyms = synonyms - EXCLUDED_TERMS
        if any(syn.lower() in title_lower for syn in usable_synonyms):
            matched.add(idx)
    return matched


class UnionFind:
    """find(i): i의 그룹 대표. union(i, j): 같은 그룹으로 합침."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        root_i, root_j = self.find(i), self.find(j)
        if root_i != root_j:
            self.parent[root_j] = root_i

    def groups(self) -> list[list[int]]:
        buckets: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            root = self.find(i)
            buckets.setdefault(root, []).append(i)
        return list(buckets.values())


def stage1_group(articles: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """1차 키워드 사전 매칭만으로 그룹 생성. 1건만 걸린 건 unmatched로."""
    n = len(articles)
    uf = UnionFind(n)

    key_to_article_indices: dict[int, list[int]] = {}
    for i, article in enumerate(articles):
        keys = _stage1_match_keys(article.get("title", ""))
        for key in keys:
            key_to_article_indices.setdefault(key, []).append(i)

    for indices in key_to_article_indices.values():
        for a, b in combinations(indices, 2):
            uf.union(a, b)

    grouped = []
    unmatched = []
    for indices in uf.groups():
        if len(indices) >= 2:
            grouped.append([articles[i] for i in indices])
        else:
            unmatched.append(articles[indices[0]])

    return grouped, unmatched


# --- 2차: BGE-M3 임베딩 코사인 유사도 매칭 ---
THRESHOLD = 0.7  # 잠정값 - export_similarity_scores로 튜닝
BORDERLINE_MARGIN = 0.06  # threshold 근처 애매 구간 폭(3차 LLM 보조 대상)

SIMILARITY_DEBUG_CSV = (os.environ.get("SIMILARITY_DEBUG_CSV") or "").strip().lower() in ("1", "true", "yes", "on")


def _embedding_text(article: dict) -> str:
    """"제목 + 본문/설명 200자". 이 프로젝트는 body가 항상 None이라 실질적으로 description만 있음."""
    title = article.get("title", "")
    material = article.get("body") or article.get("description")
    if material:
        return f"{title} {material[:200]}"
    return title


def _cosine_similarity_matrix(vectors):
    """N x N 코사인 유사도 행렬."""
    import numpy as np

    vectors = np.array(vectors)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    normalized = vectors / norms
    return normalized @ normalized.T


def export_similarity_scores(articles: list[dict], sim_matrix, threshold: float,
                              borderline_margin: float, path: str = "similarity_debug/similarity_scores.csv",
                              min_score: float = 0.4) -> str | None:
    """THRESHOLD 튜닝용 유사도 디버그 CSV 저장(min_score 미만 제외). 실패해도 로그만."""
    n = len(articles)
    rows = []
    for i, j in combinations(range(n), 2):
        sim = float(sim_matrix[i][j])
        if sim < min_score:
            continue
        if sim >= threshold:
            status = "merged"
        elif threshold - borderline_margin <= sim < threshold:
            status = "borderline"
        else:
            status = "none"
        rows.append((sim, articles[i], articles[j], status))

    rows.sort(key=lambda r: r[0], reverse=True)

    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["similarity", "status", "title_a", "title_b",
                              "category_a", "category_b", "source_a", "source_b"])
            for sim, a, b, status in rows:
                writer.writerow([
                    f"{sim:.4f}", status,
                    a.get("title", ""), b.get("title", ""),
                    a.get("category", ""), b.get("category", ""),
                    a.get("source", ""), b.get("source", ""),
                ])
    except OSError as e:
        print(f"[issue_grouper] 🟡 주의 [IG-01] - 유사도 디버그 CSV 저장 실패: {path} - {type(e).__name__}: {e}")
        return None

    print(f"[issue_grouper] 유사도 디버그 CSV 저장 완료 ({len(rows)}쌍) -> {path}")
    return path


def stage2_group(
    articles: list[dict],
    model=None,
    threshold: float = THRESHOLD,
    borderline_margin: float = BORDERLINE_MARGIN,
) -> tuple[list[list[dict]], list[dict], list[tuple[dict, dict, float]]]:
    """1차에서 못 잡은 기사를 임베딩으로 매칭. 반환: (새 그룹, 여전히 미매칭, 애매 구간 쌍)."""
    if not articles:
        return [], [], []

    texts = [_embedding_text(a) for a in articles]
    vectors = model.encode(texts, normalize_embeddings=True)
    sim_matrix = _cosine_similarity_matrix(vectors)

    if SIMILARITY_DEBUG_CSV:
        export_similarity_scores(articles, sim_matrix, threshold, borderline_margin)

    n = len(articles)
    uf = UnionFind(n)

    for i, j in combinations(range(n), 2):
        sim = float(sim_matrix[i][j])
        if sim >= threshold:
            uf.union(i, j)

    borderline_pairs = []
    for i, j in combinations(range(n), 2):
        if uf.find(i) == uf.find(j):
            continue
        sim = float(sim_matrix[i][j])
        if threshold - borderline_margin <= sim < threshold:
            borderline_pairs.append((articles[i], articles[j], sim))

    grouped = []
    still_unmatched = []
    for indices in uf.groups():
        if len(indices) >= 2:
            grouped.append([articles[i] for i in indices])
        else:
            still_unmatched.append(articles[indices[0]])

    return grouped, still_unmatched, borderline_pairs


# --- 3차: LLM 그룹핑 보조(임계값 애매 구간만 대상) ---
LLM_MODEL_OPENROUTER = os.environ.get("OPENROUTER_MODEL") or "openrouter/free"
LLM_API_URL_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

LLM_MODEL_OPENROUTER_2 = os.environ.get("OPENROUTER_MODEL_2") or ""
LLM_MODEL_OPENROUTER_3 = os.environ.get("OPENROUTER_MODEL_3") or ""

_LLM_MODEL_CHAIN_OPENROUTER_ROLES: list[tuple[str, str]] = []
if LLM_MODEL_OPENROUTER:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("1순위", LLM_MODEL_OPENROUTER))
if LLM_MODEL_OPENROUTER_2:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("2순위", LLM_MODEL_OPENROUTER_2))
if LLM_MODEL_OPENROUTER_3:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("3순위", LLM_MODEL_OPENROUTER_3))
if "openrouter/free" not in [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("최종 안전망", "openrouter/free"))

_LLM_MODEL_ROLE_ERROR_CODE = {
    "1순위": "IG-02",
    "2순위": "IG-03",
    "3순위": "IG-04",
    "최종 안전망": "IG-05",
}

LLM_MODEL_CHAIN_OPENROUTER = [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]

LLM_BATCH_SIZE = 20
TIME_BUDGET_SECONDS = 90 * 60  # standalone 기본값 - 정상 실행은 호출부의 deadline이 우선

_OPENROUTER_X_TITLE = "AI-IT-news-issue-grouping-stage3"  # ASCII 필수(한글 섞이면 헤더 인코딩 실패)

_LLM_SYSTEM_PROMPT = (
    "You are a judge that assists news issue grouping. Given two article titles, decide whether the two articles cover \"exactly the same "
    "event\". "
    "Even if they cover the same technology/topic, judge them as separate events if the company, product, or timing differs (e.g. an article about OpenAI's GPT model release and one about Google's Gemini model release are separate events even though the topic name (LLM release) is the same). "
    "This applies not only at the company level but also to different products or divisions within the same company (e.g. an article about a security vulnerability in AWS and one in Azure OpenAI Service are both cloud AI security issues but are separate events if the affected product differs). "
    "Also judge them as separate events when one article covers a single issue (A) alone and the other article covers several issues including that one (A, B, C, D, etc.) together - overlapping on one issue does not make them the same event, because the scope and focus covered are different (e.g. a standalone article on \"OpenAI's new safety policy announcement\" and an article on \"OpenAI's safety policy, funding round, and leadership changes combined\" are not the same event even though the safety policy overlaps, because the latter is a separate roundup-style article covering multiple developments together). "
    "Titles may be in different languages (Korean/English/other languages mixed) - judge them as the same event if they refer to the same event, regardless of language. "
    "If you are not certain, you must answer false (conservative default - it is safer not to group than to group incorrectly)."
    "Output only a JSON array with no other explanation. Each element must be in the form {\"id\": number, \"same_event\": true|false}, and id must exactly match the number of the input pair."
)


def _build_llm_user_prompt(pairs: list[tuple[dict, dict, float]]) -> str:
    lines = ["Judge whether each of the following pairs of article titles covers exactly the same event.\n"]
    for idx, (a, b, _sim) in enumerate(pairs, start=1):
        lines.append(f"{idx}. A: \"{a.get('title', '')}\" / B: \"{b.get('title', '')}\"")
    lines.append(
        f'\nThere are {len(pairs)} pairs total. Include the number above as "id" in each '
        f'element and answer with a JSON array only (e.g. [{{"id": 1, "same_event": true}}, '
        f'{{"id": 2, "same_event": false}}, ...]). Do not omit any id or change the order.'
    )
    return "\n".join(lines)


def _snippet_for_log(text: str, limit: int = 200) -> str:
    if not text:
        return "(빈 응답)"
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _request_openrouter(system_prompt: str, user_prompt: str, api_key: str,
                         session: requests.Session, model_name: str) -> str:
    llm_rate_limiter.wait_for_openrouter_slot()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "X-Title": _OPENROUTER_X_TITLE,
    }
    body = {
        "model": model_name,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = session.post(LLM_API_URL_OPENROUTER, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _request_llm_text(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session,
                       validate=None):
    """모델 체인(1순위->2순위->3순위->최종 안전망)을 순서대로 시도, 실패하면 다음 모델로."""
    chain = _LLM_MODEL_CHAIN_OPENROUTER_ROLES
    last_error: Exception | None = None
    for idx, (role, model_name) in enumerate(chain):
        is_final = idx == len(chain) - 1
        try:
            if idx > 0:
                print(f"[issue_grouper] 🟡 주의 - 3차 그룹핑 {role} 모델('{model_name}')로 재시도 "
                      f"({idx + 1}/{len(chain)})")
            text = _request_openrouter(system_prompt, user_prompt, api_key, session, model_name)
            return validate(text, is_final) if validate else text
        except Exception as e:
            last_error = e
            code = _LLM_MODEL_ROLE_ERROR_CODE[role]
            level = "🔴 조치필요" if is_final else "🟡 주의"
            next_note = "더 시도할 모델 없음 - 이 배치 전체 '안 묶음' fallback" if is_final else "다음 후보 모델로 재시도"
            print(f"[issue_grouper] {level} [{code}] - 3차 그룹핑 {role} 모델('{model_name}') "
                  f"호출/응답 검증 실패 - {next_note}: {type(e).__name__} - {e!r}")
    raise last_error


def _call_llm(pairs: list[tuple[dict, dict, float]], api_key: str, session: requests.Session) -> list[bool] | None:
    """pairs 각각의 same_event 판정. 신뢰할 수 없는 응답이면 None."""
    user_prompt = _build_llm_user_prompt(pairs)

    def _validate(text: str, is_final: bool) -> list:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())

        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")
        return parsed

    try:
        parsed = _request_llm_text(_LLM_SYSTEM_PROMPT, user_prompt, api_key, session, validate=_validate)
    except Exception as e:
        print(f"[issue_grouper] 🔴 조치필요 [IG-06] - 3차 LLM 호출/파싱 실패 - 이 배치({len(pairs)}쌍)는 "
              f"전부 '안 묶음' fallback: {type(e).__name__} - {e!r}")
        return None

    by_id: dict[int, bool] = {}
    for item in parsed:
        try:
            by_id[int(item["id"])] = bool(item["same_event"])
        except (KeyError, TypeError, ValueError):
            continue

    results = []
    missing = []
    for idx in range(1, len(pairs) + 1):
        if idx in by_id:
            results.append(by_id[idx])
        else:
            missing.append(idx)
            results.append(False)

    if missing:
        print(f"[issue_grouper] 🟡 주의 [IG-07] - 3차 LLM 출력에서 id {missing} 누락"
              f"(기대 {len(pairs)}쌍 중 {len(missing)}쌍) - 그 쌍들만 '안 묶음' 기본값 처리")

    return results


def stage3_llm_assist(borderline_pairs: list[tuple[dict, dict, float]],
                       deadline: float | None = None) -> list[tuple[dict, dict, float]]:
    """애매 구간 쌍들을 LLM에 물어 "같은 사건"으로 확정된 쌍만 반환."""
    if not borderline_pairs:
        return []

    if not llm_rate_limiter.LLM_ENABLED:
        print(f"[issue_grouper] LLM_ENABLED=off - 3차 LLM 보조 스킵, "
              f"애매 구간 {len(borderline_pairs)}쌍 전부 '안 묶음' 기본값 유지")
        return []

    key_env_var = "OPENROUTER_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[issue_grouper] 🟡 주의 [IG-08] - {key_env_var} 없음 - 3차 LLM 보조 생략, "
              f"애매 구간 {len(borderline_pairs)}쌍 전부 '안 묶음' 기본값 유지")
        return []

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER)
    print(f"[issue_grouper] 3차 LLM 보조 시작 - model={model_desc}, "
          f"대상 {len(borderline_pairs)}쌍")

    effective_deadline = deadline if deadline is not None else time.monotonic() + TIME_BUDGET_SECONDS

    def _over_budget() -> bool:
        return time.monotonic() >= effective_deadline

    confirmed = []
    skipped_pairs = 0
    with requests.Session() as session:
        for i in range(0, len(borderline_pairs), LLM_BATCH_SIZE):
            if _over_budget():
                skipped_pairs = len(borderline_pairs) - i
                print(f"[issue_grouper] 🟡 주의 - 3차 LLM 보조 시간 예산 소진 - 남은 {skipped_pairs}쌍은 건너뜀")
                break

            batch = borderline_pairs[i:i + LLM_BATCH_SIZE]
            results = _call_llm(batch, api_key, session)
            if results is None:
                continue
            for (a, b, sim), same_event in zip(batch, results):
                if same_event:
                    confirmed.append((a, b, sim))

    print(f"[issue_grouper] 3차 LLM 보조 완료 - 애매 구간 {len(borderline_pairs)}쌍 중 "
          f"{len(confirmed)}쌍 '같은 사건'으로 최종 병합"
          + (f" ({skipped_pairs}쌍은 시간 예산 소진으로 미시도)" if skipped_pairs else ""))
    return confirmed


def _sort_group_by_representative(group: list[dict]) -> list[dict]:
    """판단 근거 텍스트(body 우선, 없으면 description)가 가장 긴 기사를 index 0(대표)으로 정렬."""
    def _material_length(article: dict) -> int:
        text = article.get("body") or article.get("description") or ""
        return len(text)
    return sorted(group, key=_material_length, reverse=True)


def group_issues(articles: list[dict], model=None, deadline: float | None = None) -> list[list[dict]]:
    """1차+2차+3차를 순서대로 실행해 최종 그룹 리스트 생성(각 그룹은 대표순 정렬됨)."""
    stage1_grouped, stage1_unmatched = stage1_group(articles)

    if model is None:
        print("[issue_grouper] 🟡 주의 [IG-09] - 임베딩 모델이 없어 2차(임베딩) 매칭 생략 - 1차 결과만 사용")
        singleton = [[a] for a in stage1_unmatched]
        return [_sort_group_by_representative(g) for g in stage1_grouped + singleton]

    stage2_grouped, still_unmatched, borderline_pairs = stage2_group(stage1_unmatched, model=model)

    confirmed_pairs: list[tuple[dict, dict, float]] = []
    if borderline_pairs:
        print(f"[issue_grouper] 임계값 애매 구간 {len(borderline_pairs)}쌍 발견 - 3차 LLM 보조로 최종 판단")
        confirmed_pairs = stage3_llm_assist(borderline_pairs, deadline=deadline)

    components = stage2_grouped + [[a] for a in still_unmatched]
    components = _merge_confirmed_components(
        components, confirmed_pairs,
        extra_confirm=lambda pairs: stage3_llm_assist(pairs, deadline=deadline))

    return [_sort_group_by_representative(g) for g in stage1_grouped + components]


def _merge_confirmed_components(components: list[list[dict]],
                                 confirmed_pairs: list[tuple[dict, dict, float]],
                                 extra_confirm=None) -> list[list[dict]]:
    """
    confirmed_pairs로 components를 추가 병합. 단순 Union-Find는 "A-B, B-C 확정"만으로
    A-C를 직접 확인 없이 한 그룹으로 묶는 연쇄(transitive chaining) 문제가 있어서, 각 연결
    그룹이 완전그래프(클리크)인지 검증 - 클리크면 병합, 아니면(사슬만 연결) 빠진 쌍만
    extra_confirm으로 재확인 후 재판정.
    """
    if not confirmed_pairs:
        return components

    url_to_component: dict[str, int] = {}
    for idx, comp in enumerate(components):
        for article in comp:
            url_to_component[article.get("url")] = idx

    edges: set[tuple[int, int]] = set()
    for a, b, _sim in confirmed_pairs:
        idx_a = url_to_component.get(a.get("url"))
        idx_b = url_to_component.get(b.get("url"))
        if idx_a is not None and idx_b is not None and idx_a != idx_b:
            edges.add((min(idx_a, idx_b), max(idx_a, idx_b)))

    if not edges:
        return components

    comp_uf = UnionFind(len(components))
    for idx_a, idx_b in edges:
        comp_uf.union(idx_a, idx_b)

    merged_components = []
    pending_recheck: list[tuple[int, ...]] = []
    for indices in comp_uf.groups():
        if len(indices) <= 2:
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
            continue

        is_clique = all(
            (min(x, y), max(x, y)) in edges
            for x, y in combinations(indices, 2)
        )
        if is_clique:
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
        else:
            pending_recheck.append(indices)

    if pending_recheck and extra_confirm is not None:
        extra_candidates: list[tuple[dict, dict, float]] = []
        pair_lookup: dict[tuple, tuple[int, int]] = {}
        for indices in pending_recheck:
            for x, y in combinations(indices, 2):
                key = (min(x, y), max(x, y))
                if key in edges:
                    continue
                rep_a, rep_b = components[x][0], components[y][0]
                extra_candidates.append((rep_a, rep_b, 0.0))
                pair_lookup[(rep_a.get("url"), rep_b.get("url"))] = key

        if extra_candidates:
            print(f"[issue_grouper] 사슬로만 연결된 컴포넌트 {len(pending_recheck)}개 발견 - "
                  f"빠진 쌍 {len(extra_candidates)}개만 대표 기사로 3차 LLM에 추가 확인")
            newly_confirmed = extra_confirm(extra_candidates)
            for a, b, _sim in newly_confirmed:
                key = pair_lookup.get((a.get("url"), b.get("url")))
                if key:
                    edges.add(key)

    for indices in pending_recheck:
        is_clique_now = all(
            (min(x, y), max(x, y)) in edges
            for x, y in combinations(indices, 2)
        )
        if is_clique_now:
            print(f"[issue_grouper] 사슬 컴포넌트 재확인으로 클리크 완성(컴포넌트 {len(indices)}개) - 병합")
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
        else:
            print(f"[issue_grouper] 🟡 주의 [IG-10] - 3차 확정 쌍이 사슬로만 연결됨(컴포넌트 "
                  f"{len(indices)}개) - 연쇄 병합 방지로 안 묶고 개별 유지")
            for idx in indices:
                merged_components.append(components[idx])

    return merged_components


# --- 4차: Top N 사후 재검토 + 병합 + 순위 승격 ---
# 3차와 판정 기준이 다름 - "시점이 달라도 같은 발병의 후속 보도면 같은 사건".
_STAGE4_SYSTEM_PROMPT = (
    "You are a judge that assists final de-duplication of a news issue "
    "ranking. Given two issue summaries (each made of one or more article "
    "titles about the same underlying story), decide whether they are "
    "actually reporting on the same real-world event or outbreak, even if "
    "worded very differently or reported on different days. "
    "Judge them as the SAME event if they describe the same disease "
    "outbreak or incident continuing, escalating, or being confirmed over "
    "time in the same country/region (e.g. an initial suspected-case "
    "report and a later article confirming wider spread of that same "
    "outbreak are the SAME event, even though the specific facts and "
    "wording changed as the story developed). "
    "Judge them as separate events if the country/region differs, or if "
    "they are genuinely unrelated incidents (different disease, different "
    "outbreak) that merely share a topic. "
    "When genuinely unsure, prefer NOT merging (same_event: false) - a "
    "missed merge is safer than an incorrect one. "
    "Output only a JSON array with no other explanation. Each element must "
    "be in the form {\"id\": number, \"same_event\": true|false}, and id "
    "must exactly match the number of the input pair."
)


def _build_stage4_user_prompt(pairs: list[tuple[dict, dict]]) -> str:
    """item(그룹)당 대표 제목 최대 5개를 A/B로 나열해 판정 요청."""
    lines = ["Judge whether each of the following pairs of issue summaries covers the same real-world event.\n"]
    for idx, (item_a, item_b) in enumerate(pairs, start=1):
        titles_a = " / ".join(item_a.get("titles", [])[:5]) or "(제목 없음)"
        titles_b = " / ".join(item_b.get("titles", [])[:5]) or "(제목 없음)"
        lines.append(f'{idx}. A: "{titles_a}" / B: "{titles_b}"')
    lines.append(
        f'\nThere are {len(pairs)} pairs total. Include the number above as "id" in each '
        f'element and answer with a JSON array only (e.g. [{{"id": 1, "same_event": true}}, '
        f'{{"id": 2, "same_event": false}}, ...]). Do not omit any id or change the order.'
    )
    return "\n".join(lines)


def _call_stage4_llm_batch(pairs: list[tuple[dict, dict]], api_key: str,
                            session: requests.Session) -> list[bool] | None:
    """단일 배치 호출. 실패 시 None."""
    user_prompt = _build_stage4_user_prompt(pairs)

    def _validate(text: str, is_final: bool) -> list:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())
        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")
        return parsed

    try:
        parsed = _request_llm_text(_STAGE4_SYSTEM_PROMPT, user_prompt, api_key, session, validate=_validate)
    except Exception as e:
        print(f"[issue_grouper] 🔴 조치필요 [IG-11] - 4차 재검토 LLM 호출/파싱 실패 - "
              f"이 배치({len(pairs)}쌍)는 전부 '병합 안 함' fallback: {type(e).__name__} - {e!r}")
        return None

    by_id: dict[int, bool] = {}
    for item in parsed:
        try:
            by_id[int(item["id"])] = bool(item["same_event"])
        except (KeyError, TypeError, ValueError):
            continue

    results = []
    missing = []
    for idx in range(1, len(pairs) + 1):
        if idx in by_id:
            results.append(by_id[idx])
        else:
            missing.append(idx)
            results.append(False)

    if missing:
        print(f"[issue_grouper] 🟡 주의 [IG-12] - 4차 재검토 LLM 출력에서 id {missing} 누락"
              f"(기대 {len(pairs)}쌍 중 {len(missing)}쌍) - 그 쌍들만 '병합 안 함' 기본값 처리")

    return results


def _call_stage4_llm(pairs: list[tuple[dict, dict]], api_key: str, session: requests.Session) -> list[bool]:
    """LLM_BATCH_SIZE 단위로 배치 호출 후 병합. 실패분은 False로 채움."""
    results: list[bool] = []
    for i in range(0, len(pairs), LLM_BATCH_SIZE):
        batch = pairs[i:i + LLM_BATCH_SIZE]
        batch_results = _call_stage4_llm_batch(batch, api_key, session)
        results.extend(batch_results if batch_results is not None else [False] * len(batch))
    return results


def stage4_dedupe_and_promote(ranked_pool: list[dict], top_n: int, label: str = "",
                               deadline: float | None = None) -> list[dict]:
    """상위 top_n 후보끼리 같은 사건인지 LLM 재확인, 병합하고 빈 자리는 다음 순위로 승격. 최대 3회."""
    if top_n is None:
        top_n = len(ranked_pool)
    candidates = list(ranked_pool[:top_n])
    reserve = list(ranked_pool[top_n:])

    if len(candidates) < 2:
        return candidates

    prefix = f"[issue_grouper] {label} " if label else "[issue_grouper] "

    if not llm_rate_limiter.LLM_ENABLED:
        print(f"{prefix}LLM_ENABLED=off - 4차 Top N 재검토 스킵(기존 순위 그대로 사용)")
        return candidates

    key_env_var = "OPENROUTER_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[issue_grouper] 🟡 주의 [IG-13] - {key_env_var} 없음 - "
              f"4차 Top N 재검토 생략(기존 순위 그대로 사용)")
        return candidates

    max_rounds = 3
    merged_any = False

    with requests.Session() as session:
        for round_idx in range(1, max_rounds + 1):
            if len(candidates) < 2:
                break

            if deadline is not None and time.monotonic() >= deadline:
                print(f"{prefix}🟡 주의 - 4차 Top N 재검토 시간 예산 소진 - {round_idx}회차부터 중단")
                break

            index_pairs = list(combinations(range(len(candidates)), 2))
            llm_pairs = [(candidates[i], candidates[j]) for i, j in index_pairs]
            print(f"{prefix}4차 Top N 재검토 {round_idx}회차 - 후보 {len(candidates)}건({len(index_pairs)}쌍) 확인")
            results = _call_stage4_llm(llm_pairs, api_key, session)

            merge_at = next((k for k, same in enumerate(results) if same), None)
            if merge_at is None:
                break

            i, j = index_pairs[merge_at]
            item_a, item_b = candidates[i], candidates[j]
            rep_a = item_a["titles"][0] if item_a.get("titles") else "(제목 없음)"
            rep_b = item_b["titles"][0] if item_b.get("titles") else "(제목 없음)"
            print(f"{prefix}🔗 같은 사건으로 판정돼 병합: '{rep_a}' + '{rep_b}'")

            merged_articles = item_a.get("articles", []) + item_b.get("articles", [])
            merged_item = scorer.score_group(merged_articles)
            merged_any = True

            candidates = [c for k, c in enumerate(candidates) if k not in (i, j)]
            candidates.append(merged_item)

            if reserve:
                promoted = reserve.pop(0)
                rep_p = promoted["titles"][0] if promoted.get("titles") else "(제목 없음)"
                print(f"{prefix}⬆️ 빈 자리에 다음 순위 후보 승격: '{rep_p}'")
                candidates.append(promoted)

            candidates.sort(key=lambda c: c["issue_score"], reverse=True)
            candidates = candidates[:top_n]
        else:
            print(f"{prefix}🟡 주의 - 4차 재검토가 최대 {max_rounds}회 한도에 도달함")

    if merged_any:
        print(f"{prefix}4차 Top N 재검토 완료 - 최종 {len(candidates)}건")

    return candidates