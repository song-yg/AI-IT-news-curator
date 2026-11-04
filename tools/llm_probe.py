"""OpenRouter 무료 모델 생존/속도/JSON 응답 품질 진단(파이프라인과 무관, 저장 안 함)."""
import json, os, re, time
import requests

KEY = os.environ.get("OPENROUTER_API_KEY", "")
H = {"Authorization": f"Bearer {KEY}", "content-type": "application/json", "X-Title": "llm-probe"}

models = requests.get("https://openrouter.ai/api/v1/models", timeout=60).json()["data"]
free = [m for m in models if m["id"].endswith(":free") or m["id"] == "openrouter/free"]
print(f"[probe] 전체 {len(models)}개 중 무료 {len(free)}개")
for m in sorted(free, key=lambda m: -m.get("created", 0)):
    print(f"  {m['id']} | ctx={m.get('context_length')} | created={time.strftime('%Y-%m-%d', time.gmtime(m.get('created', 0)))}")

CONFIGURED = ["poolside/laguna-s-2.1:free", "inclusionai/ling-3.0-flash:free",
              "nvidia/nemotron-3-super-120b-a12b:free", "inclusionai/ling-3.0-tiny:free",
              "nvidia/nemotron-nano-9b-v2:free", "google/gemma-4-26b-a4b-it:free"]
SYSTEM = "너는 뉴스 중복 판정기다. 반드시 JSON 배열만 출력한다. 설명 금지."
USER = ('다음 기사 쌍이 같은 사건이면 true, 아니면 false. 형식: [{"id":1,"same":true}]\n'
        '1. "삼성전자, 3분기 영업이익 12조 돌파" / "삼성전자 3Q 영업익 12조원…시장 기대 상회"\n'
        '2. "엔비디아 신형 GPU 공개" / "오픈AI, 새 추론 모델 출시"\n'
        '3. "카카오, AI 에이전트 베타 출시" / "카카오 AI 에이전트 베타 서비스 시작"')
EXPECT = [True, False, True]

def test(model):
    t0 = time.monotonic()
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=H, timeout=60,
                          json={"model": model, "temperature": 0,
                                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}]})
    except requests.RequestException as e:
        return f"ERR {type(e).__name__}", time.monotonic() - t0, ""
    dt = time.monotonic() - t0
    if r.status_code != 200:
        return f"HTTP {r.status_code}", dt, r.text[:120].replace("\n", " ")
    msg = r.json().get("choices", [{}])[0].get("message", {})
    content = msg.get("content")
    used = r.json().get("model", "")
    if not content:
        return "EMPTY content(None)", dt, f"served_by={used}"
    m = re.search(r"\[.*\]", content, re.S)
    try:
        arr = json.loads(m.group(0)) if m else None
        got = [bool(x.get("same")) for x in sorted(arr, key=lambda x: x.get("id", 0))]
        ok = "정답" if got == EXPECT else f"오답{got}"
    except Exception:
        ok = "JSON 파싱 실패"
    return f"200 {ok}", dt, f"served_by={used}"

cands = [m["id"] for m in free]
targets = list(dict.fromkeys(CONFIGURED + cands))
print(f"\n[probe] 실제 호출 테스트 {len(targets)}개 (각 2회)")
for mid in targets:
    res = []
    for _ in range(2):
        st, dt, extra = test(mid)
        res.append(f"{st} {dt:.1f}s")
        time.sleep(4)
    print(f"  {mid}: " + " | ".join(res) + f"  {extra}")
