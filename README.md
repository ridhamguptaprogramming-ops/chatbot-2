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

Benchmark:
```bash
python3 ai_devops_agent.py benchmark --episodes 20 --cycles 8 --max-actions 2
python3 ai_devops_agent.py benchmark --json
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
