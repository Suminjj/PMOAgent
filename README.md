# PMOAgent — CoE 인력관리

Python · Streamlit · SQLite로 동작하는 로컬 PoC입니다. 현재 단계에서는
인력과 투입 정보를 등록·조회·수정하고 변경 이력을 확인할 수 있습니다.
LLM API 키 없이 실행할 수 있습니다.

## 실행

Python 3.11 이상을 준비한 뒤 저장소 최상위에서 실행하세요.
검증 환경은 Python 3.12, Streamlit 1.65.0입니다.

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

PowerShell에서 활성화가 제한되면 정책 변경 없이 아래처럼 직접 실행할 수 있습니다.

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m streamlit run app.py
```

macOS / Linux:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

기본 접속 주소는 http://localhost:8501 입니다.
최초 실행 시 `data/pmo.db`가 생성되고 앱 종료·재실행 후에도 데이터가 유지됩니다.
실제 데이터는 개인 PC에만 저장하고 Git에 커밋하지 마세요.
별도의 샘플 데이터는 자동으로 등록하지 않습니다.

## 사용 순서

1. 사이드바의 변경 주체를 입력합니다. 이 값은 이력 기록용이며 사용자 인증은 아닙니다.
2. 인력 관리에서 이름·업체명·분류·투입 등급·시작일·종료일을 입력합니다.
   계정 ID는 발급 전이면 비워둡니다. 내부 관리번호는 자동 생성됩니다.
3. 미리보기에서 내용을 확인한 뒤 확인 후 저장을 누릅니다. 취소하면 저장하지 않습니다.
4. 계정이 발급되면 인력 관리에서 기존 인력을 선택해 계정 ID를 추가합니다.
   투입 관리에서는 첫 투입 건을 선택하여 과제·단가를 보완하거나 추가 투입을 등록합니다.
   과제·매출·매입단가는 모르면 비워둘 수 있습니다. 단가를 입력하면 단위(예: 원/MM)도 입력합니다.
5. 투입 기간이 다른 건과 겹치면 내역을 검토하고 검토 확인란을 선택합니다.
6. 목록 아래에서 기존 인력·투입을 선택하여 수정하고 변경 이력에서 전후 값을 확인합니다.

단가는 쉼표 없이 입력하며 0원도 유효한 입력입니다. 빈 단가는 미입력으로 저장·표시하며
0원으로 대체하지 않습니다. 이름·업체명이 같아도 별도의 내부 관리번호로 등록할 수 있습니다.
MM·검수 계산, 계약 관리, 자연어 요청은 후속 단계입니다.
신규 투입 저장 시 4개 준비 체크리스트 항목은 함께 생성합니다.
항목의 조회·상태 변경 화면은 다음 단계에서 구현합니다.

## 설정

| 환경변수 | 기본값 | 용도 |
| --- | --- | --- |
| PMO_DB_PATH | 저장소의 data/pmo.db | 별도 DB 파일 경로 |
| PMO_GRADES | L7,L6,L5 | 쉼표로 구분한 허용 등급 |

Windows PowerShell 예:

```powershell
$env:PMO_DB_PATH = "C:\PMOData\pmo.db"
$env:PMO_GRADES = "L7,L6,L5"
python -m streamlit run app.py
```

macOS / Linux 예:

```bash
PMO_DB_PATH=/absolute/path/pmo.db PMO_GRADES=L7,L6,L5 python -m streamlit run app.py
```

등급 목록을 바꾸더라도 기존 값을 자동 변경하지 않습니다. 기존 등급이 목록에서 빠졌다면
수정 시 허용된 등급을 선택해야 합니다.

## 검증

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

별도 임시 DB와 가상 데이터로 저장 지속성, 동명이인, 유효성 검증,
변경 이력, 중복 요청, 수정 충돌, 트랜잭션 롤백을 검증합니다.
Streamlit AppTest로 미리보기·취소·저장·수정·재실행 흐름을 검증합니다.
실제 사용자 PC 및 구매시스템과의 업무 검증은 별도로 수행해야 합니다.

## 구성 및 데이터

- `app.py`: Streamlit 입력·조회·미리보기·확인 화면
- `pmo/config.py`: DB 경로와 허용 등급 설정
- `pmo/db.py`: 연결과 초기 스키마
- `pmo/service.py`: 유효성 검증과 등록·수정·이력 저장
- `tests/`: 업무 로직 및 화면 흐름 테스트
- [데이터 설계](docs/data-model.md): 필드 의미, 저장 규칙 및 후속 TODO

초기 PoC는 로컬 담당자 사용을 전제로 합니다. 인증·접근 권한 기능은 없습니다.
단가와 과제는 투입 정보에 저장하며, 구매기간·구매MM은 계약·검수 구현 시 추가합니다.
알 수 없는 기존 DB의 스키마는 자동 변경하지 않습니다.

## 백업과 미확정 사항

DB 백업·복구는 앱을 종료한 상태에서 `data/pmo.db` 또는 설정된 DB 파일을
복사하는 방식으로 수행하세요. 향후 스키마 변경 전에 별도 백업과 마이그레이션을 마련합니다.

업무 규칙의 원본은 [AGENTS.md](AGENTS.md)입니다. MM 공식, 단가 변경 적용 기간,
과제명 변경·중복 식별과 중복 투입 허용 기준은 아직 확정하지 않았습니다.
현재 투입 수정은 저장 정보 정정이며 기간별 단가 이력 계산을 의미하지 않습니다.

### 기존 DB 업데이트

첫 버전 DB(스키마 v1)는 앱 실행 시 v2로 자동 변경됩니다. 변경 전에 DB 파일과 같은 폴더에
`pmo.db.backup-v1-날짜-구분값.db` 형식의 백업을 생성합니다.
인력·투입 관리번호, 기존 단가, 체크리스트, 변경 이력과 저장 요청 기록은 보존합니다.
기존에 직접 입력한 ID는 내부 관리번호로 유지합니다. 회사 계정 ID였더라도
자동 추정하지 않으므로 인력 수정 화면에서 계정 ID를 따로 보완하세요.

변경 실패 시 DB 구조 변경을 되돌리고 오류를 표시합니다. 수동 복구가 필요하면 앱을 종료하고,
현재 DB를 별도 보관한 뒤 백업 파일을 원래 DB 경로로 복사하세요.
백업 파일도 업무 데이터를 포함하므로 Git에 추가하지 마세요.
