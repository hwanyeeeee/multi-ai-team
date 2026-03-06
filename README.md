# Multi-AI Team

3개 AI (Claude, Codex, Gemini)가 tmux 분할 화면에서 실시간으로 협업하는 범용 도구.

```
┌──────────────────┬──────────────────┐
│  Claude          │  Codex           │
│  (pane 0)        │  (pane 1)        │
├──────────────────┼──────────────────┤
│  Gemini          │  Chat Input      │
│  (pane 2)        │  (pane 3)        │
└──────────────────┴──────────────────┘
```

브레인스토밍, 논문 리뷰, 아이디어 토론, 기획, 분석, 개발 등 어떤 주제든
3개 AI에게 동시에 질문하고 응답을 비교·종합할 수 있습니다.

---

## 설치 가이드

### 1단계: WSL 설치 (Windows)

Windows에서 실행하므로 WSL(Windows Subsystem for Linux)이 필요합니다.

```powershell
# PowerShell (관리자 권한)
wsl --install
```

설치 후 재부팅하고, Ubuntu 사용자 이름/비밀번호를 설정합니다.

> 이미 WSL이 설치되어 있다면 이 단계를 건너뛰세요.

### 2단계: tmux 설치

WSL 터미널을 열고 설치합니다:

```bash
sudo apt update && sudo apt install -y tmux
```

설치 확인:

```bash
tmux -V
# 예: tmux 3.3a
```

### 3단계: Python 환경 설정

WSL에 Python 3.10+ 과 필수 패키지를 설치합니다:

```bash
sudo apt install -y python3 python3-pip
pip3 install rich
```

### 4단계: Node.js 설치

AI CLI들은 npm으로 설치하므로 Node.js가 필요합니다:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

설치 확인:

```bash
node -v   # v20.x.x
npm -v    # 10.x.x
```

### 5단계: AI CLI 설치

사용할 AI CLI를 WSL 환경에 설치합니다. **최소 1개 이상** 필요하며,
3개 모두 설치하면 최적의 협업이 가능합니다.

#### Claude Code (Anthropic)

```bash
npm install -g @anthropic-ai/claude-code

# 최초 인증 (브라우저가 열림)
claude
```

#### Codex CLI (OpenAI)

```bash
npm install -g @openai/codex

# API 키 설정
echo 'export OPENAI_API_KEY="sk-..."' >> ~/.bashrc
source ~/.bashrc
```

#### Gemini CLI (Google)

```bash
npm install -g @google/gemini-cli

# 최초 인증 (브라우저가 열림)
gemini
```

> 각 CLI의 정확한 패키지 이름과 인증 방법은 공식 문서를 확인하세요.

### 6단계: 설치 확인

```bash
which claude && echo "Claude OK" || echo "Claude NOT FOUND"
which codex  && echo "Codex OK"  || echo "Codex NOT FOUND"
which gemini && echo "Gemini OK" || echo "Gemini NOT FOUND"
```

---

## 사용법

### 기본 실행 (tmux 대화형 모드)

Windows PowerShell에서:

```powershell
cd C:\tools\multi_ai_team
python run.py
```

또는 WSL에서:

```bash
cd /mnt/c/tools/multi_ai_team
python3 run.py
```

실행하면 tmux 4분할 화면이 열리고, 각 AI CLI가 자동으로 시작됩니다.
사용 가능한 AI CLI만 자동 감지하여 실행됩니다.

### 배치 모드 (tmux 없이)

```bash
# 단일 작업 실행
python run.py --no-tmux "이 주제에 대해 분석해줘"

# 대화형 루프
python run.py --no-tmux
```

### 옵션

| 옵션                  | 설명                       |
| ------------------- | ------------------------ |
| `--no-tmux`         | tmux 없이 배치 모드로 실행        |
| `--work-dir <path>` | 작업 디렉토리 지정 (기본: 스크립트 위치) |
| `--skip-check`      | 사전 환경 검사 건너뛰기            |

---

## AI 지정 (@mention)

```
# 특정 AI에게만 전송
@claude 이 아이디어의 논리적 허점을 찾아줘

# 여러 AI에게 전송
@claude @gemini 이 논문 요약해줘

# 전체에게 명시적 전송
@all 각자 의견을 말해봐

# @mention 없이 입력 → 전체 브로드캐스트
이 주제에 대해 어떻게 생각해?
```

`@mention` 없이 메시지를 보내면 **모든 활성 AI에게 브로드캐스트**됩니다.

---

## 채팅 명령어

| 명령어           | 설명                        |
| ------------- | ------------------------- |
| `/help`       | 도움말 표시                    |
| `/models`     | 활성 AI 모델 목록               |
| `/synth`      | 모든 AI 응답을 캡처하여 종합         |
| `/autosynth`  | 자동 종합 켜기/끄기 토글            |
| `/task <설명>`  | 자동 오케스트레이션 (계획→할당→실행→종합)  |
| `/batch <주제>` | AI간 토론 (합의까지 반복)          |
| `/history`    | 대화 로그 보기                  |
| `/clear`      | 로그 초기화                    |
| `/events [n]` | 최근 이벤트 스트림 (기본 20개)       |
| `/sessions`   | 과거 세션 목록                  |
| `"""`         | 멀티라인 입력 모드 (`"""`로 시작/종료) |
| `/quit`       | 종료                        |

---

## 입력 단축키

| 키               | 동작             |
| --------------- | -------------- |
| ← → 방향키         | 커서 좌우 이동       |
| Home / End      | 줄 처음/끝으로 이동    |
| Delete          | 커서 위치 문자 삭제    |
| Backspace       | 커서 앞 문자 삭제     |
| Ctrl+A / Ctrl+E | 줄 처음/끝 이동      |
| Ctrl+U          | 커서부터 줄 처음까지 삭제 |
| Ctrl+K          | 커서부터 줄 끝까지 삭제  |

> 한글(CJK) 2칸 문자도 정확히 처리됩니다.

---

## 주요 기능

### /task — 자동 오케스트레이션

AI 3개가 자동으로 작업을 나눠 수행합니다.

```
/task 이 사업 아이디어의 시장성을 분석해줘
```

1. **Phase 1 (Planning)**: 각 AI가 독립적으로 계획 수립 (병렬)
2. **Phase 2 (Assignment)**: Claude가 계획을 검토하고 각 AI에게 작업 할당 → 사용자 확인
3. **Phase 3 (Execution)**: 각 AI가 할당된 작업 실행 → Claude가 결과 종합

### /batch — AI간 토론

```
/batch AGI는 10년 내에 실현 가능한가?
```

- Round 1: 각 AI가 독립적으로 의견 제시
- Round 2+: 다른 AI 의견 분석(강한 포인트, 동의/반대, 빠진 관점) 후 반응
- 합의 체크: Claude가 수렴 여부 판단
- 최대 5라운드 또는 합의 시 종료
- 최종 종합 결과 출력
- **결과가 각 AI pane에 자동 전달** → 후속 대화를 바로 이어갈 수 있음

### /synth — 수동 종합

각 AI의 현재 화면을 캡처하여 Claude가 종합합니다.
자유 대화 중에 "지금까지 각자 뭐라고 했는지" 정리할 때 유용합니다.

### 자동 합성 (/autosynth)

2개 이상의 AI에게 메시지를 보내면, 응답 완료 후 Claude가 자동으로 종합합니다.
`/autosynth`로 ON/OFF 전환 가능.

### 컨텍스트 자동 관리

각 AI의 컨텍스트 사용량을 주기적으로 모니터링합니다 (5 메시지마다 점검).
- **경고**: 80K 문자 초과 시 알림
- **자동 리셋**: 150K 문자 초과 시 대화 요약 후 세션 재시작

---

## 배치 모드 라운드 프로토콜

`--no-tmux` 모드에서는 4단계 라운드로 진행됩니다:

```
Round 1: 계획 (Plan)       — 각 AI가 계획안 작성
Round 2: 리뷰 (Review)     — 다른 AI 계획을 검토/평가
Round 3: 수정 (Revise)     — 피드백 반영하여 수정
Round 4: 종합 (Synthesize) — Claude가 최종안 통합
```

---

## 세션 데이터

모든 세션 데이터는 `shared/<timestamp>/`에 저장됩니다:

```
shared/2025-01-15_143022/
├── session.json           # 세션 메타데이터
├── chat.jsonl             # 대화 로그
├── events.jsonl           # 이벤트 스트림
├── shared_context.json    # AI 간 공유 컨텍스트
├── todo.md                # /task 체크리스트
├── orch_plan_*.md         # /task 계획
├── orch_assign.md         # /task 역할 배정
├── orch_final.md          # /task 최종 합성
├── batch_r*_*.md          # /batch 라운드별 응답
├── batch_synthesis.md     # /batch 최종 합성
└── final_report.md        # 배치 모드 최종 보고서
```

---

## 프로젝트 구조

```
multi_ai_team/
├── run.py              # 메인 실행 (tmux/배치 모드 분기)
├── config.py           # 설정, 프롬프트 템플릿, 세션 관리
├── chat_loop.py        # 대화형 채팅 루프 (tmux input pane)
├── orchestrator.py     # /task 오케스트레이터, /batch 토론 엔진
├── ai_worker.py        # AI CLI 실행, pane 메시지 전송, 유휴 감지
├── tmux_manager.py     # tmux 세션/pane 생성 및 관리
├── round_manager.py    # 배치 모드 멀티라운드 프로토콜
├── conversation.py     # ConversationLog, SharedContext, EventStream
├── docs/
│   └── protocol.md     # 메시지 교환 프로토콜 규격
└── tests/              # 테스트
```

## 요구사항

- **OS**: Windows 10/11 + WSL2 (또는 네이티브 Linux/macOS)
- **Python**: 3.10+
- **패키지**: `rich`
- **tmux**: 3.0+ (대화형 모드)
- **AI CLI**: 최소 1개 (Claude Code, Codex CLI, Gemini CLI 중)

## 라이선스

MIT
