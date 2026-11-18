"""교차 검증: 1p의 사실 문장을 '원래 출처가 아닌 다른 기관의 자료'로 다시 확인하고, 틀린 내용은 고친다.

1) 조사: 원래 출처 도메인(같은 기관 사이트 포함)을 막은 상태로 웹 검색·원문 읽기 → 문장별 판정 노트.
2) 정리: 문장 id별 판정(일치/불일치/확인 불가), 근거, 고친 문장, 근거 출처를 JSON으로.
3) 적용: 불일치 + 독립 출처가 실제로 확인된 경우에만 문장을 고치고 출처를 추가한다.
   고친 문장과 확인 못 한 문장은 모두 "확인 필요"에 넣어 사람이 원문 대조하게 한다.
"""
import json
from datetime import datetime

from .claude_client import FALLBACK_BETA, MODEL, get_client
from .report_gen import MARK_RE
from .research import _collect_urls, _domain_matches, _norm_domain

VERDICTS = ["일치", "불일치", "확인 불가"]
OVERVIEW = {"maker": "제조사·개발 주체", "country": "국가", "released": "공개 시기"}
SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "verdict": {"type": "string", "enum": VERDICTS},
            "evidence": {"type": "string"},
            "correction": {"type": "string"},
            "source_title": {"type": "string"},
            "source_url": {"type": "string"},
        },
        "required": ["id", "verdict", "evidence", "correction", "source_title", "source_url"],
        "additionalProperties": False}}},
    "required": ["results"], "additionalProperties": False,
}


def base_domain(domain):
    """같은 기관 판단용: news.rub.de → rub.de, www.dot.ca.gov → ca.gov (단순 규칙)."""
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "go", "or", "ac", "gov", "com", "org", "edu", "ne") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def claims_of(d):
    """검증할 사실 문장 목록 [{id, text}]. 국내 적용성(분석·의견)은 제외."""
    out = [{"id": k, "text": f"{label}: {d.get(k)}"} for k, label in OVERVIEW.items() if d.get(k)]
    for key in ("functions", "specs", "reasons"):
        out += [{"id": f"{key}.{i}", "text": MARK_RE.sub("", t).strip()} for i, t in enumerate(d.get(key) or [])]
    for k, v in (d.get("improvements") or {}).items():
        if v:
            out.append({"id": f"improvements.{k}", "text": MARK_RE.sub("", v).strip()})
    return out


def get_text(d, cid):
    if cid in OVERVIEW:
        return d.get(cid)
    key, _, idx = cid.partition(".")
    if key == "improvements":
        return (d.get("improvements") or {}).get(idx)
    items = d.get(key) or []
    return items[int(idx)] if idx.isdigit() and int(idx) < len(items) else None


def set_text(d, cid, text):
    if cid in OVERVIEW:
        d[cid] = text
        return
    key, _, idx = cid.partition(".")
    if key == "improvements":
        d.setdefault("improvements", {})[idx] = text
    else:
        d[key][int(idx)] = text


def apply_results(d, results, seen_domains, original_domains, blocked):
    """판정 결과를 1p에 적용. (고친 데이터, 기록) 반환. 독립 출처가 확인된 불일치만 고친다."""
    d = json.loads(json.dumps(d, ensure_ascii=False))  # 깊은 복사
    orig_bases = {base_domain(x) for x in original_domains}
    checks_text = {c["text"] for c in d.get("checks", [])}
    records = []

    def add_check(text, reason):
        if text and text not in checks_text:
            d.setdefault("checks", []).append({"text": text, "reason": reason, "checked": False})
            checks_text.add(text)

    for r in results:
        old = get_text(d, r["id"])
        if old is None:
            continue
        url = (r.get("source_url") or "").strip()
        dom = _norm_domain(url)
        independent = bool(url) and dom in seen_domains and base_domain(dom) not in orig_bases \
            and not _domain_matches(dom, blocked)
        rec = {"id": r["id"], "before": MARK_RE.sub("", old).strip(), "verdict": r["verdict"],
               "evidence": r.get("evidence", ""), "source": {"title": r.get("source_title", ""), "url": url}
               if independent else None, "after": None}
        if r["verdict"] == "불일치" and independent and r.get("correction", "").strip():
            srcs = d.setdefault("sources", [])
            urls = [s["url"].rstrip("/") for s in srcs]
            if url.rstrip("/") in urls:
                n = urls.index(url.rstrip("/")) + 1
            else:
                srcs.append({"title": r.get("source_title") or dom, "url": url})
                n = len(srcs)
            fixed = MARK_RE.sub("", r["correction"]).strip()
            new = fixed if r["id"] in OVERVIEW else f"{fixed} [{n}]"
            set_text(d, r["id"], new)
            rec["after"] = new
            add_check(fixed, f"교차 검증으로 수정됨 (이전: {rec['before'][:60]})")
        elif r["verdict"] == "불일치":
            rec["verdict"] = "불일치 의심"
            add_check(rec["before"], f"다른 출처와 다를 수 있음 — {rec['evidence'][:80]} (독립 출처 미확인으로 자동 수정 안 함)")
        elif r["verdict"] == "일치" and not independent:
            rec["verdict"] = "확인 불가"
            rec["evidence"] = "독립 출처 없이 판단됨 — " + rec["evidence"]
        if rec["verdict"] == "확인 불가" and any(ch.isdigit() for ch in rec["before"]):
            add_check(rec["before"], "독립 출처로 교차 확인 못 함 (수치·날짜 포함)")
        records.append(rec)
    return d, records


def research_prompt(d, claims, original_domains):
    lines = "\n".join(f"- [{c['id']}] {c['text']}" for c in claims)
    return f"""너는 사실 확인(팩트체크) 담당자다. 아래는 '{d.get('name')}'에 대한 보고서 문장이다.
이 문장들은 다음 사이트 자료만 근거로 쓰였다: {', '.join(sorted(original_domains))}

# 할 일
1. 위 사이트(같은 기관의 다른 사이트 포함)가 아닌 **독립된 출처**를 찾아라:
   다른 언론 보도, 정부·도로기관 자료, 다른 연구기관·학회 자료, 협력 기관 발표 등. 영어·현지어로 검색한다.
2. 찾은 원문을 읽고, 문장마다 독립 출처의 내용과 비교해 판정하라:
   - 일치: 독립 출처가 같은 내용을 말함
   - 불일치: 독립 출처가 다른 내용(수치·날짜·명칭·기관 등)을 말함 → 올바른 문장을 제시
   - 확인 불가: 독립 출처에서 이 내용을 찾지 못함
3. 판정마다 근거(원문 표현을 짧게 인용)와 출처 URL을 적어라. 지어내지 않는다.

# 검증할 문장
{lines}"""


def structure_prompt(notes, claims):
    ids = ", ".join(c["id"] for c in claims)
    return f"""아래 팩트체크 노트를 JSON으로 정리하라. 문장 id: {ids}
- 모든 id에 대해 결과를 하나씩 낸다. 노트에 없으면 verdict "확인 불가".
- 문장의 일부라도(수치·날짜·명칭·주체, "(업체 주장)" 같은 표기 포함) 틀렸으면 "일치"가 아니라 "불일치"다.
- correction: 불일치일 때만, 원래 문장과 같은 개조식 문체("~함", "~임")로 고친 문장. 원래 문장보다 길게 쓰지 않는다
  (A4 1장 유지). 틀린 부분만 고친다. 대학·연구기관이 개발한 장비의 자체 주장은 "(연구팀 주장)"으로 표기한다. 그 외에는 "".
- source_url: 판정 근거가 된 독립 출처 URL (없으면 ""). 노트에 있는 URL만 쓴다.
- evidence: 근거를 한국어 한 문장으로.

# 노트
{notes}"""


def cross_check(d, settings, progress):
    """교차 검증 실행. (고친 데이터, 기록, 사용량) 반환."""
    client = get_client()
    claims = claims_of(d)
    original = {_norm_domain(s["url"]) for s in d.get("sources", [])}
    blocked = settings.get("blocked_domains", [])
    block = sorted(set(blocked) | original | {base_domain(x) for x in original})
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8, "blocked_domains": block},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 6, "blocked_domains": block}]
    messages = [{"role": "user", "content": research_prompt(d, claims, original)}]
    content, n_s, n_f = [], 0, 0
    usage = {"input": 0, "output": 0}
    progress("원래 출처가 아닌 다른 자료를 찾는 중…")
    for _ in range(6):
        with client.beta.messages.stream(
            model=MODEL, max_tokens=32000, betas=[FALLBACK_BETA], fallbacks="default",
            output_config={"effort": "high"}, tools=tools, messages=messages,
        ) as stream:
            for ev in stream:
                if ev.type == "content_block_start":
                    t = ev.content_block.type
                    if t == "web_search_tool_result":
                        n_s += 1
                    elif t == "web_fetch_tool_result":
                        n_f += 1
                    elif t == "text":
                        progress(f"다른 출처 검색 {n_s}회·원문 {n_f}건 확인 — 문장별 비교 중…")
                        continue
                    else:
                        continue
                    progress(f"다른 출처 검색 {n_s}회 · 원문 {n_f}건 읽는 중…")
            resp = stream.get_final_message()
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        content.extend(resp.content)
        if resp.stop_reason == "refusal":
            raise RuntimeError("교차 검증 중 안전 거절이 발생했습니다.")
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    notes = "\n".join(b.text for b in content if b.type == "text").strip()
    seen = {_norm_domain(u) for u in _collect_urls(content)}

    progress("판정을 정리하고 틀린 문장을 고치는 중…")
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=16000, betas=[FALLBACK_BETA], fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": structure_prompt(notes, claims)}],
    )
    usage["input"] += resp.usage.input_tokens
    usage["output"] += resp.usage.output_tokens
    if resp.stop_reason == "refusal":
        raise RuntimeError("교차 검증 정리 중 안전 거절이 발생했습니다.")
    results = json.loads(next(b.text for b in resp.content if b.type == "text"))["results"]
    fixed, records = apply_results(d, results, seen, original, blocked)
    fixed["crosscheck"] = {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "records": records,
                           "searches": n_s, "fetches": n_f, "notes": notes, "usage": usage}
    return fixed
