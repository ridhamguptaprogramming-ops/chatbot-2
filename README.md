# AI DevOps Chatbot (Python Only)

This project is a Python-only autonomous DevOps agent that behaves like a chatbot and can self-heal a deployment.

It implements a full control loop:
- `Observe` deployment state
- `Diagnose` issues
- `Plan` remediation actions
- `Act` on the best action
- `Verify` whether health improved
- `Learn` from outcomes for future decisions

No external libraries are required.

## 1) Project Goal

Build an AI DevOps bot that can:
- monitor service health
- detect deployment incidents
- choose remediation automatically
- execute self-healing actions
- learn which actions work best

## 2) What You Get in This Repo

- `ai_devops_agent.py`: complete autonomous agent + chatbot CLI
- `deployment_state.json`: generated at runtime, tracks service state
- `agent_memory.json`: generated at runtime, tracks historical action success

## 3) Architecture (Step-by-Step)

1. `State Layer`  
The agent tracks service metrics: error rate, latency, CPU, memory, replica health, status, and version.

2. `Platform Adapter`  
`MockDeploymentPlatform` simulates production failures and remediation actions:
- `restart_service`
- `rollback`
- `scale`
- `clear_cache`

3. `Reasoning Layer`  
`SelfHealingDevOpsAgent`:
- diagnoses conditions from metrics
- creates candidate actions
- scores actions using base priority + learned success rate
- executes best action

4. `Verification Layer`  
After each action, it computes a risk score before/after to decide if recovery improved.

5. `Learning Layer`  
The agent persists action outcomes and reuses them for better future action selection.

6. `Chatbot Layer`  
`DevOpsChatbot` gives natural CLI interaction:
- `status`
- `simulate`
- `heal`
- `auto 5`
- `memory`
- `help`
- `quit`

## 4) Run the Bot

```bash
python3 ai_devops_agent.py chat
```

Then type:
- `status`
- `simulate`
- `heal`
- `auto 5`
- `memory`

## 5) Non-Interactive Commands

Check state:
```bash
python3 ai_devops_agent.py status
```

Run one healing cycle:
```bash
python3 ai_devops_agent.py heal --simulate
```

Run autonomous loop:
```bash
python3 ai_devops_agent.py loop --cycles 10 --interval 2 --simulate
```

Show learned memory:
```bash
python3 ai_devops_agent.py memory
```

## 6) Why This Is Autonomous

It does not wait for manual instructions when running loop mode:
- observes incidents
- plans and executes remediation
- validates outcomes
- updates strategy with memory

## 7) Move from Mock to Real DevOps

Replace `MockDeploymentPlatform` methods with real integrations:
- Kubernetes API (`restart`, `scale`, health checks)
- deployment tool (`rollback` via Helm/Argo/Jenkins)
- observability APIs (Prometheus, Datadog, CloudWatch)
- incident systems (PagerDuty/Slack/Jira)

Keep the same agent logic, swap only platform actions.
