"""화면 테마: 시안 A '네이비 콘솔' (2026-10-19 확정).

색·글꼴·사이드바 기본값은 .streamlit/config.toml 에 두고, 그 설정으로 안 되는 모양(사이드바 메뉴,
카테고리 알약 버튼, 카드, 채팅 말풍선)만 여기 CSS로 맞춘다. 업무 로직과 무관하며 화면 모양만 바꾼다.
CSS 선택자는 Streamlit이 붙이는 data-testid와 key 클래스(st-key-<key>)를 쓴다.
"""
from html import escape

import streamlit as st

NAVY, BLUE, MUTED = "#0F1B33", "#1D4ED8", "#5B6780"

# 왼쪽 메뉴 (10/25 담당자: 인력 관리·구매계약 관리를 하위 메뉴로 세분화).
# (그룹 이름, 아이콘, 하위 화면 목록) — 하위 화면이 없으면 그룹 이름이 곧 화면 이름이다.
NAV_GROUPS = [
    ("홈", ":material/forum:", []),
    ("인력 관리", ":material/person:", ["전체 인력 조회", "신규 인력 등록", "과제 투입 관리", "수정 요청"]),  # 11/09 수정 요청을 인력 관리 아래로
    ("구매계약 관리", ":material/description:", ["전체 계약 조회", "계약 등록·수정"]),
    ("검수 관리", ":material/payments:", ["검수하기"]),
    ("계약 종료 알림", ":material/notifications:", []),
    ("변경 이력", ":material/history:", []),
    ("사용자 관리", ":material/manage_accounts:", []),     # v2: PMO·PM
]
# v2 역할별 메뉴 (11/03 담당자): 과제 리더는 조회 메뉴와 수정 요청만. 등록·수정 화면과 변경 이력·사용자 관리는 숨긴다.
LEADER_HIDDEN = {"신규 인력 등록", "계약 등록·수정", "검수하기", "변경 이력", "사용자 관리"}  # 검수: 단가 권한(v3)
SUB_ICONS = {
    "전체 인력 조회": ":material/table_view:", "신규 인력 등록": ":material/person_add:",
    "과제 투입 관리": ":material/work:", "전체 계약 조회": ":material/table_view:",
    "계약 등록·수정": ":material/edit_note:", "검수하기": ":material/fact_check:",
    "수정 요청": ":material/rate_review:",
}
NAV_ICONS = {}
GROUP_OF = {}
for _group, _icon, _subs in NAV_GROUPS:
    for _page in _subs or [_group]:
        NAV_ICONS[_page] = SUB_ICONS.get(_page, _icon)
        GROUP_OF[_page] = _group
NAV_LABELS = {"홈": "홈 · PMO Agent"}
# 홈 화면 업무 카테고리 버튼의 이모티콘 (10/20 담당자). 키는 pmo/agent/agents.py의 AGENTS 키.
AGENT_ICONS = {"auto": "🤖", "staff": "👤", "contract": "📄", "inspection": "💰"}


def agent_label(key: str, label: str) -> str:
    return f"{AGENT_ICONS.get(key, '')} {label}".strip()
PAGES = list(NAV_ICONS)

_CSS = """
<style>
/* 기본 머리말 띠를 배경과 맞춘다 */
header[data-testid="stHeader"] { background: transparent; }
[data-testid="stMainBlockContainer"] { padding-top: 2.2rem; }

/* ---- 사이드바: 네이비 메뉴 (버튼 nav_0~nav_9, 하위 메뉴는 navgroup_* 안에서 들여쓰기) ---- */
.pmo-nav-group { display: flex; align-items: center; gap: 10px; color: #8C9AB5; font-size: 13px;
  font-weight: 600; padding: 14px 14px 2px; letter-spacing: 0.02em; }
[data-testid="stSidebar"] [class*="st-key-navgroup_"] [class*="st-key-nav_"] button {
  min-height: 40px; padding: 8px 14px 8px 30px; }
[data-testid="stSidebar"] [class*="st-key-navgroup_"] [class*="st-key-nav_"] button p { font-size: 15px; }
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
.pmo-kpis.cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.st-key-home_mm_calc { background: #FFFFFF; }
.st-key-home_mm_calc .pmo-kpi { margin-top: 4px; }
.pmo-kpis.cols-1 { grid-template-columns: minmax(0, 1fr); margin-bottom: 0; }   /* 홈 왼쪽 알림 카드 (11/09) */
@media (max-width: 900px) { .pmo-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.pmo-kpi { background: #FFFFFF; border: 1px solid #E1E6EF; border-radius: 12px; padding: 14px 18px; }
.pmo-kpi-label { font-size: 13px; color: #5B6780; }
.pmo-kpi-value { font-size: 28px; font-weight: 700; color: #172033; margin-top: 2px; }
.pmo-kpi-value.warn { color: #B45309; }
.pmo-kpi-unit { font-size: 14px; color: #5B6780; font-weight: 500; margin-left: 2px; }
/* 홈 왼쪽 '오늘의 업무' 제목 + 알림 카드 아래 간단 목록 (11/09) */
.pmo-side-title { font-size: 17px; font-weight: 700; color: #172033; margin: 2px 0 2px; }
.pmo-mini { list-style: none; margin: 10px 0 0; padding: 0; border-top: 1px solid #EEF1F6; }
.pmo-mini li { padding: 7px 0; border-bottom: 1px solid #EEF1F6; line-height: 1.35; }
.pmo-mini li:last-child { border-bottom: none; }
.pmo-mini-title { font-size: 13.5px; font-weight: 600; color: #172033; }
.pmo-mini-sub { font-size: 12px; color: #5B6780; }
.pmo-mini-tag { float: right; font-size: 12px; font-weight: 600; color: #B45309; }
.pmo-mini-more, .pmo-mini-empty { font-size: 12px; color: #8A94A6; padding-top: 6px; }

/* ---- 로그인·초기 설정 (11/09 담당자: 가운데 좁게 + AI Agent 그림) ---- */
.pmo-user { text-align: right; font-size: 13.5px; color: #172033; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.st-key-logout button { min-height: 34px; padding: 2px 10px; border-radius: 999px; }
.st-key-logout button p { font-size: 13px; }
.pmo-auth-hero { text-align: center; margin: 24px 0 14px; }
.pmo-auth-hero svg { display: inline-block; }
.pmo-auth-title { font-size: 24px; font-weight: 700; color: #172033; margin-top: 4px; }
.pmo-auth-sub { font-size: 14px; color: #5B6780; }
.st-key-auth_box [data-testid="stForm"] { background: #FFFFFF; border: 1px solid #E1E6EF; border-radius: 14px; padding: 20px; }
/* 11/09 담당자: 로그인 버튼은 폼 오른쪽 끝(app.py에서 오른쪽 칸에 둠) */
.st-key-auth_box [data-testid="stFormSubmitButton"] button {
  width: 100%; border-radius: 999px; border: none; background: linear-gradient(110deg, #1E2A78, #5B3FD1); color: #FFFFFF;
}
.st-key-auth_box [data-testid="stFormSubmitButton"] button p { color: #FFFFFF; font-weight: 600; }

/* ---- 홈: 흰 카드 패널 ---- */
.st-key-home_chat_panel {
  background: #FFFFFF; border: 1px solid #E3E7F2; border-radius: 18px; padding: 0 20px 20px;
  box-shadow: 0 10px 30px rgba(30, 41, 90, 0.14); overflow: hidden;
}
/* 11/09 담당자 시안 A: 그라데이션 머리띠(네이비 → 인디고 → 보라). 왼쪽 네이비 메뉴와 이어지고 AI 느낌을 준다. */
.st-key-home_agent_head {
  background: linear-gradient(110deg, #0F1B33 0%, #1E2A78 45%, #5B3FD1 100%);
  margin: 0 -20px 6px; padding: 18px 24px;
}
.pmo-agent-head { position: relative; color: #FFFFFF; }
.pmo-agent-title { display: flex; align-items: center; gap: 10px; font-size: 21px; font-weight: 700; }
.pmo-agent-spark { width: 34px; height: 34px; border-radius: 10px; background: rgba(255,255,255,.14);
  display: grid; place-items: center; font-size: 18px; }
.pmo-agent-sub { margin-top: 4px; font-size: 13px; color: #C7CCF5; }
.pmo-agent-live { position: absolute; right: 0; top: 2px; font-size: 12px; color: #FFFFFF;
  background: rgba(255,255,255,.14); padding: 5px 11px; border-radius: 999px; }
.pmo-agent-live::before { content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: #34D399; margin-right: 6px; box-shadow: 0 0 8px #34D399; vertical-align: 1px; }
/* 대화 영역: 테두리 없이, 답변 아이콘은 그라데이션 원 */
.st-key-home_chat_log { border: none !important; }
.st-key-home_chat_log [data-testid="stChatMessageAvatarAssistant"] {
  background: linear-gradient(135deg, #3B5BDB, #7C3AED); color: #FFFFFF; border-radius: 50%;
}
.st-key-home_chat_log [data-testid="stChatMessageAvatarAssistant"] svg { fill: #FFFFFF; color: #FFFFFF; }
.st-key-home_chat_log [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) { background: #F4F5FB; }
.st-key-home_chat_log [data-testid="stCaptionContainer"] p { color: #6B5BD6; }
/* 입력창: 둥근 알약 모양 + 그라데이션 보내기 버튼 */
.st-key-home_chat_panel [data-testid="stChatInput"] { border-radius: 999px; border: 1.5px solid #D9DDF0; background: #FFFFFF; }
.st-key-home_chat_panel [data-testid="stChatInput"] > div { border-radius: 999px; background: #FFFFFF; }
.st-key-home_chat_panel [data-testid="stChatInputSubmitButton"] {
  border-radius: 50%; background: linear-gradient(135deg, #3B5BDB, #7C3AED); color: #FFFFFF;
}
.st-key-home_chat_panel [data-testid="stChatInputSubmitButton"] svg { fill: #FFFFFF; color: #FFFFFF; }
/* 채팅창처럼: 사용자 질문은 오른쪽 말풍선, Agent 답변은 왼쪽 (11/08) */
.st-key-home_chat_log [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
  flex-direction: row-reverse; margin-left: 20%; border-radius: 14px;
  background: linear-gradient(110deg, #2442B8, #5B3FD1);
}
.st-key-home_chat_log [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) p { color: #FFFFFF; }
.st-key-home_chat_log [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] {
  text-align: right;
}

/* ---- 업무 카테고리: 큰 버튼 (key home_agent_<카테고리>) ---- */
.st-key-home_agent [data-testid="stHorizontalBlock"] { gap: 8px; }
.st-key-home_agent button {
  min-height: 42px; padding: 6px 14px; border: 1px solid #DCE1EE; border-radius: 999px;
  background: #FFFFFF; color: #172033; box-shadow: none;
}
.st-key-home_agent button p { font-size: 14px; line-height: 1.3; }
.st-key-home_agent button:hover { border-color: #5B3FD1; color: #5B3FD1; }
.st-key-home_agent button[data-testid="stBaseButton-primary"] {
  background: linear-gradient(110deg, #1E2A78, #5B3FD1); border-color: transparent; color: #FFFFFF;
}
.st-key-home_agent button[data-testid="stBaseButton-primary"] p { font-weight: 600; color: #FFFFFF; }

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


go = _go   # 화면의 '보기' 버튼에서 다른 메뉴로 이동할 때 쓴다


def pages_for(role: str) -> list[str]:
    return [p for p in PAGES if not (role == "과제 리더" and p in LEADER_HIDDEN)]


def sidebar_nav(alerts: int = 0, allowed: list[str] | None = None) -> str:
    """시안 A의 왼쪽 메뉴: 메뉴 버튼(key nav_0~nav_9). 지금 화면은 primary 버튼으로 강조한다.

    인력 관리·구매계약 관리는 그룹 제목 아래에 하위 메뉴 버튼을 들여 써서 보여준다.
    """
    allowed = allowed or PAGES
    if st.session_state.get("nav_page") not in allowed:   # 예전 메뉴 이름·권한 밖 메뉴는 홈으로
        st.session_state["nav_page"] = PAGES[0]
    current = st.session_state["nav_page"]

    def button(page: str) -> None:
        st.button(nav_label(page, alerts), key=f"nav_{PAGES.index(page)}", icon=NAV_ICONS[page],
                  type="primary" if page == current else "secondary", width="stretch",
                  on_click=_go, args=(page,))

    with st.sidebar:
        for index, (group, _icon, all_subs) in enumerate(NAV_GROUPS):
            subs = [p for p in all_subs if p in allowed]
            if not all_subs:
                if group in allowed:
                    button(group)
                continue
            if not subs:
                continue
            st.markdown(f'<div class="pmo-nav-group">{escape(group)}</div>', unsafe_allow_html=True)
            with st.container(key=f"navgroup_{index}"):
                for page in subs:
                    button(page)
    return current


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
    """날짜 + 화면 제목. 제목이 비면 날짜만(11/09 담당자: 홈의 '무엇을 도와드릴까요?' 삭제)."""
    st.markdown(
        f'<div class="pmo-date">{day.year}년 {day.month}월 {day.day}일 ({_WEEKDAYS[day.weekday()]}) · Asia/Seoul</div>'
        + (f'<div class="pmo-h1">{escape(title)}</div>' if title else ""), unsafe_allow_html=True)


def alert_card(label: str, count: int, unit: str, items: list[tuple[str, str, str]], *, empty: str,
               limit: int = 5) -> None:
    """홈 왼쪽 알림 카드(11/09 담당자): 건수 + 아래 간단 목록. items = [(제목, 보조 설명, 오른쪽 표시)]."""
    rows = "".join(
        f'<li><span class="pmo-mini-tag">{escape(tag)}</span><div class="pmo-mini-title">{escape(title)}</div>'
        f'<div class="pmo-mini-sub">{escape(sub)}</div></li>' for title, sub, tag in items[:limit])
    more = f'<div class="pmo-mini-more">외 {len(items) - limit}건</div>' if len(items) > limit else ""
    body = f'<ul class="pmo-mini">{rows}</ul>{more}' if items else f'<div class="pmo-mini-empty">{escape(empty)}</div>'
    st.markdown(f'<div class="pmo-kpi"><div class="pmo-kpi-label">{escape(label)}</div>'
                f'<div class="pmo-kpi-value{" warn" if count else ""}">{count}'
                f'<span class="pmo-kpi-unit">{escape(unit)}</span></div>{body}</div>', unsafe_allow_html=True)


# 11/09 담당자: 로그인 화면의 AI Agent 그림(직접 그린 SVG, 시안 A와 같은 네이비→보라 색)
AGENT_SVG = ('<svg width="168" height="150" viewBox="0 0 168 150" role="img" aria-label="PMO Agent" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="pmoBody" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1E2A78"/><stop offset="1" stop-color="#7C3AED"/></linearGradient><radialGradient id="pmoHalo"><stop offset="0" stop-color="#8B5CF6" stop-opacity=".35"/><stop offset="1" stop-color="#8B5CF6" stop-opacity="0"/></radialGradient></defs><circle cx="80" cy="78" r="72" fill="url(#pmoHalo)"/><line x1="80" y1="22" x2="80" y2="36" stroke="#5B3FD1" stroke-width="4" stroke-linecap="round"/><circle cx="80" cy="18" r="7" fill="#A78BFA"/><circle cx="80" cy="18" r="3" fill="#FFFFFF"/><rect x="28" y="62" width="10" height="26" rx="5" fill="#3B5BDB"/><rect x="122" y="62" width="10" height="26" rx="5" fill="#3B5BDB"/><rect x="36" y="36" width="88" height="78" rx="28" fill="url(#pmoBody)"/><rect x="48" y="52" width="64" height="38" rx="19" fill="#0F1B33"/><ellipse cx="66" cy="70" rx="6" ry="7.5" fill="#67E8F9"/><ellipse cx="94" cy="70" rx="6" ry="7.5" fill="#67E8F9"/><path d="M72 98 Q80 104 88 98" stroke="#E0E7FF" stroke-width="3" fill="none" stroke-linecap="round"/><path d="M140 24 l3.5 9 9 3.5 -9 3.5 -3.5 9 -3.5 -9 -9 -3.5 9 -3.5z" fill="#F0ABFC"/><path d="M22 112 l2.5 6 6 2.5 -6 2.5 -2.5 6 -2.5 -6 -6 -2.5 6 -2.5z" fill="#93C5FD"/><rect x="112" y="104" width="50" height="30" rx="12" fill="#FFFFFF" stroke="#DCE1EE"/><circle cx="126" cy="119" r="3.5" fill="#5B3FD1"/><circle cx="137" cy="119" r="3.5" fill="#7C3AED"/><circle cx="148" cy="119" r="3.5" fill="#A78BFA"/></svg>')


def auth_hero(subtitle: str) -> None:
    """로그인·초기 설정 화면 머리: AI Agent 그림 + 'PMO Agent' + 안내 한 줄."""
    st.markdown(f'<div class="pmo-auth-hero">{AGENT_SVG}<div class="pmo-auth-title">PMO Agent</div>'
                f'<div class="pmo-auth-sub">{escape(subtitle)}</div></div>', unsafe_allow_html=True)


def agent_header() -> None:
    """홈 PMO Agent 머리띠(11/09 담당자 시안 A). 배지는 실제 연결 확인이 아니라 '로컬 LLM' 사용 표시다."""
    st.markdown('<div class="pmo-agent-head"><div class="pmo-agent-title"><span class="pmo-agent-spark">✦</span>'
                'PMO Agent</div><span class="pmo-agent-live">로컬 LLM</span></div>', unsafe_allow_html=True)


def kpi_row(items: list[tuple[str, int, str, bool]], columns: int = 4) -> None:
    """[(이름, 값, 단위, 강조 여부)] 숫자 카드 한 줄."""
    cards = "".join(
        f'<div class="pmo-kpi"><div class="pmo-kpi-label">{escape(label)}</div>'
        f'<div class="pmo-kpi-value{" warn" if warn else ""}">{value}'
        f'<span class="pmo-kpi-unit">{escape(unit)}</span></div></div>'
        for label, value, unit, warn in items)
    st.markdown(f'<div class="pmo-kpis cols-{columns}">{cards}</div>', unsafe_allow_html=True)
