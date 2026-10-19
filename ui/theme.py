"""화면 테마: 시안 A '네이비 콘솔' (2026-10-19 확정).

색·글꼴·사이드바 기본값은 .streamlit/config.toml 에 두고, 그 설정으로 안 되는 모양(사이드바 메뉴,
카테고리 알약 버튼, 카드, 채팅 말풍선)만 여기 CSS로 맞춘다. 업무 로직과 무관하며 화면 모양만 바꾼다.
CSS 선택자는 Streamlit이 붙이는 data-testid와 key 클래스(st-key-<key>)를 쓴다.
"""
from html import escape

import streamlit as st

NAVY, BLUE, MUTED = "#0F1B33", "#1D4ED8", "#5B6780"

# 메뉴 값(화면 이름)은 그대로 두고 표시만 바꾼다. 아이콘은 Streamlit Material 아이콘.
NAV_ICONS = {
    "홈": ":material/forum:", "인력 관리": ":material/person:", "투입 관리": ":material/work:",
    "근무 환경 준비": ":material/task_alt:", "구매계약 관리": ":material/description:",
    "계약 종료 알림": ":material/notifications:", "변경 이력": ":material/history:",
}
NAV_LABELS = {"홈": "홈 · AI Agent"}
PAGES = list(NAV_ICONS)

_CSS = """
<style>
/* 기본 머리말 띠를 배경과 맞춘다 */
header[data-testid="stHeader"] { background: transparent; }
[data-testid="stMainBlockContainer"] { padding-top: 2.2rem; }

/* ---- 사이드바: 네이비 메뉴 (버튼 nav_0~nav_6) ---- */
[data-testid="stSidebar"] [class*="st-key-nav_"] { margin-bottom: -10px; }
[data-testid="stSidebar"] [class*="st-key-nav_"] button {
  justify-content: flex-start; gap: 10px; min-height: 46px; padding: 10px 14px;
  background: transparent; border: none; border-radius: 8px; color: #C7D0E0; box-shadow: none;
}
[data-testid="stSidebar"] [class*="st-key-nav_"] button > div { justify-content: flex-start; }
[data-testid="stSidebar"] [class*="st-key-nav_"] button p,
[data-testid="stSidebar"] [class*="st-key-nav_"] button span { font-size: 16px; }
[data-testid="stSidebar"] [class*="st-key-nav_"] button:hover { background: #17264A; color: #FFFFFF; }
[data-testid="stSidebar"] [class*="st-key-nav_"] button:focus:not(:focus-visible) { box-shadow: none; }
[data-testid="stSidebar"] [class*="st-key-nav_"] button[kind="primary"],
[data-testid="stSidebar"] [class*="st-key-nav_"] button[data-testid="stBaseButton-primary"] {
  background: #1E2E52; color: #FFFFFF; font-weight: 600;
}
[data-testid="stSidebar"] [class*="st-key-nav_"] button[data-testid="stBaseButton-primary"] p { font-weight: 600; }
.pmo-brand { display: flex; align-items: center; gap: 10px; padding: 4px 4px 14px; }
.pmo-brand-mark { width: 34px; height: 34px; border-radius: 8px; background: #3B82F6;
  display: flex; align-items: center; justify-content: center; flex: none; }
.pmo-brand-title { color: #FFFFFF; font-weight: 700; font-size: 15px; line-height: 1.3; }
.pmo-brand-sub { color: #8C9AB5; font-size: 12px; }
.pmo-side-foot { border-top: 1px solid #24365C; margin-top: 8px; padding-top: 12px;
  font-size: 12px; color: #8C9AB5; line-height: 1.8; }
.pmo-dot { display: inline-block; width: 8px; height: 8px; border-radius: 4px; background: #34D399;
  margin-right: 6px; vertical-align: middle; }

/* ---- 화면 머리말 ---- */
.pmo-date { font-size: 13px; color: #5B6780; }
.pmo-h1 { font-size: 26px; font-weight: 700; margin: 2px 0 14px; color: #172033; }

/* ---- 홈: 숫자 카드 ---- */
.pmo-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 8px; }
@media (max-width: 900px) { .pmo-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.pmo-kpi { background: #FFFFFF; border: 1px solid #E1E6EF; border-radius: 12px; padding: 14px 18px; }
.pmo-kpi-label { font-size: 13px; color: #5B6780; }
.pmo-kpi-value { font-size: 28px; font-weight: 700; color: #172033; margin-top: 2px; }
.pmo-kpi-value.warn { color: #B45309; }
.pmo-kpi-unit { font-size: 14px; color: #5B6780; font-weight: 500; margin-left: 2px; }

/* ---- 홈: 흰 카드 패널 ---- */
.st-key-home_chat_panel, .st-key-home_result_panel {
  background: #FFFFFF; border: 1px solid #E1E6EF; border-radius: 14px; padding: 20px;
}

/* ---- 업무 카테고리: 알약 버튼 ---- */
.st-key-home_agent [role="radiogroup"] { gap: 6px; flex-wrap: wrap; }
.st-key-home_agent label[data-baseweb="radio"] {
  margin: 0; padding: 5px 13px; border: 1px solid #D5DCE8; border-radius: 16px; background: #FFFFFF;
}
.st-key-home_agent label[data-baseweb="radio"] > div:first-child { display: none; }
.st-key-home_agent label[data-baseweb="radio"]:has(input:checked) {
  background: #1D4ED8; border-color: #1D4ED8;
}
.st-key-home_agent label[data-baseweb="radio"]:has(input:checked) * { color: #FFFFFF; font-weight: 600; }

/* ---- 채팅 말풍선 ---- */
[data-testid="stChatMessage"] { border-radius: 14px; padding: 10px 14px; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) { background: #E8EEFB; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) { background: #F1F4F9; }

/* ---- 확인 카드 (저장 전) ---- */
div[class*="st-key-act"][class*="_card"] {
  background: #FFFBF2; border: 1px solid #F2C46B; border-radius: 12px; padding: 16px;
}
</style>
"""


def apply() -> None:
    """모든 화면에 한 번 넣는다."""
    st.markdown(_CSS, unsafe_allow_html=True)


def nav_label(page: str, alerts: int = 0) -> str:
    """메뉴 버튼 글자(아이콘은 버튼의 icon으로 따로 넣는다)."""
    label = NAV_LABELS.get(page, page)
    if page == "계약 종료 알림" and alerts:
        label += f" :orange-badge[{alerts}]"
    return label


def _go(page: str) -> None:
    st.session_state["nav_page"] = page


def sidebar_nav(alerts: int = 0) -> str:
    """시안 A의 왼쪽 메뉴: 라디오 대신 메뉴 버튼(key nav_0~nav_6). 지금 화면은 primary 버튼으로 강조한다."""
    current = st.session_state.setdefault("nav_page", PAGES[0])
    for i, page in enumerate(PAGES):
        st.sidebar.button(nav_label(page, alerts), key=f"nav_{i}", icon=NAV_ICONS[page],
                          type="primary" if page == current else "secondary", width="stretch",
                          on_click=_go, args=(page,))
    return st.session_state["nav_page"]


def sidebar_brand() -> None:
    st.sidebar.markdown(
        '<div class="pmo-brand"><div class="pmo-brand-mark">'
        '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#FFFFFF" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3"></circle>'
        '<path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"></path><path d="M16 4.5a3 3 0 0 1 0 6"></path>'
        '<path d="M18 14c2 .8 3 2.8 3 6"></path></svg></div>'
        '<div><div class="pmo-brand-title">CoE 인력관리</div><div class="pmo-brand-sub">PMO Agent</div></div></div>',
        unsafe_allow_html=True)


def sidebar_footer(db_name: str, people: int, model: str) -> None:
    st.sidebar.markdown(
        f'<div class="pmo-side-foot">연결된 DB · {escape(db_name)} · 인력 {people}명<br>'
        f'<span class="pmo-dot"></span>로컬 LLM {escape(model)} · PC 밖 전송 없음</div>',
        unsafe_allow_html=True)


_WEEKDAYS = "월화수목금토일"


def page_header(day, title: str) -> None:
    st.markdown(
        f'<div class="pmo-date">{day.year}년 {day.month}월 {day.day}일 ({_WEEKDAYS[day.weekday()]}) · Asia/Seoul</div>'
        f'<div class="pmo-h1">{escape(title)}</div>', unsafe_allow_html=True)


def kpi_row(items: list[tuple[str, int, str, bool]]) -> None:
    """[(이름, 값, 단위, 강조 여부)] 숫자 카드 한 줄."""
    cards = "".join(
        f'<div class="pmo-kpi"><div class="pmo-kpi-label">{escape(label)}</div>'
        f'<div class="pmo-kpi-value{" warn" if warn else ""}">{value}'
        f'<span class="pmo-kpi-unit">{escape(unit)}</span></div></div>'
        for label, value, unit, warn in items)
    st.markdown(f'<div class="pmo-kpis">{cards}</div>', unsafe_allow_html=True)
