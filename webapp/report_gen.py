"""F3 1p 초안 생성 · F5 항목별 다시 생성 · F7 문장별 출처 번호 정리.

1) 조사: 후보의 출처 원문을 읽고(web_fetch) 필요하면 추가 검색해 사실 노트를 쓴다.
2) 작성: 노트만 근거로 7개 항목을 JSON 스키마에 맞춰 쓴다. 사실 문장 끝에는 [n](참고 자료 번호)을 붙인다.
3) 검증: 조사 중 확인된 도메인의 출처만 남기고 [n] 번호를 다시 매긴다. 근거가 사라진 문장은 "확인 필요"로 보낸다.
"""
import json
import re

from .claude_client import FALLBACK_BETA, MODEL, get_client
from .research import _collect_urls, _norm_domain, filter_sources

JUDGEMENTS = ["권고", "보류", "불필요"]
SECTIONS = {  # 화면 이름 → 데이터 키
    "functions": "주요 기능", "specs": "제원", "improvements": "기존 방식 대비 개선점",
    "domestic": "국내 적용성", "judgement": "벤치마킹 판단",
}

_str_list = {"type": "array", "items": {"type": "string"}}
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "short_name": {"type": "string"},
        "maker": {"type": "string"}, "country": {"type": "string"},
        "released": {"type": "string"}, "category": {"type": "string"},
        "functions": _str_list, "specs": _str_list,
        "improvements": {
            "type": "object",
            "properties": {"safety": {"type": "string"}, "efficiency": {"type": "string"}, "cost": {"type": "string"}},
            "required": ["safety", "efficiency", "cost"], "additionalProperties": False,
        },
        "domestic": _str_list,
        "judgement": {"type": "string", "enum": JUDGEMENTS},
        "reasons": _str_list,
        "sources": {"type": "array", "items": {
            "type": "object", "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
            "required": ["title", "url"], "additionalProperties": False}},
        "checks": {"type": "array", "items": {
            "type": "object", "properties": {"text": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["text", "reason"], "additionalProperties": False}},
    },
    "required": ["name", "short_name", "maker", "country", "released", "category", "functions", "specs",
                 "improvements", "domestic", "judgement", "reasons", "sources", "checks"],
    "additionalProperties": False,
}

WRITING_RULES = """# 작성 규칙 (기관장 보고용 A4 1장 — 사진 포함해 반드시 1장)
- 한국어 개조식 보고서체로 쓴다. 문장은 "~함", "~임", "~됨"으로 끝낸다. 한 항목은 50자 이내(출처 번호 제외), 1줄.
- 분량: functions 3개, specs 0~2개("항목: 값" 형식), improvements 각 1문장, domestic 2개, reasons 2개.
- 개요 칸은 짧게: name은 한국어 명칭(원어 약칭) 40자 이내, maker 25자 이내, country 국가명만,
  released는 "2026-11 발표"처럼 15자 이내. 설명·괄호 부연은 개요 칸에 넣지 않는다.
- short_name은 파일명에 쓸 장비 키워드: 장비를 나타내는 한국어 단어 2~3개를 공백 없이 붙여 12자 이내
  (예: 노면동결센서, 포트홀보수로봇, 원격조종방호차, 교량점검드론).
- 사실·수치가 들어간 문장 끝에는 근거 출처 번호를 [1], [2]처럼 붙인다. 번호는 sources 배열 순서(1부터)다.
- 노트에 없는 사실·수치는 쓰지 않는다. 모르면 "확인 필요"라고 쓴다.
- 제조사·개발사가 스스로 밝힌 성능·비용 수치에는 "(업체 주장)"을 붙인다.
  개발 주체가 대학·연구기관이면 "(업체 주장)" 대신 "(연구팀 주장)"을 붙인다.
- 국내 적용성에서 한국도로공사 내부 사정(예산, 보유 장비, 조직)은 추정하지 말고 "검토 필요"로 쓴다.
- judgement는 권고(벤치마킹 추진 가치 큼) / 보류(추가 정보·실증 결과 대기) / 불필요(국내 적용성 낮음) 중 하나.
- checks에는 원문 대조가 필요한 내용을 넣는다: 업체 주장 수치, 출처가 하나뿐인 사실, 날짜가 불확실한 내용.
  text는 보고서 속 해당 표현, reason은 대조가 필요한 이유(예: "업체 주장", "단일 출처", "날짜 불확실").
- sources는 노트에 적힌 공식 출처 URL만 쓴다(유튜브·블로그 제외). 최대 5개."""


def build_research_prompt(c):
    srcs = "\n".join(f"- {s['title']}: {s['url']}" for s in c["sources"])
    return f"""너는 한국도로공사 첨단기계화센터의 해외 기술동향 분석관이다.
아래 장비로 기관장 보고용 1p를 쓰기 위해 사실을 조사하라.

# 장비
- 이름: {c['name']} / 제조사: {c['maker']} / 국가: {c['country']} / 분야: {c['category']}
- 후보 요약: {c['summary']}
- 신규성: {c['novelty']} ({c['novelty_date']})
- 알려진 출처:
{srcs}

# 할 일
1. 위 출처 원문을 먼저 읽는다(web_fetch). 부족하면 영어·현지어로 추가 검색한다.
2. 공식 자료(제조사 홈페이지·보도자료, 정부·도로기관 발표, 학술·연구 보고서, 전문 언론 기사)만 근거로 쓴다.
3. 다음을 사실 노트로 정리한다. 사실마다 근거 URL을 붙이고, 업체 자체 주장인지 표시한다:
   주요 기능, 제원(수치·단위), 공개·시험·도입 시기, 기존 방식 대비 안전·효율·비용 개선점,
   국내 고속도로 유지관리 작업(차로 차단 작업, 이동식 작업 등)에 적용할 때 고려할 점, 한계·위험 요소.
4. 확인하지 못한 내용은 "확인 못함"이라고 적는다. 지어내지 않는다."""


def build_writing_prompt(c, notes):
    return f"""아래 사실 노트만 근거로 '{c['name']}'의 1p 보고서 항목을 JSON으로 작성하라.
category는 "{c['category']}"을 그대로 쓴다.

{WRITING_RULES}

# 사실 노트
{notes}"""


# ---------- [n] 출처 번호 정리 ----------
MARK_RE = re.compile(r"\[(\d+)\]")


def remap_markers(data, kept_old_indexes):
    """출처 필터 후 [n] 번호를 새 순서로 바꾼다. 근거가 사라진 문장은 확인 필요 목록에 추가."""
    mapping = {old: new for new, old in enumerate(kept_old_indexes, start=1)}
    lost = []

    def fix(text):
        def rep(m):
            old = int(m.group(1))
            if old in mapping:
                return f"[{mapping[old]}]"
            lost.append(text)
            return ""
        return MARK_RE.sub(rep, text).replace("  ", " ").strip()

    for key in ("functions", "specs", "domestic", "reasons"):
        data[key] = [fix(t) for t in data.get(key, [])]
    data["improvements"] = {k: fix(v) for k, v in data.get("improvements", {}).items()}
    for t in dict.fromkeys(lost):
        data["checks"].append({"text": MARK_RE.sub("", t).strip(), "reason": "근거 출처가 검증에서 제외됨", "checked": False})
    return data


def verify_report(data, seen_domains, blocked):
    """출처 필터 + 번호 재정리 + 확인 항목 기본값."""
    original = data.get("sources", [])
    kept = filter_sources(original, seen_domains, blocked)
    kept_keys = [s["url"].rstrip("/") for s in kept]
    kept_old = []
    for key in kept_keys:  # 원래 몇 번이었는지 찾기
        for i, s in enumerate(original, start=1):
            if s["url"].rstrip("/") == key:
                kept_old.append(i)
                break
    data["sources"] = kept
    data["checks"] = [{**c, "checked": False} for c in data.get("checks", [])]
    data = remap_markers(data, kept_old)
    if data.get("judgement") not in JUDGEMENTS:
        data["judgement"] = "보류"
    return data


# ---------- 실행 ----------
def generate_report(candidate, settings, progress, cancelled):
    client = get_client()
    blocked = settings.get("blocked_domains", [])
    tools = [{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 6},
             {"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    if blocked:
        for t in tools:
            t["blocked_domains"] = blocked

    messages = [{"role": "user", "content": build_research_prompt(candidate)}]
    content, n_fetch, n_search = [], 0, 0
    usage = {"input": 0, "output": 0}
    progress("출처 원문을 읽는 중…")
    for _ in range(6):
        with client.beta.messages.stream(
            model=MODEL, max_tokens=32000, betas=[FALLBACK_BETA], fallbacks="default",
            output_config={"effort": "high"}, tools=tools, messages=messages,
        ) as stream:
            for ev in stream:
                if cancelled():
                    raise InterruptedError("사용자가 취소했습니다.")
                if ev.type == "content_block_start":
                    t = ev.content_block.type
                    if t == "web_fetch_tool_result":
                        n_fetch += 1
                    elif t == "web_search_tool_result":
                        n_search += 1
                    elif t == "text":
                        progress(f"원문 {n_fetch}건·검색 {n_search}회 확인 — 사실 노트 작성 중…")
                        continue
                    else:
                        continue
                    progress(f"원문 {n_fetch}건 읽음 · 추가 검색 {n_search}회…")
            resp = stream.get_final_message()
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        content.extend(resp.content)
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude가 이 요청을 처리하지 않았습니다(안전 거절).")
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break

    notes = "\n".join(b.text for b in content if b.type == "text").strip()
    if not notes:
        raise RuntimeError("사실 노트가 비어 있습니다. 다시 생성해 주세요.")
    seen = {_norm_domain(u) for u in _collect_urls(content)}
    seen |= {_norm_domain(s["url"]) for s in candidate["sources"]}  # 후보 단계에서 이미 검증된 출처

    progress("1p 항목을 작성하는 중…")
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=16000, betas=[FALLBACK_BETA], fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
        messages=[{"role": "user", "content": build_writing_prompt(candidate, notes)}],
    )
    usage["input"] += resp.usage.input_tokens
    usage["output"] += resp.usage.output_tokens
    if resp.stop_reason == "refusal":
        raise RuntimeError("1p 작성 단계에서 안전 거절이 발생했습니다.")
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    data = verify_report(data, seen, blocked)
    data["usage"] = usage
    return data, notes


TEXT_KEYS = ["name", "maker", "country", "released", "functions", "specs", "improvements", "domestic", "reasons"]


def shorten_report(data, pages):
    """F6 줄이기: 사실·출처 번호·(업체 주장)은 유지하고 글자 수만 줄인다."""
    sub = {"type": "object", "properties": {k: REPORT_SCHEMA["properties"][k] for k in TEXT_KEYS},
           "required": TEXT_KEYS, "additionalProperties": False}
    current = {k: data.get(k) for k in TEXT_KEYS}
    prompt = f"""아래 1p 보고서 내용이 워드에서 {pages}페이지가 나온다. 사진 포함 A4 1장에 들어가도록 줄여라.

규칙:
- 전체 글자 수를 약 35% 줄인다. 덜 중요한 문장은 빼고(functions 최소 2개, domestic·reasons 최소 1개 유지), 나머지는 짧게 다듬는다.
- 새로운 사실을 추가하지 않는다. 남기는 문장의 [n] 출처 번호와 "(업체 주장)", "(연구팀 주장)", "확인 필요", "검토 필요" 표시는 그대로 둔다.
- 개요 칸(name·maker·country·released)은 부연 설명을 빼고 짧은 명칭만 남긴다.
- 개조식("~함", "~임")을 유지한다.

# 현재 내용
{json.dumps(current, ensure_ascii=False)}"""
    resp = get_client().beta.messages.create(
        model=MODEL, max_tokens=8000, betas=[FALLBACK_BETA], fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": sub}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("줄이기 중 안전 거절이 발생했습니다.")
    out = json.loads(next(b.text for b in resp.content if b.type == "text"))
    return _drop_bad_markers(out, len(data.get("sources", [])))


def _drop_bad_markers(out, n):
    """없는 출처 번호([0], [n+1] 이상)는 지운다."""
    def fix(t):
        return MARK_RE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= n else "", t).strip()
    for k, v in out.items():
        if isinstance(v, list):
            out[k] = [fix(t) for t in v]
        elif isinstance(v, dict):
            out[k] = {kk: fix(t) for kk, t in v.items()}
    return out


def regenerate_section(section, data, notes):
    """F5 항목별 다시 생성 (웹 검색 없이 저장된 노트만 사용)."""
    if section not in SECTIONS:
        raise ValueError("다시 생성할 수 없는 항목입니다.")
    keys = ["judgement", "reasons"] if section == "judgement" else [section]
    sub = {"type": "object", "properties": {k: REPORT_SCHEMA["properties"][k] for k in keys},
           "required": keys, "additionalProperties": False}
    srcs = "\n".join(f"[{i}] {s['title']} {s['url']}" for i, s in enumerate(data.get("sources", []), 1))
    current = {k: data.get(k) for k in keys}
    prompt = f"""'{data['name']}' 1p 보고서의 「{SECTIONS[section]}」 항목만 다시 써라.
지금 내용과 다르게, 더 간결하고 정확하게 쓴다. 출처 번호는 아래 참고 자료 번호를 쓴다.

{WRITING_RULES}

# 참고 자료 번호
{srcs}

# 지금 내용
{json.dumps(current, ensure_ascii=False)}

# 사실 노트
{notes}"""
    resp = get_client().beta.messages.create(
        model=MODEL, max_tokens=8000, betas=[FALLBACK_BETA], fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": sub}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("다시 생성 중 안전 거절이 발생했습니다.")
    out = json.loads(next(b.text for b in resp.content if b.type == "text"))
    return _drop_bad_markers(out, len(data.get("sources", [])))
