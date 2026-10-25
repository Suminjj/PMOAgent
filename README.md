# PMOAgent — CoE 인력관리

Python · Streamlit · SQLite로 동작하는 로컬 MVP입니다. 인력·투입·구매계약 정보를
등록·조회·수정하고, 계약 종료 예정 목록과 종료 14일 전 알림, 변경 이력을 확인할 수 있습니다.
LLM API 키 없이 폼과 버튼으로 사용합니다. 개발 지침은 [AGENTS.md](AGENTS.md),
개발 계획과 구조는 [docs/dev-plan.md](docs/dev-plan.md)에 있습니다.

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

## 가상 데이터로 시연하기

업무 DB와 분리된 시연용 DB에 가상 인력 5명·투입·계약을 넣고 그 DB로 앱을 실행합니다.
오늘 날짜를 기준으로 종료 예정·알림 사례가 보이도록 만들어집니다.
데이터가 이미 있는 DB에는 넣지 않습니다.

```bash
python -m scripts.seed_demo              # data/demo.db 생성
PMO_DB_PATH=data/demo.db python -m streamlit run app.py
```

Windows PowerShell:

```powershell
python -m scripts.seed_demo
$env:PMO_DB_PATH = "data\demo.db"
python -m streamlit run app.py
```

시연 DB를 지우고 다시 만들려면 앱을 종료한 뒤 `data/demo.db` 파일만 삭제하고 다시 실행하세요.

## 사용 순서

첫 화면 '홈'에서는 PMO Agent에게 자연어로 묻고, 왼쪽 메뉴의 각 화면에서는 직접 조회·등록·수정합니다.

1. 사이드바의 변경 주체를 입력합니다. 이 값은 이력 기록용이며 사용자 인증은 아닙니다.
왼쪽 메뉴: 홈 · 인력 관리(전체 인력 조회 · 신규 인력 등록 · 과제 투입 관리) · 근무 환경 준비 ·
구매계약 관리(전체 계약 조회 · 계약 등록·수정) · 계약 종료 알림 · 변경 이력

2. 인력 관리 › 신규 인력 등록에서 이름·업체명·분류·투입 등급·시작일·종료일을 입력합니다.
   계정 ID는 발급 전이면 비워둡니다. 내부 관리번호는 자동 생성됩니다.
   투입 Type이 Full time이면 기간 입력 즉시 MM를 계산하고, Part time이면 MM를 직접 입력합니다.
3. 미리보기에서 내용을 확인한 뒤 확인 후 저장을 누릅니다. 취소하면 저장하지 않습니다.
4. 계정이 발급되면 인력 관리 › 전체 인력 조회 아래 '인력 정보 수정 · 계정 ID 보완'에서 계정 ID를 추가합니다.
   인력 관리 › 과제 투입 관리에서는 첫 투입 건을 선택하여 과제·단가를 보완하거나 추가 투입을 등록합니다.
   과제·매출·매입단가는 모르면 비워둘 수 있습니다. 단가를 입력하면 단위(예: 원/MM)도 입력합니다.
5. 투입 기간이 다른 건과 겹치면 내역을 검토하고 검토 확인란을 선택합니다.
6. 목록 아래에서 기존 인력·투입을 선택하여 수정하고 변경 이력에서 전후 값을 확인합니다.
   과제 투입 관리는 현재·예정·과거 투입을 나눠 보여주며, 투입 과제와 별도로 매출귀속 프로젝트를 입력할 수 있습니다.
   전체 인력 조회의 '전체 투입 · 월별 MM' 표는 회계연도(4월~다음 해 3월)를 골라 투입별 시작·종료일과 달마다의 MM(구매시스템 방식)을 보여줍니다.
7. 구매계약 관리 › 계약 등록·수정에서 협력·컨설팅 인력의 계약을 등록합니다. 계약번호 발급 전이면 상태를 '준비'로,
   발급 후 '확정'으로 바꾸고 계약번호를 입력합니다. 연장 계약은 새로 등록하면서 이전 계약을 선택합니다.
8. 구매계약 관리 › 전체 계약 조회에서 모든 계약을 봅니다. 계약 종료 예정(종료 14일 전~당일, 연장 계약 미등록)은
   붉은 줄로 표시되고, '계약 종료 예정만 보기'로 그것만 볼 수 있습니다. 업체·계약 상태·종료 월로도 거를 수 있습니다.
9. '근무 환경 준비'에서 투입별 방문예약·계정 신청·ID카드 신청·OA기기 신청 진행 상황을 확인하고 상태를 바꿉니다.
   '해당 없음'은 사유가 필요하고, '완료'는 사내 시스템에서 실제 신청·예약을 마쳤다고 확인해야 저장됩니다.
   '신청 초안 만들기'는 사내 시스템에 붙여 넣을 문구만 만들며 상태는 바뀌지 않습니다.
10. 계약 종료 14일 전부터 화면 상단과 '계약 종료 알림' 메뉴에 표시됩니다. 앱을 다시 실행해도
   미확인 대상은 계속 표시됩니다. '확인'은 알림을 봤다는 기록이며 계약 갱신 완료와는 별개입니다.

단가는 쉼표 없이 입력하며 0원도 유효한 입력입니다. 빈 단가는 미입력으로 저장·표시하며
0원으로 대체하지 않습니다. 이름·업체명이 같아도 별도의 내부 관리번호로 등록할 수 있습니다.
검수 금액 계산, 월별 구매 필요 판정, 자연어 등록·수정은 후속 단계입니다.
신규 투입 저장 시 4개 준비 체크리스트 항목을 함께 생성합니다.

## 로컬 LLM으로 자연어 조회 (Ollama)

LLM은 PC에서 직접 실행하는 [Ollama](https://ollama.com)를 사용합니다. 질문과 데이터가 PC 밖으로 나가지 않습니다.
LLM이 없어도 다른 화면 기능은 그대로 동작합니다.

1. Ollama를 설치하고 실행합니다(Windows 설치 파일 사용). Text-to-SQL에 쓰는 LlamaIndex는
   `requirements.txt`에 포함되어 있어 `pip install -r requirements.txt`로 함께 설치됩니다.
2. 모델을 받습니다. RAM 32GB PC 기준으로 아래 순서로 시험해 보세요.

   ```powershell
   ollama pull qwen3:8b       # 기본값, 가볍고 빠름
   ollama pull gemma4:12b     # 비교 후보 (Google, 약 8GB)
   ollama pull qwen3:14b      # 비교 후보 (조금 더 무거움)
   ollama show qwen3:8b       # Capabilities에 tools가 있어야 함
   # 이름에 cloud가 붙은 모델(예: gemma4:cloud)은 Ollama 서버에서 실행되어 데이터가 PC 밖으로 나가므로 쓰지 않습니다.
   ```

3. 평가 질문으로 모델을 비교합니다. 질문별 정답 도구 선택 여부, 답변, 응답 시간이 출력되고
   `evals/results/`에 저장됩니다.

   ```powershell
   .venv\Scripts\python.exe -m scripts.run_evals --model qwen3:8b,gemma4:12b,qwen3:14b
   # 자동(전체) 단일 Agent와 카테고리별 전문 Agent 비교
   .venv\Scripts\python.exe -m scripts.run_evals --model qwen3:8b --routing auto,category
   ```

4. 앱 첫 화면인 '홈'에서 질문합니다. 왼쪽은 AI Agent와의 대화, 오른쪽은 Agent가 사용한 도구와 조회 결과 표입니다.
   "어느 김가상이요?"처럼 Agent가 되물으면 이어서 답하면 됩니다(최근 대화 8개를 함께 보냄).
   질문이 여러 개면 오른쪽 위에서 결과를 볼 질문을 고를 수 있습니다. 터미널에서 한 번 물어볼 수도 있습니다.

   입력창 위의 **업무 카테고리**(자동(전체) · 인력관리 · 근무 환경 준비 · 구매계약 · 검수)를 고르면
   그 업무 전문 Agent가 필요한 도구와 업무 지침(`agent_skills/`)만 가지고 답합니다. 자동(전체)은 모든 도구를 쓰는 단일 Agent입니다.
   맡은 업무 밖의 질문이면 다른 카테고리를 고르라고 안내합니다. 터미널에서는 `--agent contract`처럼 지정합니다.

   ```powershell
   $env:PMO_OLLAMA_MODEL = "qwen3:8b"
   $env:PMO_DB_PATH = "data\demo.db"
   .venv\Scripts\python.exe -m scripts.ask "베타컨설팅 10월 계약 종료 예정 인력 알려줘"
   .venv\Scripts\python.exe -m streamlit run app.py
   ```

**조회**: 정해진 조회 도구로 답하고, 그 밖의 조회(조건 검색, 건수 집계 등)는 LlamaIndex Text-to-SQL로
SELECT 문을 만들어 조회합니다. 만든 SQL은 오른쪽 결과에 그대로 보여주며, 읽기 전용 연결에서
검사를 통과한 SELECT만 실행되어 데이터가 바뀌지 않습니다.

**등록·수정·삭제**: "이가상 ID카드 신청 진행 중으로 바꿔줘"처럼 요청하면 Agent는 저장하지 않고,
대상을 확정해 오른쪽에 '현재 → 변경' 확인 카드를 보여줍니다. '확인 후 저장'을 눌러야 반영되고, '취소'하면 저장하지 않습니다.
'완료'와 삭제는 확인란을 한 번 더 선택해야 합니다. 동명이인이나 계약·투입이 여러 건이면 카드 대신 Agent가 되묻습니다. Agent로 할 수 있는 작업은 근무 환경 준비 상태 변경,
구매계약 등록·수정·삭제, 계정 ID 입력, 계약 종료 알림 확인입니다.

구매 필요 판정·검수 금액은 규칙이 미확정이라 '구현 예정'으로 안내합니다. 첫 질문은 모델을 메모리에 올리느라 느릴 수 있습니다.

평가는 질문이 끝날 때마다 결과를 출력합니다. 그래픽카드 메모리가 작아 `ollama ps`의 PROCESSOR가
CPU/GPU로 나뉘면 질문당 1분 이상 걸릴 수 있습니다. 이때는 모델을 하나씩 돌리고,
추론 모드는 기본으로 꺼져 있습니다. 켜서 비교하려면 `$env:PMO_OLLAMA_THINK = "true"`로 설정하세요.
평가 시작 전에 Ollama 서버와 모델을 먼저 확인하므로, 서버가 꺼져 있거나 모델이 없으면 바로 안내하고 멈춥니다.

## 설정

| 환경변수 | 기본값 | 용도 |
| --- | --- | --- |
| PMO_DB_PATH | 저장소의 data/pmo.db | 별도 DB 파일 경로 |
| PMO_GRADES | L7,L6,L5 | 쉼표로 구분한 허용 등급 |
| PMO_EXTRA_HOLIDAYS | 빈 값 | 추가 제외할 회사 휴일·임시공휴일(YYYY-MM-DD, 쉼표 구분) |
| PMO_OLLAMA_MODEL | qwen3:8b | 자연어 조회에 쓸 Ollama 모델 |
| PMO_OLLAMA_URL | http://localhost:11434 | Ollama 서버 주소 |
| PMO_OLLAMA_THINK | false | 추론 모드. 기본 끔(평가에서 정답률은 같고 약 4배 빠름). `true`로 켜고, `model`이면 모델 기본값 |
| PMO_OLLAMA_NUM_CTX | 8192 | 모델이 한 번에 읽는 길이(토큰). Ollama 기본 4096은 지시문·도구 설명이 잘릴 수 있어 늘려 둠 |

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
python -m scripts.run_evals
```

`run_evals`는 가상 데이터를 임시 DB에 넣고 대표 질문(`evals/questions.json`)마다 정답 도구를 실행해
성공 여부와 소요 시간을 `evals/results/`에 기록합니다. `--model`을 주면 로컬 LLM Agent로 같은 질문을
실행해 모델별 정답률과 응답 시간을 비교합니다(위 '로컬 LLM' 참고).

별도 임시 DB와 가상 데이터로 저장 지속성, 동명이인, 유효성 검증,
변경 이력, 중복 요청, 수정 충돌, 트랜잭션 롤백을 검증합니다.
Streamlit AppTest로 미리보기·취소·저장·수정·재실행 흐름을 검증합니다.
실제 사용자 PC 및 구매시스템과의 업무 검증은 별도로 수행해야 합니다.

## 구성 및 데이터

- `app.py`: Streamlit 입력·조회·미리보기·확인 화면
- `ui/theme.py`, `.streamlit/config.toml`: 화면 테마(네이비 콘솔). 글꼴(IBM Plex Sans KR)은 Google Fonts에서 받으며, 받을 수 없으면 기본 글꼴로 보입니다
- `pmo/config.py`: DB 경로, 허용 등급, 기준일(Asia/Seoul) 설정
- `pmo/db.py`: 연결, 스키마와 마이그레이션
- `pmo/service.py`: 유효성 검증, 등록·수정·이력 저장, 계약·알림·투입 이력 조회, 실행 기록
- `pmo/contracts.py`: 계약 검증과 알림 기준 계산
- `pmo/tools.py`: Agent가 호출하는 도구 계층
- `pmo/agent/`: LLM 연결(Ollama), 시스템 프롬프트, Agent 실행 루프, 카테고리별 전문 Agent(`agents.py`)
- `scripts/ask.py`: 터미널에서 자연어 질문
- `pmo/harness.py`, `scripts/run_evals.py`, `evals/`: 평가 하네스
- `pmo/demo.py`, `scripts/seed_demo.py`: 가상 데이터
- `agent_skills/`: 서비스 Agent 업무 지침 (전문 Agent 프롬프트에 주입, 자동(전체)에는 미주입)
- `.claude/skills/`: Claude Code 개발 스킬
- `tests/`: 업무 로직 및 화면 흐름 테스트
- [데이터 설계](docs/data-model.md): 필드 의미, 저장 규칙 및 후속 TODO

초기 PoC는 로컬 담당자 사용을 전제로 합니다. 인증·접근 권한 기능은 없습니다.
단가와 과제는 투입 정보에 저장하며, 구매기간·구매MM은 계약·검수 구현 시 추가합니다.
알 수 없는 기존 DB의 스키마는 자동 변경하지 않습니다.

## 백업과 미확정 사항

DB 백업·복구는 앱을 종료한 상태에서 `data/pmo.db` 또는 설정된 DB 파일을
복사하는 방식으로 수행하세요. 향후 스키마 변경 전에 별도 백업과 마이그레이션을 마련합니다.

업무 규칙의 원본은 [AGENTS.md](AGENTS.md)입니다. 회사별 휴일, 단가 변경 적용 기간,
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

구매계약 기능 업데이트 시 v3 DB는 `pmo.db.backup-v3-구분값.db` 백업 후 v4로 변경합니다.
기존 인력·투입·이력은 그대로 두고 계약·알림·실행 기록 테이블과 매출귀속 프로젝트 칸만 추가합니다.

체크리스트 기능 업데이트 시 v4 DB는 `pmo.db.backup-v4-구분값.db` 백업 후 v5로 변경합니다.
기존 항목의 상태·사유는 그대로 두고 버전·신청 초안·처리 시각 칸만 추가합니다.

MM 기능 업데이트 시 v2 DB도 백업 후 v3로 변경합니다.
기존 투입 건의 Type과 MM는 임의 추정하지 않고 미입력으로 유지합니다.
과제 투입 관리에서 기존 건을 선택한 뒤 Type을 지정하여 보완하세요.

## 엑셀 데이터 가져오기 (명령어)

실제 데이터는 PC에서 엑셀 양식에 채운 뒤 아래 명령으로 넣습니다. 파일 내용은 PC 밖으로 나가지 않습니다.
화면에서 올리고 내려받는 기능은 각 메뉴를 완성한 뒤 필요한 화면에 추가합니다.

```powershell
Remove-Item Env:PMO_DB_PATH -ErrorAction SilentlyContinue   # 시연 DB가 아닌 업무 DB(data\pmo.db)
.venv\Scripts\python.exe -m scripts.import_excel --template 양식.xlsx        # 빈 양식
.venv\Scripts\python.exe -m scripts.import_excel 채운파일.xlsx               # 미리보기 → y 입력 시 저장
.venv\Scripts\python.exe -m scripts.import_excel --export 현재데이터.xlsx    # 현재 DB 내보내기
```

- 가져올 건수, 오류(시트·행·이유), 기간 중복 경고, 구매시스템 MM 비교, 건너뛸 줄을 먼저 보여줍니다.
- 오류가 하나라도 있으면 저장하지 않습니다. 저장은 전부 되거나 하나도 되지 않으며, 저장 전 DB를 `data\backups\`에 복사합니다.
- 같은 파일을 다시 실행하면 "이미 가져온 파일"로 멈춥니다. 이미 등록된 인력(관리번호)·투입·계약과 같은 줄은 건너뜁니다.
- 인력키는 파일 안에서만 쓰는 연결용 값입니다(예: P001). 이미 등록된 사람에게 추가할 때는 그 사람의 내부 관리번호를 씁니다.
- 양식 열: 인력(인력키·이름·업체명·분류·직무·등급·단가·계정 ID), 투입(인력키·투입 과제·매출귀속 프로젝트·매출귀속팀·
  투입시작일·투입종료일·투입 Type·투입 MM), 구매계약(인력키·업체명·계약번호·계약 시작일·계약 종료일).
  인력 시트의 등급·단가는 그 사람의 투입에 기본값으로 들어갑니다(단가는 매입단가 원/MM). 컨설팅은 등급을 비워도 됩니다.
  구매계약 상태는 계약번호가 있으면 확정, 없으면 준비입니다. 계약 업체가 인력의 업체와 다르면 경고합니다.

## 투입 MM

- Full time은 구매시스템과 같은 방식으로 계산합니다(구매시스템 결과 11건과 일치 확인, `tests/test_purchase_mm.py`).
  - 시작·종료일을 포함해 월별로 (투입 근무일 ÷ 그 달 전체 근무일)을 구해 합산합니다.
  - 토·일요일과 한국 공휴일·대체공휴일은 근무일에서 뺍니다. 단 노동절·제헌절·지방선거일은 근무일로 셉니다.
  - 합계를 소수 셋째 자리에서 반올림합니다. 월별 값은 반올림하지 않습니다.
- 예: 2026-10-01~2026-10-10은 5/20 = 0.250MM, 2026-04-27~2026-05-08은 4/22 + 5/19 = 0.445MM입니다.
- 월을 넘으면 각 월의 비율을 합산합니다. 10월·11월 전체 투입은 2MM입니다.
- Part time: 선택 기간 전체의 MM를 직접 입력합니다. 단위는 MM이며 0 이상 숫자가 필요합니다.
- 날짜·Type·입력을 변경하면 미리보기를 무효화하므로 새 미리보기에서 확인 후 저장하세요.
- 저장하는 MM는 셋째 자리까지 반올림한 구매시스템 값입니다. 반올림 전 값(`mm_exact`)과 분수 원본은 계산 근거에 남깁니다.
  검수 금액의 반올림 기준은 아직 정하지 않았습니다.
- 규칙을 바꾸기 전에 저장한 Full time 투입은 그 투입을 다시 저장할 때 새 규칙으로 계산됩니다.
- 달력은 holidays 0.106의 KR PUBLIC 데이터를 사용하며 런타임 네트워크 없이 계산합니다.
  라이브러리에 반영되지 않은 임시공휴일이나 회사 휴일은 추가 설정이 필요합니다.
  계산 근거에서 제외 날짜를 확인하고 구매시스템 달력과 대조하세요.
- 회사 휴일 예: PowerShell에서
  `$env:PMO_EXTRA_HOLIDAYS = "2026-12-31"` 설정 후 앱을 실행합니다.
  이 예시 날짜는 회사 휴일로 확정한 것이 아닙니다.
- 월 전체 근무일이 없거나 달력 범위를 벗어나면 계산 실패를 안내하고 저장하지 않습니다.
