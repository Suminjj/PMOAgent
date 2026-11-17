"""Agent 처리 과정을 콘솔(앱을 실행한 터미널)에 한 줄씩 남긴다 — 시연·디버깅용(11/16 담당자: 시연은 콘솔 로그 중심).

PMO_TRACE=1 일 때만 출력한다(기본 꺼짐). 화면에는 보이지 않고 앱을 띄운 PowerShell 창에만 나온다.
도구 입력에 인력 이름이 들어가므로 실제 데이터 DB에서는 켜지 않는다(로컬 창이라 외부 전송은 없음).

    10:31:02.114  USER          전표작성 자동화 류협력을 12월 1일부터 노협력으로 교체해줘.
    10:31:02.116  ROUTER        키워드 → 인력관리 Agent  (점수 인력관리 1 · 구매계약 0 · 검수 0)  1ms
    10:31:02.116  PLANNING      실행 계획 1단계
                                  1) 인력관리 Agent ← 「전표작성 자동화 류협력을 …」 · 도구 10개
    10:31:03.402  LLM           #1 → 도구 2개 요청: find_people, find_people  1,285ms · 토큰 8,812+64
    10:31:03.415  TOOL CALL     find_people(name=류협력) → 성공 1건  11ms
    10:31:04.902  SIMULATE      인력 교체 · 변경 4건 계획 생성(DB에 쓰지 않음) · 업무 규칙 검증 통과 → 확인 카드(사람 승인 대기)
    10:31:05.711  VERIFY        답변 검증 4항목 → 통과

11/16 시연 가이드(Planning·Self-Correction·Tool Call 로그): 태그 이름만 바꾼 것이 아니라 실제로 하는 일을 보인다.
PLANNING = 라우터가 만든 실행 계획(LLM 분류면 규칙 후보와 비교), SIMULATE = 저장 없이 만든 변경 계획과 검증,
SELF-CORRECT = 코드가 잘못된 입력·답변을 잡아 바로잡거나 LLM에 다시 시킨 일, VERIFY = 답변 검사 결과.
"""
import os
import sys
import threading
import time
from datetime import datetime

COLORS = {"USER": "97", "ROUTER": "95", "PLANNING": "95", "AGENT": "96", "LLM": "94", "TOOL CALL": "92",
          "GUARD": "93", "SIMULATE": "96", "SELF-CORRECT": "93", "VERIFY": "92", "SAVE": "92", "REQUEST": "96", "DONE": "97", "GRAPH": "95",
          "ERROR": "91"}
WIDTH = 12
_ready = False
_state = threading.local()       # Streamlit은 세션마다 스레드가 다르다: 바로잡은 횟수를 스레드별로 센다


def reset_corrections() -> None:
    _state.corrections = 0


def corrections() -> int:
    return getattr(_state, "corrections", 0)


def enabled() -> bool:
    return os.environ.get("PMO_TRACE", "").strip().lower() in ("1", "true", "on", "yes")


def _color() -> bool:
    return sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def log(tag: str, message: str) -> None:
    """한 줄 출력. 꺼져 있으면 아무것도 하지 않는다. 출력 실패가 Agent 처리를 막지 않게 한다."""
    global _ready
    if tag == "SELF-CORRECT":
        _state.corrections = corrections() + 1
    if not enabled():
        return
    try:
        if not _ready and os.name == "nt":
            os.system("")            # Windows 콘솔에서 ANSI 색을 켠다
        _ready = True
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        head = f"{tag:<{WIDTH}}"
        if _color():
            head = f"\033[{COLORS.get(tag, '0')}m{head}\033[0m"
        print(f"{stamp}  {head} {message}", file=sys.stderr, flush=True)
    except Exception:       # noqa: BLE001 — 로그 때문에 답변이 실패하면 안 된다
        pass


def sub(lines) -> None:
    """앞 줄에 딸린 세부 줄(계획 단계 등). 시각·태그 자리를 비워 들여 쓴다."""
    if not enabled():
        return
    try:
        for line in lines:
            print(" " * (14 + WIDTH + 1) + "  " + str(line), file=sys.stderr, flush=True)
    except Exception:       # noqa: BLE001
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
