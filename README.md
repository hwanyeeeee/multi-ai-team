# Multi-AI Team

Claude + Codex + Gemini가 tmux 분할 화면에서 실시간 협업하는 시스템.

사용자가 채팅 pane에서 메시지를 보내면 AI들이 각자의 pane에서 응답하고,
자동 합성·토론·작업 위임까지 지원한다.

## 설치 요구사항

### WSL 환경

```bash
# tmux (필수)
sudo apt install tmux

# AI CLIs (최소 1개 이상 필요)
npm install -g @anthropic-ai/claude-code    # Claude
npm install -g @openai/codex                # Codex
npm install -g @google/gemini-cli           # Gemini
```

### Python 패키지

```bash
pip install rich
```

## 실행 방법

### 대화형 tmux 모드 (기본, 추천)

```bash
python run.py
```

tmux 4-pane 레이아웃에서 AI 3개 + 채팅 입력 pane이 열린다.
사용 가능한 AI CLI만 자동 감지하여 실행된다.

### 배치 모드 (tmux 없이)

```bash
# 단일 작업 실행
python run.py --no-tmux "로그인 시스템을 설계해줘"

# 대화형 루프
python run.py --no-tmux
```

### 옵션

| 옵션 | 설명 |
|------|------|
| `--no-tmux` | tmux 없이 배치 모드로 실행 |
| `--work-dir <path>` | 작업 디렉토리 지정 (기본: 스크립트 위치) |
| `--skip-check` | 사전 요구사항 검사 건너뛰기 |

## tmux 화면 구성

```
┌──────────────────┬──────────────────┐
│  🔵 Claude       │  🟢 Codex        │
│  (추론/설계)     │  (코드/분석)     │
├──────────────────┼──────────────────┤
│  🔴 Gemini       │  💬 Chat Input   │
│  (리서치/UI)     │  (사용자 입력)   │
└──────────────────┴──────────────────┘
```

각 AI pane 상단에 현재 상태(대기중/실행중/완료)가 표시된다.

## 채팅 명령어

### 메시지 전송

| 방식 | 예시 | 설명 |
|------|------|------|
| `@mention` | `@claude 이 코드 리뷰해줘` | 특정 AI에게 전송 |
| 다중 mention | `@claude @codex 분석해줘` | 여러 AI에게 전송 |
| `@all` | `@all 의견 말해봐` | 모든 AI에게 전송 |
| 키워드 라우팅 | `이 버그 수정해줘` | 키워드 기반 자동 라우팅 |
| `"""` | 여러 줄 입력 후 `"""`로 제출 | 멀티라인 입력 모드 |

### 키워드 기반 스마트 라우팅

`@mention` 없이 메시지를 보내면 키워드로 자동 라우팅된다:

| AI | 키워드 |
|----|--------|
| Claude | 설계, 아키텍처, 리뷰, 분석, 추론, 계획 |
| Codex | 코드, 구현, 함수, 버그, 디버그, 테스트, 리팩토링 |
| Gemini | 검색, 리서치, 문서, 프론트엔드, UI, CSS, React |

매칭 없으면 Claude로 기본 라우팅.

### 슬래시 명령어

| 명령어 | 설명 |
|--------|------|
| `/task <설명>` | 자동 오케스트레이션 (계획→배정→탐색→실행→합성) |
| `/batch <주제>` | AI-to-AI 토론 (자동 수렴 감지) |
| `/synth` | 모든 AI pane 캡처 후 Claude가 합성 |
| `/autosynth` | 자동 합성 ON/OFF 토글 |
| `/events [n]` | 최근 이벤트 스트림 표시 (기본: 20개) |
| `/models` | 활성 AI 모델 목록 |
| `/route` | 스마트 라우팅 키워드 표시 |
| `/history` | 대화 내역 표시 |
| `/sessions` | 과거 세션 목록 |
| `/clear` | 대화 로그 초기화 |
| `/help` | 도움말 표시 |
| `/quit` | 종료 |

## 핵심 기능

### 1. `/task` — 자동 작업 오케스트레이션

AI 3개가 자동으로 역할을 나눠 작업을 수행한다.

```
Phase 1   — Planning:   각 AI가 계획안 작성 (병렬)
Phase 2   — Assign:     Claude가 역할 배정 → 사용자 확인
            Todo:       shared/todo.md 체크리스트 자동 생성
Phase 2.5 — Discovery:  각 AI가 구현 전 코드베이스 탐색
Phase 3   — Execute:    각 AI가 자신의 pane에서 작업 수행
            Synthesize: Claude가 결과를 통합
```

사용 예:
```
/task 사용자 인증 시스템을 구현해줘
```

### 2. `/batch` — AI 토론 (자동 수렴)

AI들이 주제에 대해 자유 토론하고, 합의에 도달하면 자동 종료된다.

```
Round 1 — 각 AI가 초기 의견 제시
Round 2+ — 다른 AI 의견 분석 후 반응 (Think/Reflection 패턴)
수렴 체크 — Claude가 합의 여부 판단
합성 — 최종 결론 도출
```

사용 예:
```
/batch React vs Vue vs Svelte 중 어떤 프레임워크가 좋을까?
```

### 3. 자동 합성 (`/autosynth`)

2개 이상의 AI에게 메시지를 보내면, 응답 완료 후 Claude가 자동으로 종합한다.
`/autosynth`로 ON/OFF 전환 가능.

### 4. 컨텍스트 자동 관리

각 AI의 컨텍스트 사용량을 주기적으로 모니터링한다.
- **경고**: 80K 문자 초과 시 알림
- **자동 리셋**: 150K 문자 초과 시 대화 요약 후 세션 재시작

## 적용된 AI 도구 패턴

Manus, Devin, Cursor 등 주요 AI 도구의 검증된 패턴 6가지를 적용했다.

### 패턴 1: 공유 체크리스트 (Manus/Cursor)

`/task` 실행 시 `shared/<session>/todo.md`에 체크리스트가 자동 생성된다.
각 AI에게 "todo.md를 참조하라"는 지시가 함께 전달되어, 전체 작업 맥락을 공유한다.

### 패턴 2: Event Stream 로깅 (Manus)

모든 Phase 진행, 메시지 전송, 오류를 `events.jsonl`에 JSONL 형식으로 기록한다.
`/events` 명령으로 최근 이벤트를 테이블로 확인할 수 있다.

```
/events      # 최근 20개
/events 50   # 최근 50개
```

### 패턴 3: Discovery 단계 (Devin)

`/task`의 Phase 2와 3 사이에 "Phase 2.5: Discovery"가 자동 삽입된다.
각 AI가 구현 전에 관련 파일, 함수, 의존성을 먼저 파악하여 실수를 줄인다.

### 패턴 4: Think/Reflection (Devin)

`/batch` 토론의 Round 2부터 AI들이 응답 전에 다른 AI의 논점을 분석한다:
1. 각 AI의 가장 강한 포인트는?
2. 동의/반대하는 부분과 이유는?
3. 빠진 관점은?

이 분석을 기반으로 더 깊이 있는 토론이 이루어진다.

### 패턴 5: 구조화된 진행 알림 (Cursor/Manus)

각 Phase 진행 시 Rich 포맷의 컬러 상태 메시지가 표시된다.
모든 알림은 Event Stream에도 자동 기록된다.

### 패턴 6: 코드 컨벤션 감지 (Devin/Cursor)

작업 지시와 팀 컨텍스트에 "기존 코드 스타일을 따르라"는 지시가 포함된다.
AI들이 프로젝트의 네이밍, 포맷팅, 패턴을 자동으로 준수한다.

## 배치 모드 라운드 프로토콜

`--no-tmux` 모드에서는 4단계 라운드로 진행된다:

```
Round 1: 계획 (Plan)      — 각 AI가 계획안 작성
Round 2: 리뷰 (Review)    — 다른 AI 계획을 검토/평가
Round 3: 수정 (Revise)    — 피드백 반영하여 수정
Round 4: 종합 (Synthesize) — Claude가 최종안 통합
```

## 세션 데이터

모든 세션 데이터는 `shared/<timestamp>/`에 저장된다:

```
shared/2025-01-15_143022/
├── session.json           # 세션 메타데이터
├── chat.jsonl             # 대화 로그
├── events.jsonl           # 이벤트 스트림 (Phase/메시지/오류)
├── shared_context.json    # AI 간 공유 컨텍스트
├── todo.md                # /task 체크리스트
├── orch_plan_*.md         # /task Phase 1 계획
├── orch_assign.md         # /task Phase 2 역할 배정
├── orch_final.md          # /task Phase 3 최종 합성
├── batch_r*_*.md          # /batch 라운드별 응답
├── batch_synthesis.md     # /batch 최종 합성
└── final_report.md        # 배치 모드 최종 보고서
```

## 파일 구조

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
└── tests/              # 테스트
    ├── test_config_validation.py
    ├── test_round_manager_flow.py
    └── test_round_manager_prompts.py
```

## AI 모델별 역할

| AI | 역할 | 주요 강점 |
|----|------|----------|
| Claude | 추론/설계 | 복잡한 추론, 아키텍처, 코드 리뷰, 계획 |
| Codex | 코드/분석 | 코드 생성, 빠른 반복, 디버깅, 테스트 |
| Gemini | 리서치/UI | 검색, 긴 컨텍스트, 프론트엔드, 문서화 |
