# Multi-AI Team — 시스템 아키텍처

시스템 내부 동작 원리를 설명하는 기술 문서.

## 1. 시스템 아키텍처 개요

### 4-pane tmux 레이아웃

```
┌──────────────────┬──────────────────┐
│  Pane 0: Claude  │  Pane 1: Codex   │
│  (interactive)   │  (interactive)   │
├──────────────────┼──────────────────┤
│  Pane 2: Gemini  │  Pane 3: Chat    │
│  (interactive)   │  (chat_loop.py)  │
└──────────────────┴──────────────────┘
```

### 프로세스 구조

```
python run.py
  ├─ tmux 세션 생성 (tmux_manager.py)
  ├─ Pane 0-2: AI CLI 시작 (ai_worker.start_interactive)
  │   └─ 각 CLI는 팀 컨텍스트를 initial_prompt로 받아 시작
  └─ Pane 3: chat_loop.py (별도 프로세스)
      └─ 사용자 입력 → @mention 파싱 → 대상 pane에 메시지 전송
```

`run.py`가 tmux 세션을 만들고, 각 pane에서 AI CLI를 시작한 후, input pane에서 `chat_loop.py`를 실행한다. `chat_loop.py`는 별도 프로세스로 동작하며, `--session-dir` 인자를 통해 세션 디렉토리 경로를 공유받는다.

## 2. AI pane 입력 메커니즘

TUI 기반 AI CLI에 메시지를 전달하는 핵심 메커니즘. `ai_worker.py`에 구현되어 있다.

### 문제

tmux의 `send-keys -l`(literal mode)은 입력을 bracket paste 이스케이프 시퀀스(`\e[200~`...`\e[201~`)로 감싼다. ink/React 기반 TUI(Claude Code, Codex)는 이 시퀀스를 처리할 때 Enter 키를 삼키거나, 긴 텍스트를 잘라버리는 문제가 발생한다.

### 해결 과정

| 시도 | 방식 | 결과 |
|------|------|------|
| 1차 | `send-keys -l` + 딜레이 | 실패 — Enter가 TUI에서 무시됨 |
| 2차 | 40자 단위 청크 전송 | 부분 성공 — 긴 텍스트 잘림 발생 |
| 3차 | **hex 모드 (`send-keys -H`)** | 성공 — bracket paste 완전 우회 |

### 현재 구현: hex 모드 전송

`send_message_to_pane()` (`ai_worker.py:362`)의 동작:

```
1. 메시지를 단일 라인으로 정리 (줄바꿈 → 공백)
2. UTF-8 바이트로 변환 → 각 바이트를 hex 문자열로
3. 200바이트 청크 단위로 tmux send-keys -H 전송
   (청크 간 0.2초 딜레이)
4. TUI 렌더링 안정화 대기 (2.0초)
5. pane 내용 스냅샷 저장 (baseline)
6. Enter(0x0D)를 hex 모드로 전송
7. pane 내용 변화 확인 → 변화 없으면 최대 3회 재시도
   (시도마다 백오프: 1.0s + attempt × 1.0s)
```

### 초기 컨텍스트: 파일 기반 CLI 인자 전달

`start_interactive()` (`ai_worker.py:317`)에서 초기 팀 컨텍스트는 TUI 입력을 거치지 않고 CLI 시작 인자로 전달한다:

```bash
# 프롬프트를 임시 파일에 저장
cat > /tmp/_team_init_claude.txt

# CLI 시작 시 파일 내용을 인자로 전달
claude --dangerously-skip-permissions "$(cat /tmp/_team_init_claude.txt)"
```

이 방식은 tmux `send-keys`를 사용하지 않으므로 bracket paste 문제가 근본적으로 발생하지 않는다.

### 이후 메시지

초기 컨텍스트 이후의 모든 메시지는 hex 모드(`send_message_to_pane`)로 전송한다.

### 타이밍 상수 (`ai_worker.py:19-25`)

| 상수 | 값 | 설명 |
|------|-----|------|
| `HEX_CHUNK_BYTES` | 200 | 한 번에 전송하는 hex 바이트 수 |
| `HEX_CHUNK_DELAY_SEC` | 0.2 | 청크 간 대기 시간 |
| `POST_TEXT_SETTLE_SEC` | 2.0 | Enter 전 TUI 렌더링 대기 |
| `ENTER_BASE_DELAY_SEC` | 1.0 | 첫 Enter 전 대기 |
| `ENTER_RETRY_BACKOFF_SEC` | 1.0 | 재시도당 추가 대기 |
| `ENTER_VERIFY_WAIT_SEC` | 1.0 | Enter 후 검증 대기 |
| `MAX_ENTER_RETRIES` | 3 | Enter 최대 재시도 횟수 |

## 3. 작업 종료 감지 (Idle Detection)

`wait_for_all_panes_idle()` (`ai_worker.py:422`)은 AI가 응답을 완료했는지 감지한다.

### 2-Phase 접근

```
Phase 1: 대기 → 시작 감지
  pane 내용이 초기 스냅샷에서 변경될 때까지 대기
  (AI가 응답을 시작했음을 의미)

Phase 2: 시작 → 완료 감지
  pane 내용이 stable_secs(기본 5초) 동안 변하지 않을 때까지 대기
  (AI가 응답을 완료했음을 의미)
```

### 폴링 전략

- **지수 백오프**: `min_poll`(1.0s)에서 시작, 1.5배씩 증가, `max_poll`(5.0s)까지
- **활동 감지 시 리셋**: pane 내용이 변하면 즉시 `min_poll`로 복귀
- **타임아웃**: 기본 1800초 (30분)

### Pane 상태 표시

각 AI pane 상단에 현재 상태가 표시된다:

| 상태 | 의미 |
|------|------|
| 대기중 | 입력 대기 |
| 실행중 | AI가 응답 생성 중 |
| 완료 | 응답 완료 |
| 에러 | 타임아웃 또는 오류 |

## 4. 메시지 라우팅

`chat_loop.py`에서 사용자 메시지를 적절한 AI pane으로 전달하는 로직.

### @mention 파싱 (`parse_mentions`)

```
@claude 이 코드 리뷰해줘     → Claude에만 전송
@claude @codex 분석해줘      → Claude + Codex에 전송
@all 의견 말해봐             → 모든 AI에 전송
(mention 없음)               → 스마트 라우팅으로 분기
```

### 키워드 기반 스마트 라우팅 (`smart_route`)

`@mention`이 없으면 `config.py`의 `SMART_ROUTING_KEYWORDS`로 자동 라우팅한다:

| AI | 키워드 |
|------|------|
| Claude | 설계, 아키텍처, 리뷰, 분석, 추론, 계획, ... |
| Codex | 코드, 구현, 함수, 버그, 디버그, 테스트, 리팩토링, ... |
| Gemini | 검색, 리서치, 문서, 프론트엔드, UI, CSS, React, ... |

매칭 점수가 높은 상위 2개 모델이 선택되며, 매칭이 없으면 Claude로 기본 라우팅된다.

### 멀티라인 입력

`"""` 입력으로 멀티라인 모드에 진입하고, 다시 `"""`으로 제출한다.

## 5. /task 오케스트레이션 파이프라인

`orchestrator.py`의 `TaskOrchestrator` 클래스.

### Phase 1: Planning (병렬 배치)

각 AI에게 `ORCH_PLAN_PROMPT`를 전달하여 계획안을 작성하게 한다. `ThreadPoolExecutor`로 병렬 실행하고, `run_ai_cli`(배치 모드)로 결과를 수집한다.

### Phase 2: Assignment

Claude가 모든 계획안을 종합하여 `[model] instruction` 형식으로 역할을 배정한다. 사용자가 확인(`y/n`) 후 진행된다.

`_parse_assignments()`는 `[model] instruction` 패턴을 정규식으로 파싱하며, 멀티라인 지시도 지원한다. 파싱 실패 시 모든 모델에 동일한 지시를 할당하는 폴백이 있다.

### Phase 2: Todo

역할 배정 결과로 `shared/<session>/todo.md` 체크리스트를 자동 생성한다.

### Phase 2.5: Discovery

각 AI의 interactive pane에 `ORCH_DISCOVER_PROMPT`를 전송하여 구현 전 코드베이스를 탐색하게 한다. `wait_for_all_panes_idle()`로 완료를 대기한다 (타임아웃 120초).

### Phase 3: Execute + Synthesize

1. 각 AI pane에 최종 지시 전송 (`send_message_to_pane`)
2. `wait_for_all_panes_idle()`로 모든 AI 완료 대기 (타임아웃 30분)
3. 각 pane 결과를 캡처하여 Claude가 `ORCH_FINAL_PROMPT`로 합성
4. 합성 결과를 모든 AI pane에 브로드캐스트 (`_broadcast_result_to_panes`)

### 결과 브로드캐스트

`_broadcast_result_to_panes()` (`chat_loop.py:105`)는 완료된 작업의 요약(500자 이내)과 세션 디렉토리 경로를 모든 AI pane에 전송한다. 이를 통해 각 AI가 전체 작업 결과를 참조할 수 있다.

## 6. /batch AI-to-AI 토론

`orchestrator.py`의 `BatchDiscussion` 클래스.

### 라운드 구조

```
Round 1:  초기 의견 (BATCH_OPEN_PROMPT)
          각 AI가 자신의 관점에서 의견 제시

Round 2+: Think/Reflection 패턴 (BATCH_REPLY_PROMPT)
          1. 각 AI의 가장 강한 포인트는?
          2. 동의/반대하는 부분과 이유는?
          3. 빠진 관점은?
          → 이 분석을 기반으로 응답

수렴 판단: Claude가 CONVERGED / NOT_CONVERGED 판정
          (BATCH_CONSENSUS_PROMPT)

최대 5라운드 후 자동 합성 (BATCH_SYNTHESIS_PROMPT)
```

### 수렴 판단 로직

`_check_consensus()`는 Claude의 응답에서 `CONVERGED`/`NOT_CONVERGED` 키워드를 찾는다. Round 2부터 매 라운드 종료 시 확인하며, 수렴되지 않은 경우 이유가 함께 반환된다.

### 결과 처리

토론 완료 후 합성 결과를 모든 AI pane에 브로드캐스트한다 (`/task`와 동일).

## 7. 공유 컨텍스트 시스템

`conversation.py`에 정의된 3개 클래스.

### SharedContext

AI 간 응답을 공유하는 구조화된 컨텍스트 매니저. JSON 파일(`shared_context.json`)로 영속화된다.

```python
ctx = SharedContext(work_dir)
ctx.add_response("claude", result, round_name="plan")

# 다른 AI의 응답만 추출 (자기 응답 제외)
context = ctx.build_context_for("codex", round_name="plan")
```

각 응답에는 모델명, 라벨, 라운드명, 타임스탬프, 에러 정보가 구조화되어 저장된다.

### EventStream

JSONL 형식의 이벤트 로그. 모든 Phase 진행, 메시지 전송, 오류를 기록한다.

```python
events = EventStream(work_dir)
events.log("status", detail="Task started", phase="start")
```

이벤트 타입은 `frozenset`으로 검증되며, 잘못된 타입이 전달되면 `ValueError`가 발생한다:
- `action`, `observation`, `message`, `status`, `error`

### ConversationLog

사용자 메시지 기록. JSONL 파일(`chat.jsonl`)로 저장되며, `/history` 명령으로 조회 가능.

### 세션 디렉토리

```
shared/<timestamp>/
├── session.json           # 세션 메타데이터
├── chat.jsonl             # 대화 로그
├── events.jsonl           # 이벤트 스트림
├── shared_context.json    # AI 간 공유 컨텍스트
├── todo.md                # /task 체크리스트
├── orch_plan_*.md         # /task Phase 1 계획
├── orch_assign.md         # /task Phase 2 역할 배정
├── orch_final.md          # /task Phase 3 최종 합성
├── batch_r*_*.md          # /batch 라운드별 응답
└── batch_synthesis.md     # /batch 최종 합성
```

## 8. 컨텍스트 자동 관리

`chat_loop.py`의 `run_chat_loop()`에서 주기적으로 각 AI의 컨텍스트 크기를 관리한다.

### 모니터링

- `CONTEXT_CHECK_INTERVAL` = 5 메시지마다 체크
- `capture_pane_content(pane, lines=500)`으로 pane 내용 캡처
- 문자 수 기준으로 임계값 비교

### 임계값

| 임계값 | 문자 수 | 토큰(추정) | 동작 |
|--------|---------|-----------|------|
| 경고 | 80,000 | ~20K | 사용자에게 알림 |
| 자동 리셋 | 150,000 | ~37K | 요약 생성 → CLI 재시작 |

### 리셋 과정

```
1. Claude가 현재 대화 내용을 요약 (run_ai_cli 배치 모드)
   - 마지막 8000자를 CONTEXT_RESET_SUMMARY_PROMPT로 전달
   - 작업 상태, 결정 사항, 남은 작업에 집중

2. AI CLI 재시작 (restart_interactive)
   - /exit 명령 → 2초 대기 → clear → start_interactive
   - initial_prompt = 팀 컨텍스트 + "[Context Summary] " + 요약
   - 파일 기반 인자 전달로 bracket paste 문제 우회

3. 문자 카운터 리셋 (context_chars[model] = 0)
```

## 9. Gemini/Codex CLI 특이사항

### Gemini CLI

```python
"args": ["--yolo", "-p"],           # 배치 모드
"interactive_args": ["--yolo"],     # 대화형 모드
```

- `--yolo`: 확인 없이 실행 (자동화 필수)
- `-p`: prompt 인자 — **반드시 마지막 위치**에 배치해야 함 (다음 인자를 프롬프트로 인식)
- 배치 모드에서는 `-p "prompt"` 형태로, 대화형에서는 `-p` 없이 실행

### Codex CLI

```python
"args": ["exec", "--skip-git-repo-check",
         "--dangerously-bypass-approvals-and-sandbox"],  # 배치 모드
"interactive_args": ["--dangerously-bypass-approvals-and-sandbox"],
```

- `exec`: 배치 모드 서브커맨드 (대화형에서는 불필요)
- `--skip-git-repo-check`: git 저장소 외부에서도 실행 허용
- `--dangerously-bypass-approvals-and-sandbox`: 승인/샌드박스 우회 (자동화 필수)

### Claude CLI

```python
"args": ["--print", "--dangerously-skip-permissions"],  # 배치 모드
"interactive_args": ["--dangerously-skip-permissions"],
```

- `--print`: 배치 모드 (결과를 stdout으로 출력)
- `--dangerously-skip-permissions`: 권한 확인 우회 (자동화 필수)
- 배치 모드에서 프롬프트는 `"$(cat file)"` 형태로 전달
