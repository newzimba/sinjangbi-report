"""Claude API 클라이언트 생성. 키는 환경 변수에서만 읽고 출력하지 않는다."""
import os
import sys

import anthropic

MODEL = "claude-opus-5-5"
# 안전 거절 시 서버가 대체 모델로 자동 재시도 (Opus 5.5 권장 설정)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def load_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if sys.platform == "win32":  # 앱을 다시 켜기 전이면 사용자 환경 변수에서 직접 읽는다
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")[0]
        except OSError:
            return None
    return None


def get_client():
    key = load_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY가 등록되어 있지 않습니다.")
    return anthropic.Anthropic(api_key=key)


def friendly_error(e: Exception) -> str:
    """API 오류를 화면용 한국어 문장으로 바꾼다 (구체적인 오류부터 확인)."""
    if isinstance(e, anthropic.AuthenticationError):
        return "API 키가 올바르지 않습니다(401). 키를 확인하세요."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "이 API 키에는 사용 권한이 없습니다(403)."
    if isinstance(e, anthropic.RateLimitError):
        return "사용량 한도 초과 또는 크레딧 부족입니다(429). 잠시 후 다시 시도하거나 Billing을 확인하세요."
    if isinstance(e, anthropic.BadRequestError):
        return f"요청 형식 오류입니다(400): {e.message}"
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude 서버 오류입니다({e.status_code}). 잠시 후 다시 시도하세요."
    if isinstance(e, anthropic.APIConnectionError):
        return "네트워크 연결 오류입니다. 인터넷 연결을 확인하세요."
    return f"오류: {e}"
