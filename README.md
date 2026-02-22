# AI DevOps Chatbot (Advanced Python)

Python-only autonomous DevOps agent with a chatbot interface and self-healing deployment logic.

## What Is Advanced Now

- Multi-step healing in a single cycle (`--max-actions`)
- Safety policy guardrails:
  - per-action cooldown
  - per-action rate limit (5-minute window)
- Severity classification (`low`, `medium`, `high`, `critical`)
- Rich cycle reports:
  - risk score before/after
  - risk delta
  - ranked candidate actions with score + block reason
  - per-step execution trace
- Diagnose mode (`dry-run`) without taking action

## Core Flow

The agent loop is:
1. Observe current state
2. Diagnose findings
3. Plan candidate actions
4. Rank actions using priority + historical success
5. Enforce policy (cooldown/rate-limit)
6. Execute best allowed action
7. Verify improvement with risk scoring
8. Persist memory for future decisions

## Files

- `ai_devops_agent.py`: main advanced agent + chatbot CLI
- `deployment_state.json`: runtime service state
- `agent_memory.json`: action stats, action history, recent reports

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
- `quit`

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
```

## Notes

- No external dependencies required.
- Policy guardrails can intentionally block repeated actions to avoid remediation thrashing.
- The mock platform can be replaced with real integrations (Kubernetes, CI/CD, observability) while keeping the agent logic.
