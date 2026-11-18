"""Claude API 연결 테스트 (PRD 0단계). 키 값은 출력하지 않는다.

키는 ANTHROPIC_API_KEY 환경 변수에서 읽는다. 현재 프로세스에 없으면
Windows 사용자 환경 변수(HKCU\\Environment)에서 읽는다.
"""
import os
import sys

import anthropic

MODEL = "claude-opus-5-5"


def load_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")[0]
        except OSError:
            return None
    return None


def main():
    key = load_key()
    if not key:
        sys.exit("실패: ANTHROPIC_API_KEY가 등록되어 있지 않습니다.")
    client = anthropic.Anthropic(api_key=key)
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=200,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "연결 테스트입니다. '연결 성공'이라고만 답하세요."}],
        )
    except anthropic.AuthenticationError:
        sys.exit("실패(401): 키가 올바르지 않습니다. Console에서 발급한 API 키(sk-ant-api…)인지 확인하세요.")
    except anthropic.PermissionDeniedError:
        sys.exit("실패(403): 이 키에는 API 사용 권한이 없습니다.")
    except anthropic.NotFoundError:
        sys.exit(f"실패(404): 모델 {MODEL}을 찾을 수 없습니다.")
    except anthropic.RateLimitError:
        sys.exit("실패(429): 사용량 한도 초과 또는 크레딧 부족일 수 있습니다. Billing을 확인하세요.")
    except anthropic.BadRequestError as e:
        sys.exit(f"실패(400): {e.message}")
    except anthropic.APIStatusError as e:
        sys.exit(f"실패({e.status_code}): 서버 오류입니다. 잠시 후 다시 시도하세요.")
    except anthropic.APIConnectionError:
        sys.exit("실패: 네트워크 연결 오류입니다. 인터넷·방화벽을 확인하세요.")

    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    print("성공")
    print("  모델:", msg.model)
    print("  응답:", text)
    print("  종료 사유:", msg.stop_reason)
    print("  사용 토큰: 입력", msg.usage.input_tokens, "/ 출력", msg.usage.output_tokens)


if __name__ == "__main__":
    main()
