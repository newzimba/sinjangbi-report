"""F1·F2 후보 찾기: Claude 웹 검색으로 조사 → JSON 정리 → 출처·중복 검증.

1) 조사: web_search / web_fetch 서버 도구로 다국어 검색하고 조사 노트를 쓴다.
2) 정리: 조사 노트만 근거로 후보를 JSON 스키마에 맞춰 정리한다 (새 정보 추가 금지).
3) 검증: 조사 중 실제로 확인된 도메인의 출처만 남기고, 제외 도메인·보고 완료 장비를 걸러낸다.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Lock
from urllib.parse import urlparse

from .claude_client import FALLBACK_BETA, MODEL, get_client

SOURCE_TYPES = ["제조사 홈페이지·보도자료", "정부·도로기관 발표", "학술·연구 보고서", "전문 언론 기사"]
MIN_RELEVANCE = 6  # 도로 유지관리 장비 근접도(0~10)가 이보다 낮으면 후보에서 뺀다

DEV_YEARS = 3  # 개발(최초 공개) 후 이 기간이 지난 장비는 제외 (2026-11-18 사용자 요청)

RELEVANCE_GUIDE = """도로 유지관리 장비의 범위 (2026-11-18 확대): 직접 유지관리 작업 장비뿐 아니라 도로 점검·검사,
교통량·교통 데이터 수집, 도로 시설물 상태 모니터링 등 도로 유지관리에 다방면으로 활용되는 장비를 모두 포함한다.

도로 유지관리 장비 근접도(relevance, 0~10) 기준:
- 10: 도로 유지관리에 바로 쓰는 장비
      · 작업: 포장 보수·포트홀·크랙 실링, 차선 도색, 라바콘 설치·방호차 등 이동식 작업 안전, 제설·노면 청소·제초, 가드레일·표지 정비
      · 점검·검사: 노면 상태 조사차(균열·소성변형·평탄성), 교량·터널 점검 장비, 포장 비파괴 검사(GPR·FWD 등), 배수·비탈면 점검
      · 교통 데이터: 교통량·속도·차종 검지기, 축중·과적 단속 장비, 교통 데이터 수집 차량·드론·센서, 노면 기상·결빙 감지
      · 모니터링: 도로 시설물(교량·터널·포장·조명) 상태 상시 감시 센서·카메라 시스템
- 8~9: 위 장비의 핵심 모듈·시제품, 또는 도로기관이 유지관리·점검·교통관리용으로 시범 운영 중인 장비
- 6~7: 다른 시설(철도·공항·항만) 유지관리·점검 장비지만 도로에 거의 그대로 쓸 수 있는 장비
- 3~5: 일반 건설 장비·범용 드론·범용 자율주행차처럼 응용은 가능하지만 도로 유지관리·점검 전용이 아닌 것
- 0~2: 신규 도로 건설용 장비, 소프트웨어·정책만 있는 것, 도로와 무관한 것"""


def dev_cutoff(today=None):
    """개발 시기 하한: 오늘로부터 3년 전."""
    today = today or date.today()
    try:
        return today.replace(year=today.year - DEV_YEARS)
    except ValueError:  # 2월 29일
        return today.replace(year=today.year - DEV_YEARS, day=28)


def parse_dev(text):
    """'2024', '2024-05', '2024-05-12' → date (그 기간의 마지막 날로 너그럽게 본다). 모르면 None."""
    m = re.search(r"(19|20)\d{2}(?:[-./]\s*(\d{1,2}))?(?:[-./]\s*(\d{1,2}))?", text or "")
    if not m:
        return None
    y = int(m.group(0)[:4])
    mo = int(m.group(2)) if m.group(2) else 12
    d = int(m.group(3)) if m.group(3) else 28
    try:
        return date(y, max(1, min(mo, 12)), max(1, min(d, 28)))
    except ValueError:
        return None

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "search_note": {"type": "string"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "maker": {"type": "string"},
                    "country": {"type": "string"},
                    "category": {"type": "string"},
                    "summary": {"type": "string"},
                    "novelty": {"type": "string"},
                    "novelty_date": {"type": "string"},
                    "in_period": {"type": "boolean"},
                    "benchmark_value": {"type": "string"},
                    "relevance": {"type": "integer"},
                    "relevance_reason": {"type": "string"},
                    "developed": {"type": "string"},
                    "developed_basis": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                                "type": {"type": "string", "enum": SOURCE_TYPES},
                            },
                            "required": ["title", "url", "type"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "maker", "country", "category", "summary", "novelty",
                             "novelty_date", "in_period", "benchmark_value", "relevance", "relevance_reason",
                             "developed", "developed_basis", "sources"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["search_note", "candidates"],
    "additionalProperties": False,
}


# ---------- 프롬프트 ----------
def build_research_prompt(settings, start, end, categories, exclude_names, count, focus=None):
    cats = [c for c in settings.get("categories", []) if not categories or c["name"] in categories]
    kw_lines = []
    for c in cats:
        kw = "; ".join(f"{lang}: {', '.join(v)}" for lang, v in c.get("keywords", {}).items())
        kw_lines.append(f"- {c['name']} — {kw}")
    excl = "\n".join(f"- {n}" for n in exclude_names) or "- (없음)"
    focus_txt = (f"\n**이번 검색은 「{focus[0]}」 분야만 맡는다: {focus[1]}.** 다른 분야는 다른 검색이 맡으므로 찾지 않는다.\n"
                 if focus else "")
    return f"""너는 한국도로공사 첨단기계화센터(도로 유지관리 장비 개발)의 해외 기술동향 분석관이다.
기관장이 벤치마킹 여부를 판단할 주간 1p 보고서의 후보 장비를 찾는다.
{focus_txt}
# 조사 조건
- 조사 기간: {start} ~ {end} (이 기간에 발표·공개·시험·도입 소식이 나온 장비를 우선)
- 우선 국가: {', '.join(settings.get('countries', []))}
- 분야와 검색 키워드 (영어 외에 일본어·독일어 등 현지어로도 반드시 검색):
{chr(10).join(kw_lines)}
- 우선 참고 사이트: {', '.join(settings.get('preferred_domains', []))}

# 출처 규칙
- 근거로 쓸 수 있는 출처: {', '.join(SOURCE_TYPES)}
- 유튜브·블로그·커뮤니티·쇼핑몰·보도자료 재배포 사이트는 근거로 쓰지 않는다.
- 확인하지 못한 내용은 지어내지 말고 "확인 필요"라고 쓴다.

# 제외 대상 (이미 보고한 장비 또는 이번에 제외할 장비)
{excl}

# 선정 기준 — 도로 유지관리 장비에 가장 가까운 것부터
{RELEVANCE_GUIDE}

# 개발 시기 조건 — 최근 {DEV_YEARS}년 이내 개발 장비만
- 장비(또는 그 신형 모델·버전)가 처음 개발·공개·출시된 시점이 {dev_cutoff().isoformat()} 이후여야 한다.
- 오래된 제품의 단순 도입·납품·시범 운영 소식이면, 그 제품이 처음 나온 시점을 기준으로 판단한다.
  신형 모델이 새로 개발된 경우에만 신형 모델의 공개 시점을 쓴다.
- 처음 개발·공개 시점을 반드시 확인해 기록하고, {DEV_YEARS}년이 지난 장비는 후보에서 뺀다.
- 검색 한도가 정해져 있으므로, 개발 시기가 {DEV_YEARS}년이 넘은 것으로 보이는 장비는 더 조사하지 말고 바로 넘어간다.

# 해야 할 일
1. 검색해서 후보를 {count + 4}개 정도 찾는다. 실제 장비(또는 장비 시스템)여야 하고, 정책·행사 소식만 있는 것은 제외한다.
   **도로 유지관리 장비 근접도가 높은 장비를 우선해서 찾는다.** 작업 장비만이 아니라 도로 점검·검사 장비,
   교통량·교통 데이터 수집 장비, 시설물 상태 모니터링 장비도 같은 비중으로 찾는다.
2. 각 후보에 대해 다음을 조사 노트로 정리한다:
   장비명, 제조사, 국가, 분야, 한 줄 요약, 신규성 근거(언제 무엇이 발표됐는지, 날짜 포함),
   조사 기간 안의 소식인지 여부, 국내 도로 유지관리에 주는 벤치마킹 가치 한 줄,
   도로 유지관리 장비 근접도 점수(0~10)와 그 이유 한 줄,
   처음 개발·공개 시점(YYYY-MM)과 그 근거, 출처(제목, URL, 출처 유형).
3. 조사 기간 안에서 근접도 6 이상인 장비가 {count}개보다 적으면, 기간 직전 소식으로 채우고 "기간 외"라고 명시한다.
4. 노트 맨 끝에 이번 검색의 한계(예: 기간 내 발표가 적었음)를 한 줄로 적는다.
조사 노트는 한국어로 쓰고, 장비명은 원어 명칭을 괄호로 함께 적는다."""


def build_structuring_prompt(notes, count, category_names):
    return f"""아래 조사 노트를 근거로 후보 장비를 최대 {count + 3}개까지 JSON으로 정리하라 (근접도 높은 순).

규칙:
- 조사 노트에 있는 정보만 쓴다. 새로운 사실·수치·URL을 추가하지 않는다.
- 우선순위: **도로 유지관리 장비 근접도** > 조사 기간 안의 소식 > 작업자 안전 효과 > 노동력 절감.
- relevance는 아래 기준의 0~10 정수, relevance_reason은 그 이유 한 문장.
{RELEVANCE_GUIDE}
- category는 다음 중 가장 가까운 것: {', '.join(category_names)}
- novelty_date는 YYYY-MM-DD 또는 YYYY-MM 형식, 모르면 "확인 필요".
- in_period는 조사 기간 안의 소식이면 true, "기간 외"면 false.
- developed는 장비(또는 신형 모델)가 처음 개발·공개·출시된 시점 YYYY-MM (월을 모르면 YYYY, 모르면 "확인 필요").
  developed_basis는 그 근거 한 문장 (예: "2024-03 제조사 보도자료로 첫 공개").
- {DEV_YEARS}년이 지난({dev_cutoff().isoformat()} 이전에 개발된) 장비는 넣지 않는다.
- sources에는 노트에 적힌 URL만 넣고, 유튜브·블로그는 넣지 않는다.
- search_note에는 검색 한계를 한 문장으로 쓴다.

# 조사 노트
{notes}"""


# ---------- 유틸 ----------
def _norm_domain(url):
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _domain_matches(domain, patterns):
    return any(domain == p or domain.endswith("." + p) for p in patterns)


def _norm_name(s):
    return re.sub(r"[\s()（）·\-_/.,]", "", (s or "").lower())


def _collect_urls(content):
    """조사 응답에서 실제 검색·열람된 URL을 모은다."""
    urls = set()
    for b in content:
        if b.type == "web_search_tool_result" and isinstance(b.content, list):
            urls.update(getattr(x, "url", "") for x in b.content)
        elif b.type == "web_fetch_tool_result":
            u = getattr(b.content, "url", None)
            if u:
                urls.add(u)
        elif b.type == "text":
            for c in getattr(b, "citations", None) or []:
                u = getattr(c, "url", None)
                if u:
                    urls.add(u)
    urls.discard("")
    return urls


def filter_sources(sources, seen_domains, blocked):
    """조사 중 실제로 본 도메인의 출처만 남기고, 제외 도메인·중복 URL을 지운다 (F2)."""
    good, seen = [], set()
    for s in sources:
        key = (s.get("url") or "").rstrip("/")
        d = _norm_domain(key)
        if not key or key in seen or _domain_matches(d, blocked) or d not in seen_domains:
            continue
        seen.add(key)
        good.append(s)
    return good


def verify_candidates(raw, seen_domains, blocked, exclude_names, count, min_relevance=MIN_RELEVANCE, cutoff=None):
    """후보 검증: 근접도 높은 순(개발 시기 확인된 것 먼저) 정렬 → 개발 3년 경과·근접도 낮은 것·제외 장비 제거
    → 출처 필터. (후보 목록, 제외 사유) 반환.
    """
    cutoff = cutoff or dev_cutoff()
    excluded = {_norm_name(n) for n in exclude_names}
    candidates, dropped = [], []
    ranked = sorted(raw, key=lambda c: (-int(c.get("relevance", 0)), parse_dev(c.get("developed")) is None,
                                        not c.get("in_period", False)))
    for c in ranked:
        if _norm_name(c["name"]) in excluded:
            dropped.append(f"{c['name']}: 이미 보고했거나 제외한 장비")
            continue
        dev = parse_dev(c.get("developed"))
        if dev and dev < cutoff:
            dropped.append(f"{c['name']}: 개발 후 {DEV_YEARS}년 경과({c.get('developed')} 개발)")
            continue
        if int(c.get("relevance", 0)) < min_relevance:
            dropped.append(f"{c['name']}: 도로 유지관리 장비와 거리가 멂(근접도 {c.get('relevance', 0)}/10)")
            continue
        good = filter_sources(c["sources"], seen_domains, blocked)
        if not good:
            dropped.append(f"{c['name']}: 조사 중 확인된 공식 출처가 없음")
            continue
        c = {**c, "sources": good, "id": f"c{len(candidates) + 1}", "dev_unknown": dev is None}
        candidates.append(c)
        if len(candidates) >= count:
            break
    return candidates, dropped


# ---------- 검색 묶음 (분야별로 나눠 동시에 검색) ----------
# 한 번에 모든 분야를 검색하면 검색 한도가 작업 장비에 몰려 점검·교통 데이터 분야를 거의 못 본다.
SEARCH_GROUPS = [
    ("작업 장비", "포장 보수·차선 도색·이동식 작업 안전·제설·청소 등 도로 유지관리 작업 장비",
     ("포장", "이동식", "기타", "작업")),
    ("점검·검사", "노면 조사·포장 비파괴 검사·터널·교량·비탈면 점검 장비",
     ("점검", "검사", "터널", "교량")),
    ("교통 데이터·모니터링", "교통량·속도·축중 검지, 교통 데이터 수집, 노면 기상·결빙 감지, 시설물 상태 모니터링 장비",
     ("교통", "모니터링", "동향")),
]
GROUP_SEARCH_USES, GROUP_FETCH_USES = 12, 4


def group_categories(settings, categories):
    """분야를 검색 묶음 3개로 나눈다. [(묶음 이름, 설명, [분야…])] — 분야가 없는 묶음은 뺀다."""
    cats = [c for c in settings.get("categories", []) if not categories or c["name"] in categories]
    out, used = [], set()
    for label, desc, keys in SEARCH_GROUPS:
        mine = [c for c in cats if c["name"] not in used and any(k in c["name"] for k in keys)]
        used |= {c["name"] for c in mine}
        out.append([label, desc, mine])
    rest = [c for c in cats if c["name"] not in used]  # 어느 묶음에도 안 맞는 분야는 첫 묶음에
    out[0][2] += rest
    return [(label, desc, mine) for label, desc, mine in out if mine]


def _stream_search(client, prompt, tools, on_event, cancelled, effort="medium"):
    """웹 검색 도구로 조사 (pause_turn 이어받기 포함). (응답 블록, 검색어, 사용량) 반환."""
    messages = [{"role": "user", "content": prompt}]
    content, queries, usage = [], [], {"input": 0, "output": 0, "web_search": 0}
    for _turn in range(6):
        with client.beta.messages.stream(
            model=MODEL, max_tokens=32000, betas=[FALLBACK_BETA], fallbacks="default",
            output_config={"effort": effort}, tools=tools, messages=messages,
        ) as stream:
            for ev in stream:
                if cancelled():
                    raise InterruptedError("사용자가 취소했습니다.")
                if ev.type == "content_block_start":
                    on_event(ev.content_block.type)
            resp = stream.get_final_message()
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        stu = getattr(resp.usage, "server_tool_use", None)
        usage["web_search"] += getattr(stu, "web_search_requests", 0) or 0
        content.extend(resp.content)
        queries += [b.input["query"] for b in resp.content
                    if b.type == "server_tool_use" and isinstance(b.input, dict) and b.input.get("query")]
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude가 이 요청을 처리하지 않았습니다(안전 거절). 조건을 바꿔 다시 시도하세요.")
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    return content, queries, usage


# ---------- 개발 시기 확인 ----------
DEV_SCHEMA = {
    "type": "object",
    "properties": {"developed": {"type": "string"}, "developed_basis": {"type": "string"}},
    "required": ["developed", "developed_basis"], "additionalProperties": False,
}


def check_dev_date(c, tools, cancelled):
    """장비 1개의 처음 개발·공개 시점을 따로 검색해 확인한다. (결과 dict, 사용량)"""
    client = get_client()
    prompt = f"""다음 장비가 **처음 개발·공개·출시된 시점**을 확인하라.
장비: {c['name']} / 제조사: {c.get('maker')} / 국가: {c.get('country')}
알려진 출처: {', '.join(s['url'] for s in c.get('sources', []))}

- 제조사 보도자료, 제품 출시 기사, 시제품 공개 기사, 특허·논문 등으로 최초 공개 시점(YYYY-MM)을 찾는다.
- 이 모델이 기존 제품의 신형이면 신형 모델이 처음 공개된 시점을 쓴다. 기존 제품의 단순 도입·납품 소식 날짜는 쓰지 않는다.
- 찾지 못하면 developed를 "확인 필요"로 둔다. 지어내지 않는다.
결과는 마지막에 한 줄로: "developed: YYYY-MM / 근거: …" """
    content, _, usage = _stream_search(client, prompt, tools, lambda t: None, cancelled, effort="low")
    notes = "\n".join(b.text for b in content if b.type == "text")
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=2000, betas=[FALLBACK_BETA], fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": DEV_SCHEMA}},
        messages=[{"role": "user", "content": f"아래 메모에서 최초 개발·공개 시점(YYYY-MM, 모르면 '확인 필요')과 근거 한 문장을 뽑아라.\n\n{notes}"}],
    )
    usage["input"] += resp.usage.input_tokens
    usage["output"] += resp.usage.output_tokens
    return json.loads(next(b.text for b in resp.content if b.type == "text")), usage


def fill_dev_dates(cands, checker, limit, progress=lambda t: None):
    """개발 시기를 모르는 상위 후보(근접도 순)를 checker로 확인해 채운다. 확인한 후보 수를 돌려준다."""
    unknown = [c for c in sorted(cands, key=lambda c: -int(c.get("relevance", 0)))
               if int(c.get("relevance", 0)) >= MIN_RELEVANCE and parse_dev(c.get("developed")) is None][:limit]
    if not unknown:
        return 0
    progress(f"개발 시기를 모르는 후보 {len(unknown)}개의 개발 시점을 확인하는 중…")
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(checker, unknown))
    for c, r in zip(unknown, results):
        if r and parse_dev(r.get("developed")):
            c["developed"] = r["developed"]
            c["developed_basis"] = f"[개발 시기 확인 검색] {r.get('developed_basis', '')}"
    return len(unknown)


# ---------- 실행 ----------
def run_research(settings, start, end, categories, exclude_names, progress, cancelled):
    """조사 실행. progress(text) 로 진행 상황을 알리고, cancelled() 가 True면 중단한다.

    1) 분야 묶음(작업 장비 / 점검·검사 / 교통 데이터·모니터링)별로 동시에 검색
    2) 묶음별 노트를 합쳐 후보 JSON으로 정리
    3) 개발 시기를 모르는 상위 후보는 따로 검색해 개발 시점 확인
    4) 3년 경과·근접도·출처·중복 검증
    """
    client = get_client()
    count = int(settings.get("candidate_count", 3))
    blocked = settings.get("blocked_domains", [])
    category_names = [c["name"] for c in settings.get("categories", [])]

    def tools(search_uses, fetch_uses):
        t = [{"type": "web_search_20260209", "name": "web_search", "max_uses": search_uses},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": fetch_uses}]
        for x in t:
            if blocked:
                x["blocked_domains"] = blocked
        return t

    groups = group_categories(settings, categories)
    per_group = max(4, -(-(count + 4) // max(len(groups), 1)))  # 묶음마다 찾을 후보 수
    lock, counts = Lock(), {label: [0, 0] for label, _, _ in groups}

    def report():
        progress(" · ".join(f"{k} 검색 {v[0]}회" for k, v in counts.items()) + " 진행 중…")

    def run_group(g):
        label, desc, cats = g

        def on_event(t):
            with lock:
                if t == "web_search_tool_result":
                    counts[label][0] += 1
                elif t == "web_fetch_tool_result":
                    counts[label][1] += 1
                else:
                    return
            report()
        prompt = build_research_prompt({**settings, "categories": cats}, start, end, [], exclude_names, per_group - 4,
                                       focus=(label, desc))
        return label, _stream_search(client, prompt, tools(GROUP_SEARCH_USES, GROUP_FETCH_USES), on_event, cancelled)

    progress(f"{len(groups)}개 분야 묶음을 동시에 검색합니다…")
    with ThreadPoolExecutor(max_workers=len(groups)) as ex:
        results = list(ex.map(run_group, groups))

    all_content, queries, notes_parts = [], [], []
    usage = {"input": 0, "output": 0, "web_search": 0}
    for label, (content, q, u) in results:
        all_content += content
        queries += q
        for k in usage:
            usage[k] += u[k]
        text = "\n".join(b.text for b in content if b.type == "text").strip()
        if text:
            notes_parts.append(f"## [{label}] 조사 노트\n{text}")
    notes = "\n\n".join(notes_parts)
    if not notes:
        raise RuntimeError("조사 노트가 비어 있습니다. 다시 시도하세요.")
    seen_domains = {_norm_domain(u) for u in _collect_urls(all_content)}

    # 2) JSON 정리
    progress("분야별 후보를 합쳐 정리하는 중…")
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=16000, betas=[FALLBACK_BETA], fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": CANDIDATE_SCHEMA}},
        messages=[{"role": "user", "content": build_structuring_prompt(notes, count + 3, category_names)}],
    )
    usage["input"] += resp.usage.input_tokens
    usage["output"] += resp.usage.output_tokens
    if resp.stop_reason == "refusal":
        raise RuntimeError("후보 정리 단계에서 안전 거절이 발생했습니다. 다시 시도하세요.")
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))

    # 3) 개발 시기 확인 (모르는 상위 후보만)
    dev_tools = tools(3, 2)

    def checker(c):
        try:
            r, u = check_dev_date(c, dev_tools, cancelled)
            with lock:
                for k in ("input", "output"):
                    usage[k] += u[k]
            return r
        except InterruptedError:
            raise
        except Exception:
            return None
    checked = fill_dev_dates(data["candidates"], checker, count + 3, progress)

    # 4) 검증
    progress("3년 조건·근접도·출처를 검증하는 중…")
    candidates, dropped = verify_candidates(data["candidates"], seen_domains, blocked, exclude_names, count)

    return {
        "candidates": candidates,
        "search_note": data.get("search_note", ""),
        "dropped": dropped,
        "queries": queries,
        "stats": {"searches": sum(v[0] for v in counts.values()), "fetches": sum(v[1] for v in counts.values()),
                  "groups": {k: v[0] for k, v in counts.items()}, "dev_checked": checked, **usage},
        "notes": notes,
    }
