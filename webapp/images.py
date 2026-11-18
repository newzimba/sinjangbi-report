"""F4 사진: 공식 출처 페이지에서 사진 후보를 모으고, AI가 '장비 사진'인지 확인해 가장 알맞은 사진을 고른다.

1) 수집: 각 출처 페이지의 대표 이미지(og:image)와 본문 이미지(<img>)를 모은다. 로고·아이콘·작은 이미지는 제외.
2) 판별: judge(후보 목록, 장비명, 설명) → 후보별 {shows_equipment, score, what}. (photo_check.judge_images)
3) 선택: 장비가 보이는 사진 중 점수가 가장 높은 것. 없으면 사진 없음 + 제외 사유 기록.
"""
import re
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 5 * 1024 * 1024
JUDGE_MAX_BYTES = 3_500_000      # AI 판별에 보내는 이미지 1장 최대 크기
MIN_BYTES = 12_000               # 이보다 작으면 아이콘·로고로 보고 제외
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SinjangbiReport/1.0"
META_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image(?::secure_url)?|twitter:image)["'][^>]*>""", re.I)
CONTENT_RE = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.I)
IMG_RE = re.compile(r"""<img\b[^>]*?\b(?:data-src|src)\s*=\s*["']([^"']+)["']""", re.I)
SKIP_RE = re.compile(r"logo|icon|sprite|avatar|favicon|badge|banner|author|profile|placeholder|pixel|spacer|\.svg",
                     re.I)
MEDIA = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif"}


def image_kind(data: bytes):
    """파일 앞부분으로 형식을 판별한다. 워드에 넣을 수 있는 형식만 인정 (WebP·SVG 제외)."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def validate_upload(data: bytes):
    """업로드 검증. (확장자, 오류 메시지) 반환."""
    if not data:
        return None, "파일이 비어 있습니다."
    if len(data) > MAX_BYTES:
        return None, "5MB 이하 이미지만 올릴 수 있습니다."
    kind = image_kind(data)
    if not kind:
        return None, "이미지 파일이 아닙니다. JPG·PNG·GIF만 올릴 수 있습니다."
    return kind, None


def find_og_image(html: str, page_url: str):
    for tag in META_RE.findall(html):
        m = CONTENT_RE.search(tag)
        if m:
            return urljoin(page_url, m.group(1).strip())
    return None


def image_urls_in_page(html: str, page_url: str, limit=4):
    """대표 이미지 + 본문 이미지 주소 (로고·아이콘 제외, 중복 제거, 최대 limit개)."""
    urls = []
    og = find_og_image(html, page_url)
    if og:
        urls.append(og)
    for src in IMG_RE.findall(html):
        src = src.strip()
        if src.startswith("data:") or SKIP_RE.search(src):
            continue
        urls.append(urljoin(page_url, src))
    return list(dict.fromkeys(urls))[:limit]


def collect_candidates(page_urls, client, per_page=4, max_total=8):
    """출처 페이지들에서 쓸 수 있는 사진 후보를 내려받는다. [{data, kind, url, page_url}]"""
    out, seen = [], set()
    for page in page_urls:
        try:
            r = client.get(page)
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                continue
            for u in image_urls_in_page(r.text[:500_000], str(r.url), per_page):
                if u in seen or len(out) >= max_total:
                    continue
                seen.add(u)
                try:
                    ir = client.get(u)
                except httpx.HTTPError:
                    continue
                data = ir.content
                kind = image_kind(data) if ir.status_code == 200 else None
                if kind and MIN_BYTES <= len(data) <= MAX_BYTES:
                    out.append({"data": data, "kind": kind, "url": u, "page_url": page})
        except httpx.HTTPError:
            continue
        if len(out) >= max_total:
            break
    return out


def credit_for(page_url):
    host = urlparse(page_url).netloc.removeprefix("www.")
    return f"출처: {host}, {page_url}"


PHOTO_COUNT = 2  # 1p에 넣는 장비 사진 수


def find_equipment_images(page_urls, equipment, description, save_stem, judge, client=None, count=PHOTO_COUNT):
    """사진 후보를 모아 AI로 판별한 뒤 서로 다른 장비 사진을 최대 count장 저장한다.

    반환: {"photos": [{"file", "page_url", "image_url", "credit", "check": {"status", "what"}}],
           "rejected": [{"url", "what"}], "note": 화면 안내 문구}
    """
    own = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": UA})
    try:
        cands = collect_candidates(page_urls, client)
    finally:
        if own:
            client.close()
    if not cands:
        return {"photos": [], "rejected": [], "note": "출처 페이지에서 쓸 수 있는 사진을 찾지 못함"}

    judgeable = [c for c in cands if len(c["data"]) <= JUDGE_MAX_BYTES]
    try:
        verdicts = judge(judgeable, equipment, description) if judgeable else []
    except Exception as e:  # 판별 실패 → 확인되지 않은 사진은 임의로 넣지 않는다 (2026-11-18 사용자 요청)
        return {"photos": [], "rejected": [],
                "note": f"AI 사진 확인 실패({type(e).__name__}) — 확인되지 않은 사진은 넣지 않음. [사진 다시 찾기] 또는 직접 올리기"}

    by_index = {v["index"]: v for v in verdicts if 0 <= v.get("index", -1) < len(judgeable)}
    rejected, good = [], []
    for i, c in enumerate(judgeable):
        v = by_index.get(i, {"shows_equipment": False, "score": 0, "what": "판별 결과 없음", "duplicate_of": -1})
        if v["shows_equipment"]:
            good.append((i, v, c))
        else:
            rejected.append({"url": c["url"], "what": v["what"]})

    chosen = []
    for i, v, c in sorted(good, key=lambda t: -t[1]["score"]):
        dup = v.get("duplicate_of", -1)
        if any(dup == j or by_index.get(j, {}).get("duplicate_of", -1) == i for j, _, _ in chosen):
            rejected.append({"url": c["url"], "what": f"이미 고른 사진과 같은 장면 — {v['what']}"})
            continue
        if len(chosen) < count:
            chosen.append((i, v, c))
    photos = [{**save(c, save_stem, n), "check": {"status": "ok", "what": v["what"]}}
              for n, (_, v, c) in enumerate(chosen)]
    if not photos:
        note = f"사진 후보 {len(judgeable)}장 모두 장비 사진이 아님"
    elif len(photos) < count:
        note = f"장비 사진을 {len(photos)}장만 찾음 — 나머지는 직접 올려 주세요"
    else:
        note = ""
    return {"photos": photos, "rejected": rejected, "note": note}


def save(cand, save_stem, n=0):
    path = save_stem.with_name(f"{save_stem.name}_{n}").with_suffix("." + cand["kind"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cand["data"])
    return {"file": str(path), "page_url": cand["page_url"], "image_url": cand["url"],
            "credit": credit_for(cand["page_url"])}
