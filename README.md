# Multi-AI Team

Claude + Codex + Gemini가 tmux 분할 화면에서 협업하는 시스템.

## 설치 요구사항

WSL 환경:
```bash
# tmux
sudo apt install tmux

# AI CLIs (이미 설치됨)
npm install -g @anthropic-ai/claude-code
npm install -g @openai/codex
npm install -g @google/gemini-cli
```

## 사용법

### tmux 모드 (추천 - 시각적 협업)
```bash
# WSL tmux 세션 안에서 실행
wsl tmux
python multi_ai_team/run.py "로그인 시스템을 설계해줘"
```

### 배치 모드 (tmux 없이)
```bash
python multi_ai_team/run.py --no-tmux "로그인 시스템을 설계해줘"
```

## 작동 방식

```
Round 1: 계획 (Plan)
  Claude, Codex, Gemini가 각자 계획안 작성

Round 2: 리뷰 (Review)
  각 AI가 다른 AI들의 계획을 검토/평가

Round 3: 수정 (Revise)
  리뷰 피드백을 반영하여 계획 수정

Round 4: 종합 (Synthesize)
  Claude가 모든 수정안을 통합하여 최종안 도출
```

## tmux 화면 구성

```
┌──────────────────┬──────────────────┐
│  🔵 Claude       │  🟢 Codex        │
│  (추론/설계)     │  (코드/분석)     │
├──────────────────┼──────────────────┤
│  🔴 Gemini       │  📋 Chat Log     │
│  (리서치/UI)     │  (소통 내역)     │
└──────────────────┴──────────────────┘
```

## 파일 구조

```
multi_ai_team/
├── run.py              # 메인 실행 스크립트
├── config.py           # 설정 (모델, 라운드, 타임아웃)
├── tmux_manager.py     # tmux 세션/pane 관리
├── ai_worker.py        # AI CLI 실행 및 결과 수집
├── round_manager.py    # 멀티라운드 프로토콜 관리
└── README.md           # 이 파일

실행 시 생성:
shared/
├── chat.jsonl          # AI간 소통 로그
├── round_*_*.txt       # 각 라운드별 AI 응답
└── final_report.md     # 최종 통합 보고서
```
