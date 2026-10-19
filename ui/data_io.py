"""'데이터 가져오기·내보내기' 화면: 양식 내려받기 → 파일 올리기 → 미리보기 → 확인 후 가져오기, 현재 데이터 내려받기.

파일은 PC 안에서만 처리한다. 가져오기는 오류가 없을 때만, 미리보기 뒤 '확인 후 가져오기'를 눌러야 저장한다.
"""
import hashlib
import sqlite3
from datetime import datetime
from uuid import uuid4

import streamlit as st

from pmo.excel_io import build_import, export_bytes, template_bytes
from pmo.errors import ConflictError, ValidationError

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def render(service, actor: str) -> None:
    st.caption(f"연결된 DB: {service.path.name}. 실제 데이터는 시연 DB(demo.db)가 아닌 업무 DB에 넣으세요.")

    st.subheader("1. 양식 내려받기")
    st.write("인력·투입·구매계약 시트에 한 줄에 한 건씩 채웁니다. 작성 방법은 파일의 '안내' 시트에 있습니다.")
    st.download_button("빈 양식 내려받기", template_bytes(), file_name="CoE인력관리_가져오기_양식.xlsx",
                       mime=XLSX, icon=":material/download:", key="io_template")

    st.subheader("2. 파일 올리기와 미리보기")
    uploaded = st.file_uploader("채운 양식(.xlsx)", type=["xlsx"], key="io_upload")
    if uploaded is not None:
        _preview_and_import(service, actor, uploaded.getvalue())

    st.subheader("3. 현재 데이터 내려받기")
    st.write("지금 DB의 인력·투입·구매계약을 같은 양식으로 내려받습니다. 인력키는 내부 관리번호입니다. "
             "이 파일을 그대로 다시 올리면 이미 있는 데이터는 건너뜁니다.")
    if st.button("내려받을 파일 만들기", key="io_export_build"):
        try:
            st.session_state["io_export"] = export_bytes(service)
        except sqlite3.Error:
            st.error("데이터를 읽지 못했습니다. 다시 시도하세요.")
    if st.session_state.get("io_export"):
        st.download_button("현재 데이터 내려받기", st.session_state["io_export"], mime=XLSX,
                           file_name=f"CoE인력관리_{datetime.now():%Y%m%d_%H%M}.xlsx",
                           icon=":material/download:", key="io_export")


def _table(rows: list[dict]) -> None:
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _preview_and_import(service, actor: str, data: bytes) -> None:
    done = st.session_state.setdefault("io_done", {})
    digest = hashlib.sha256(data).hexdigest()
    if digest in done:                       # 가져온 뒤에는 결과만 보여준다(같은 파일 미리보기는 모두 '이미 등록')
        st.success(done[digest])
        st.caption("다른 파일을 가져오려면 위에서 파일을 바꾸세요.")
        return
    try:
        plan = build_import(service, data)
    except ValidationError as exc:
        st.error(str(exc))
        return
    except sqlite3.Error:
        st.error("DB를 읽지 못했습니다. 다시 시도하세요.")
        return
    summary = plan["summary"]
    cols = st.columns(4)
    for col, (label, value) in zip(cols, [("인력", summary["인력"]), ("투입", summary["투입"]),
                                          ("구매계약", summary["구매계약"]), ("오류", len(plan["errors"]))]):
        col.metric(f"{label} (가져올 건수)" if label != "오류" else "오류", value)
    if plan["errors"]:
        st.error(f"오류 {len(plan['errors'])}건이 있어 가져올 수 없습니다. 파일을 고친 뒤 다시 올리세요.")
        _table(plan["errors"])
    if plan["mm_check"]:
        mismatch = [r for r in plan["mm_check"] if r["일치"] == "아니오"]
        (st.warning if mismatch else st.success)(
            f"구매시스템 MM 비교: {len(plan['mm_check'])}건 중 {len(mismatch)}건 다름")
        _table(plan["mm_check"])
    if plan["warnings"]:
        st.warning("확인할 내용이 있습니다. 가져오면 검토한 것으로 기록됩니다.")
        _table(plan["warnings"])
    if plan["skipped"]:
        with st.expander(f"이미 등록되어 건너뛸 줄 {len(plan['skipped'])}건"):
            _table(plan["skipped"])
    if plan["errors"]:
        return
    if not any(summary.values()):
        st.info("새로 가져올 데이터가 없습니다.")
        return
    token = st.session_state.setdefault("io_tokens", {}).setdefault(plan["file_hash"], str(uuid4()))
    if st.button(f"확인 후 가져오기 (인력 {summary['인력']} · 투입 {summary['투입']} · 계약 {summary['구매계약']})",
                 type="primary", key="io_confirm"):
        if not actor.strip():
            st.error("왼쪽 '변경 주체'를 입력하세요.")
            return
        try:
            result = service.import_batch(plan, token=token, actor=actor.strip())
        except (ValidationError, ConflictError) as exc:
            st.error(str(exc))
            return
        except sqlite3.Error:
            st.error("저장하지 못했습니다. 아무것도 저장되지 않았습니다. 다시 시도하세요.")
            return
        done[plan["file_hash"]] = (f"가져왔습니다: 인력 {result['인력']}명, 투입 {result['투입']}건, "
                                   f"구매계약 {result['구매계약']}건. 변경 이력에 기록했습니다.")
        st.rerun()
