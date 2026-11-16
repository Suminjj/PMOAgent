"""Agent 처리 과정을 콘솔(앱을 실행한 터미널)에 한 줄씩 남긴다 — 시연·디버깅용(11/16 담당자: 시연은 콘솔 로그 중심).

PMO_TRACE=1 일 때만 출력한다(기본 꺼짐). 화면에는 보이지 않고 앱을 띄운 PowerShell 창에만 나온다.
도구 입력에 인력 이름이 들어가므로 실제 데이터 DB에서는 켜지 않는다(로컬 창이라 외부 전송은 없음).

    10:31:02.114  USER    전표작성 자동화 류협력을 12월 1일부터 노협력(L6)으로 교체해줘.
    10:31:02.116  ROUTER  keyword → 인력관리 Agent  (점수 인력관리 3 · 구매계약 0 · 검수 0)
    10:31:02.117  AGENT   인력관리 Agent 시작 · 도구 10개 · gpt-4.1
    10:31:03.402  LLM     #1 → 도구 2개 요청  1,285ms · 토큰 8,812+64
    10:31:03.415  TOOL    find_people(name=류협력) → 성공 1건  11ms
"""
import os
import sys
import time
from datetime import datetime

COLORS = {"USER": "97", "ROUTER": "95", "AGENT": "96", "LLM": "94", "TOOL": "92", "GUARD": "93",
          "PLAN": "96", "CHECK": "93", "SAVE": "92", "DONE": "97", "ERROR": "91"}
_ready = False


def enabled() -> bool:
    return os.environ.get("PMO_TRACE", "").strip().lower() in ("1", "true", "on", "yes")


def _color() -> bool:
    return sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def log(tag: str, message: str) -> None:
    """한 줄 출력. 꺼져 있으면 아무것도 하지 않는다. 출력 실패가 Agent 처리를 막지 않게 한다."""
    global _ready
    if not enabled():
        return
    try:
        if not _ready and os.name == "nt":
            os.system("")            # Windows 콘솔에서 ANSI 색을 켠다
        _ready = True
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        head = f"{tag:<7}"
        if _color():
            head = f"\033[{COLORS.get(tag, '0')}m{head}\033[0m"
        print(f"{stamp}  {head} {message}", file=sys.stderr, flush=True)
    except Exception:       # noqa: BLE001 — 로그 때문에 답변이 실패하면 안 된다
        pass


def short(value, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def args_text(args: dict | None) -> str:
    return ", ".join(f"{k}={short(v, 30)}" for k, v in (args or {}).items() if v not in (None, "", [], {}))


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return "실패: " + short(result.get("error") or "", 70)
    data = result.get("data")
    if isinstance(data, list):
        return f"성공 {len(data)}건"
    if isinstance(data, dict) and data.get("plan"):
        return "성공 · 변경 계획"
    return "성공"


class Timer:
    def __init__(self):
        self.t0 = time.perf_counter()

    @property
    def ms(self) -> int:
        return round((time.perf_counter() - self.t0) * 1000)
