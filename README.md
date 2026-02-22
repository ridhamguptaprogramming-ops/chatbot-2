# AI DevOps Chatbot (Advanced Python)

Python-only autonomous DevOps agent with a chatbot interface and self-healing deployment logic.

## What Is Advanced Now

- Multi-step healing in a single cycle (`--max-actions`)
- Safety policy guardrails:
  - per-action cooldown
  - per-action rate limit (5-minute window)
- Severity classification (`low`, `medium`, `high`, `critical`)
- Exploration-aware action scoring (balances reliable actions vs learning)
- Rich cycle reports:
  - risk score before/after
  - risk delta
  - ranked candidate actions with score + block reason
  - per-step execution trace
- Diagnose mode (`dry-run`) without taking action
- Configurable policy/scoring via external JSON (`--config-file`)
- Built-in benchmark mode for reliability/performance metrics
- Auto-tuning mode that searches configuration space and selects the best policy/scoring profile
- Analytics intelligence over historical reports:
  - stability score (0-100)
  - risk/error/latency trend slopes
  - policy-block impact ratio
  - actionable recommendation

## Core Flow

The agent loop is:
1. Observe current state
2. Diagnose findings
3. Plan candidate actions
4. Rank actions using priority + historical success
5. Add exploration bonus for less-tested actions
6. Enforce policy (cooldown/rate-limit)
7. Execute best allowed action
8. Verify improvement with risk scoring
9. Persist memory for future decisions

## Files

- `ai_devops_agent.py`: main advanced agent + chatbot CLI
- `server.py`: Python backend API + persistent chat history
- `index.html`: frontend UI connected to backend endpoints
- `deployment_state.json`: runtime service state
- `agent_memory.json`: action stats, action history, recent reports
- `chat_history.json`: persisted chat conversation messages

## Run

```bash
python3 ai_devops_agent.py chat
```

Chat commands:
- `status`
- `diagnose`
- `simulate`
- `heal`
- `auto 5`
- `auto 5 3` (5 cycles, max 3 actions/cycle)
- `memory`
- `report`
- `analytics`
- `quit`

## Python Backend (Persistent Data)

Run backend server:
```bash
python3 server.py --host 127.0.0.1 --port 8000
```

Open UI:
- `http://127.0.0.1:8000/`

If your HTML is served from another local port and you see `Backend not reachable: Request failed (404)`:
- run backend on `127.0.0.1:8000`
- open UI with `?api=http://127.0.0.1:8000`
  - example: `http://127.0.0.1:5500/index.html?api=http://127.0.0.1:8000`

Backend saves data to:
- `deployment_state.json`
- `agent_memory.json`
- `chat_history.json`

API routes:
- `GET /api/state` - current deployment state snapshot
- `GET /api/analytics?window=40` - advanced analytics summary from recent reports
- `GET /api/history?limit=150` - saved chat messages
- `POST /api/chat` - process chatbot command and save messages
- `POST /api/history/clear` - clear saved chat history
- `GET /health` - health check
- CORS enabled for local frontend/backends on different ports

## CLI Commands

Status:
```bash
python3 ai_devops_agent.py status
python3 ai_devops_agent.py status --simulate
```

Diagnose only (no actions executed):
```bash
python3 ai_devops_agent.py diagnose --simulate --max-actions 3
```

Single healing cycle:
```bash
python3 ai_devops_agent.py heal --simulate --max-actions 3
python3 ai_devops_agent.py heal --dry-run
```

Autonomous loop:
```bash
python3 ai_devops_agent.py loop --cycles 10 --interval 2 --simulate --max-actions 2
```

Memory and reports:
```bash
python3 ai_devops_agent.py memory
python3 ai_devops_agent.py report
python3 ai_devops_agent.py report --json
python3 ai_devops_agent.py analytics
python3 ai_devops_agent.py analytics --window 80 --json
```

Benchmark:
```bash
python3 ai_devops_agent.py benchmark --episodes 20 --cycles 8 --max-actions 2
python3 ai_devops_agent.py benchmark --json
```

Autotune:
```bash
python3 ai_devops_agent.py autotune --trials 12 --episodes 16 --cycles 8 --max-actions 2
python3 ai_devops_agent.py autotune --json
python3 ai_devops_agent.py autotune --write-config ./best_agent_config.json
```

Config:
```bash
python3 ai_devops_agent.py config
python3 ai_devops_agent.py --config-file ./agent_config.json config
python3 ai_devops_agent.py --config-file ./agent_config.json heal --simulate --max-actions 3
```

Example `agent_config.json`:
```json
{
  "base_priority": {
    "rollback": 100,
    "restart_service": 82
  },
  "cooldown_seconds": {
    "restart_service": 30
  },
  "rate_limit_per_5m": {
    "rollback": 1,
    "restart_service": 6
  },
  "severity_thresholds": {
    "medium": 40,
    "high": 80,
    "critical": 120
  },
  "action_penalty": {
    "rollback": 12
  },
  "exploration_strength": 2.8
}
```

## Notes

- No external dependencies required.
- Policy guardrails can intentionally block repeated actions to avoid remediation thrashing.
- The mock platform can be replaced with real integrations (Kubernetes, CI/CD, observability) while keeping the agent logic.
- Global flags (like `--config-file`) must be passed before subcommands.
- Benchmark and autotune both include a `quality_score` to compare policy/scoring quality numerically.
