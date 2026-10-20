"""엑셀 데이터 가져오기 (PC에서 실행, 파일 내용은 PC 밖으로 나가지 않음).

    python -m scripts.import_excel 파일.xlsx              # 미리보기 → 확인(y) 후 저장
    python -m scripts.import_excel --template 양식.xlsx    # 빈 양식 만들기
    python -m scripts.import_excel 파일.xlsx --export 현재.xlsx   # 저장 후 현재 DB를 같은 양식으로 내보내기

DB는 PMO_DB_PATH(기본 data/pmo.db). 저장하기 전에 DB를 data/backups/에 복사해 둔다.
오류가 하나라도 있으면 저장하지 않는다. 전부 저장되거나 하나도 저장되지 않는다.
"""
import argparse
import hashlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

from pmo.config import db_path
from pmo.errors import ConflictError, ValidationError
from pmo.excel_io import build_import, export_bytes, template_bytes
from pmo.service import Service


def show(title: str, rows: list[dict], limit: int = 50) -> None:
    if not rows:
        return
    print(f"\n[{title}] {len(rows)}건")
    for row in rows[:limit]:
        print("  - " + " · ".join(f"{k} {v}" for k, v in row.items()))
    if len(rows) > limit:
        print(f"  … 외 {len(rows) - limit}건")


def main() -> int:
    parser = argparse.ArgumentParser(description="CoE 인력관리 엑셀 데이터 가져오기")
    parser.add_argument("file", nargs="?", help="채운 양식(.xlsx)")
    parser.add_argument("--template", help="빈 양식을 이 경로로 만든다")
    parser.add_argument("--export", help="가져온 뒤(또는 파일 없이) 현재 DB를 이 경로로 내보낸다")
    parser.add_argument("--actor", default="엑셀 가져오기", help="변경 이력에 남길 변경 주체")
    parser.add_argument("--yes", action="store_true", help="확인 질문 없이 저장(오류가 없을 때만)")
    args = parser.parse_args()

    if args.template:
        Path(args.template).write_bytes(template_bytes())
        print(f"빈 양식을 만들었습니다: {args.template}")
    path = db_path()
    service = Service(path)
    print(f"연결된 DB: {path} (인력 {len(service.people())}명)")
    if path.name == "demo.db":
        print("주의: 시연 DB입니다. 실제 데이터는 PMO_DB_PATH를 지우고 업무 DB(data/pmo.db)에 넣으세요.")

    if args.file:
        try:
            data = Path(args.file).read_bytes()
        except OSError as exc:
            print(f"파일을 읽을 수 없습니다: {exc}")
            return 2
        before = service.imported_before(hashlib.sha256(data).hexdigest())
        if before:
            print(f"이미 가져온 파일입니다(인력 {before['인력']}명 · 투입 {before['투입']}건 · 구매계약 {before['구매계약']}건). "
                  "다시 저장하지 않습니다.")
            data = None
        try:
            plan = build_import(service, data) if data else None
        except ValidationError as exc:
            print(f"파일을 읽을 수 없습니다: {exc}")
            return 2
    if args.file and plan:
        s = plan["summary"]
        print(f"\n가져올 건수: 인력 {s['인력']}명 · 투입 {s['투입']}건 · 구매계약 {s['구매계약']}건")
        show("오류 (고친 뒤 다시 실행하세요)", plan["errors"])
        show("확인할 내용 (가져오면 검토한 것으로 기록)", plan["warnings"])
        show("구매시스템 MM 비교", plan["mm_check"])
        show("이미 등록되어 건너뛸 줄", plan["skipped"], limit=10)
        if plan["errors"]:
            print("\n오류가 있어 저장하지 않았습니다.")
            return 1
        if not any(s.values()):
            print("\n새로 가져올 데이터가 없습니다.")
        else:
            if not args.yes and input("\n위 내용으로 저장할까요? (y/N) ").strip().lower() != "y":
                print("취소했습니다. 저장하지 않았습니다.")
                return 0
            backup = path.parent / "backups" / f"{path.stem}-before-import-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            try:
                result = service.import_batch(plan, token=f"import:{plan['file_hash']}", actor=args.actor)
            except (ValidationError, ConflictError) as exc:
                print(f"저장하지 못했습니다(아무것도 저장되지 않음): {exc}")
                return 1
            print(f"\n저장했습니다: 인력 {result['인력']}명, 투입 {result['투입']}건, 구매계약 {result['구매계약']}건")
            print(f"저장 전 DB 백업: {backup}")
    if args.export:
        Path(args.export).write_bytes(export_bytes(service))
        print(f"현재 DB를 내보냈습니다: {args.export}")
    if not (args.file or args.template or args.export):
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
