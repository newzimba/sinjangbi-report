"""AI 사진 판별: 사진마다 '보고 대상 장비가 실제로 보이는지'를 Claude가 이미지를 보고 판단한다."""
import base64
import json

from .claude_client import FALLBACK_BETA, MODEL, get_client
from .images import MEDIA

SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "shows_equipment": {"type": "boolean"},
            "score": {"type": "integer"},
            "what": {"type": "string"},
            "duplicate_of": {"type": "integer"},
        },
        "required": ["index", "shows_equipment", "score", "what", "duplicate_of"], "additionalProperties": False}}},
    "required": ["results"], "additionalProperties": False,
}


def judge_images(cands, equipment, description):
    """cands: [{data, kind}] → [{index, shows_equipment, score(0~10), what}]"""
    content = []
    for i, c in enumerate(cands):
        content.append({"type": "text", "text": f"[사진 {i}]"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": MEDIA[c["kind"]],
            "data": base64.standard_b64encode(c["data"]).decode("ascii")}})
    content.append({"type": "text", "text": f"""위 사진 {len(cands)}장은 기관장 보고용 1p에 넣을 '{equipment}' 사진 후보다.
장비 설명: {description}

사진마다 판단하라:
- shows_equipment: 이 장비(또는 장비 시제품·작동 장면)가 사진의 주된 대상으로 분명히 보이면 true.
  사람 단체 사진·인물 사진·건물·로고·행사장·도표만 있는 사진·다른 장비 사진이면 false.
- score: 보고서 사진으로 알맞은 정도 0~10 (장비 전체가 잘 보이고 작동 장면이면 높게).
- what: 사진에 실제로 보이는 것을 한국어 한 문장으로 (예: "연구원 4명이 서 있는 단체 사진", "교량 내부를 비행하는 소형 드론").
- duplicate_of: 앞 번호 사진과 사실상 같은 장면(같은 사진의 크기·자르기만 다른 것)이면 그 사진 번호, 아니면 -1.
  보고서에는 서로 다른 장면 2장을 넣으므로, 같은 장면이 겹치지 않게 정확히 표시한다.
index는 [사진 n]의 n이다. 모든 사진에 대해 답한다."""})
    resp = get_client().beta.messages.create(
        model=MODEL, max_tokens=4000, betas=[FALLBACK_BETA], fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("사진 판별 중 안전 거절")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))["results"]
