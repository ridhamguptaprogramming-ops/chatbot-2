from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class DeploymentState:
    service: str
    version: str
    status: str
    error_rate: float
    latency_ms: int
    cpu_percent: float
    memory_percent: float
    desired_replicas: int
    ready_replicas: int
    consecutive_failures: int


@dataclass
class Action:
    name: str
    reason: str
    params: Dict[str, object]


@dataclass
class ActionResult:
    action: str
    ok: bool
    details: str


class JsonFileStore:
    def __init__(self, path: Path, default_data: Dict[str, object]):
        self.path = path
        self.default_data = default_data

    def load(self) -> Dict[str, object]:
        if not self.path.exists():
            return json.loads(json.dumps(self.default_data))
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return json.loads(json.dumps(self.default_data))

    def save(self, payload: Dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, indent=2)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.path.parent),
                delete=False,
            ) as temp_file:
                temp_file.write(data)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = temp_file.name
            os.replace(temp_path, self.path)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)


class MockDeploymentPlatform:
    def __init__(self, state_store: JsonFileStore):
        self.state_store = state_store
        self.state_data = self.state_store.load()

    def _default_service_state(self, service: str) -> Dict[str, object]:
        return {
            "service": service,
            "version": "1.3.1",
            "status": "healthy",
            "error_rate": 0.5,
            "latency_ms": 140,
            "cpu_percent": 48.0,
            "memory_percent": 58.0,
            "desired_replicas": 3,
            "ready_replicas": 3,
            "consecutive_failures": 0,
        }

    def _save(self) -> None:
        self.state_store.save(self.state_data)

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    def _sanitize_record(self, rec: Dict[str, object]) -> None:
        rec["error_rate"] = self._clamp(float(rec.get("error_rate", 0.5)), 0.0, 100.0) # type: ignore
        rec["latency_ms"] = int(self._clamp(float(rec.get("latency_ms", 140)), 50.0, 5000.0)) # type: ignore
        rec["cpu_percent"] = self._clamp(float(rec.get("cpu_percent", 48.0)), 0.0, 100.0) # type: ignore
        rec["memory_percent"] = self._clamp(float(rec.get("memory_percent", 58.0)), 0.0, 100.0) # type: ignore
        rec["desired_replicas"] = int(self._clamp(float(rec.get("desired_replicas", 3)), 1.0, 10.0)) # type: ignore
        rec["ready_replicas"] = int(
            self._clamp(float(rec.get("ready_replicas", rec["desired_replicas"])), 0.0, float(rec["desired_replicas"])) # type: ignore
        )
        rec["consecutive_failures"] = int(self._clamp(float(rec.get("consecutive_failures", 0)), 0.0, 50.0)) # type: ignore

        status = str(rec.get("status", "healthy"))
        if status not in {"healthy", "degraded", "failed"}:
            status = "healthy"

        if rec["ready_replicas"] == 0:
            status = "failed"
        elif (
            rec["error_rate"] >= 1.5
            or rec["latency_ms"] >= 450
            or rec["cpu_percent"] >= 85.0
            or rec["memory_percent"] >= 90.0
            or rec["ready_replicas"] < rec["desired_replicas"]
            or rec["consecutive_failures"] > 0
        ):
            status = "degraded"
        else:
            status = "healthy"
        rec["status"] = status

    def ensure_service(self, service: str) -> None:
        services = self.state_data.setdefault("services", {})
        created = service not in services # type: ignore
        if created:
            services[service] = self._default_service_state(service) # type: ignore
        before = json.dumps(services[service], sort_keys=True) # type: ignore
        self._sanitize_record(services[service]) # type: ignore
        after = json.dumps(services[service], sort_keys=True) # type: ignore
        if created or before != after:
            self._save()

    def get_state(self, service: str) -> DeploymentState:
        self.ensure_service(service)
        record = self.state_data["services"][service] # type: ignore
        return DeploymentState(
            service=record["service"],
            version=record["version"],
            status=record["status"],
            error_rate=float(record["error_rate"]),
            latency_ms=int(record["latency_ms"]),
            cpu_percent=float(record["cpu_percent"]),
            memory_percent=float(record["memory_percent"]),
            desired_replicas=int(record["desired_replicas"]),
            ready_replicas=int(record["ready_replicas"]),
            consecutive_failures=int(record["consecutive_failures"]),
        )

    def _mutate_state(self, service: str, mutate_fn) -> None:
        self.ensure_service(service)
        mutate_fn(self.state_data["services"][service]) # type: ignore
        self._sanitize_record(self.state_data["services"][service]) # type: ignore
        self._save()

    def inject_random_event(self, service: str) -> str:
        self.ensure_service(service)
        state = self.state_data["services"][service] # type: ignore
        roll = random.random()

        if roll < 0.20:
            state["latency_ms"] = min(2500, int(state["latency_ms"]) + random.randint(200, 450))
            state["cpu_percent"] = min(100.0, float(state["cpu_percent"]) + random.uniform(8.0, 18.0))
            state["error_rate"] = min(20.0, float(state["error_rate"]) + random.uniform(1.2, 3.0))
            state["status"] = "degraded"
            event = "Traffic spike detected."
        elif roll < 0.35:
            state["memory_percent"] = min(100.0, float(state["memory_percent"]) + random.uniform(10.0, 20.0))
            state["error_rate"] = min(20.0, float(state["error_rate"]) + random.uniform(0.8, 2.4))
            state["status"] = "degraded"
            event = "Memory leak symptoms appeared."
        elif roll < 0.45:
            state["ready_replicas"] = max(0, int(state["ready_replicas"]) - 1)
            state["consecutive_failures"] = min(10, int(state["consecutive_failures"]) + 1)
            state["error_rate"] = min(25.0, float(state["error_rate"]) + random.uniform(2.0, 4.0))
            state["status"] = "failed" if int(state["ready_replicas"]) == 0 else "degraded"
            event = "Pod crash loop observed."
        else:
            state["error_rate"] = max(0.1, float(state["error_rate"]) - random.uniform(0.2, 0.8))
            state["latency_ms"] = max(110, int(state["latency_ms"]) - random.randint(15, 70))
            state["cpu_percent"] = max(30.0, float(state["cpu_percent"]) - random.uniform(2.0, 8.0))
            state["memory_percent"] = max(40.0, float(state["memory_percent"]) - random.uniform(1.0, 6.0))
            if int(state["ready_replicas"]) < int(state["desired_replicas"]) and random.random() < 0.35:
                state["ready_replicas"] += 1
            if float(state["error_rate"]) < 1.0 and int(state["ready_replicas"]) >= int(state["desired_replicas"]):
                state["status"] = "healthy"
                state["consecutive_failures"] = max(0, int(state["consecutive_failures"]) - 1)
            event = "System stabilized naturally."

        self._sanitize_record(state)
        self._save()
        return event

    def restart_service(self, service: str) -> ActionResult:
        def mutate(rec: Dict[str, object]) -> None:
            rec["error_rate"] = max(0.2, float(rec["error_rate"]) * 0.55) # type: ignore
            rec["latency_ms"] = max(120, int(int(rec["latency_ms"]) * 0.8)) # type: ignore
            rec["memory_percent"] = max(45.0, float(rec["memory_percent"]) * 0.75) # type: ignore
            rec["ready_replicas"] = max(int(rec["ready_replicas"]), int(rec["desired_replicas"]) - 1) # type: ignore
            rec["status"] = "degraded" if float(rec["error_rate"]) > 1.5 else "healthy"
            rec["consecutive_failures"] = max(0, int(rec["consecutive_failures"]) - 1) # type: ignore

        self._mutate_state(service, mutate)
        return ActionResult(action="restart_service", ok=True, details=f"{service} restarted.")

    def rollback(self, service: str, target_version: str) -> ActionResult:
        def mutate(rec: Dict[str, object]) -> None:
            rec["version"] = target_version
            rec["error_rate"] = max(0.2, float(rec["error_rate"]) * 0.35) # type: ignore
            rec["latency_ms"] = max(110, int(int(rec["latency_ms"]) * 0.7)) # type: ignore
            rec["cpu_percent"] = max(35.0, float(rec["cpu_percent"]) * 0.82) # type: ignore
            rec["memory_percent"] = max(45.0, float(rec["memory_percent"]) * 0.88) # type: ignore
            rec["ready_replicas"] = int(rec["desired_replicas"]) # type: ignore
            rec["status"] = "healthy"
            rec["consecutive_failures"] = 0

        self._mutate_state(service, mutate)
        return ActionResult(action="rollback", ok=True, details=f"{service} rolled back to {target_version}.")

    def scale(self, service: str, replicas: int) -> ActionResult:
        def mutate(rec: Dict[str, object]) -> None:
            capped = min(10, max(1, replicas))
            rec["desired_replicas"] = capped
            rec["ready_replicas"] = min(capped, int(rec["ready_replicas"]) + 1) # type: ignore
            rec["cpu_percent"] = max(32.0, float(rec["cpu_percent"]) * 0.86) # type: ignore
            rec["latency_ms"] = max(120, int(int(rec["latency_ms"]) * 0.82)) # type: ignore
            rec["status"] = "degraded" if float(rec["error_rate"]) > 2.0 else "healthy" # type: ignore

        self._mutate_state(service, mutate)
        return ActionResult(action="scale", ok=True, details=f"{service} scaled to {replicas} replicas.")

    def clear_cache(self, service: str) -> ActionResult:
        def mutate(rec: Dict[str, object]) -> None:
            rec["latency_ms"] = max(120, int(int(rec["latency_ms"]) * 0.86)) # type: ignore
            rec["error_rate"] = max(0.2, float(rec["error_rate"]) * 0.78) # type: ignore
            rec["status"] = "degraded" if float(rec["error_rate"]) > 1.8 else "healthy"

        self._mutate_state(service, mutate)
        return ActionResult(action="clear_cache", ok=True, details=f"{service} cache cleared.")


class AgentMemory:
    def __init__(self, memory_store: JsonFileStore):
        self.memory_store = memory_store
        self.data = self.memory_store.load()

    def _save(self) -> None:
        self.memory_store.save(self.data)

    def stats_for(self, action_name: str) -> Dict[str, int]:
        stats = self.data.setdefault("action_stats", {})
        return stats.setdefault(action_name, {"ok": 0, "failed": 0}) # type: ignore

    def record(self, action_name: str, ok: bool, report: Dict[str, object]) -> None:
        stats = self.stats_for(action_name)
        if ok:
            stats["ok"] += 1
        else:
            stats["failed"] += 1

        reports: List[Dict[str, object]] = self.data.setdefault("recent_reports", []) # type: ignore
        reports.append(report)
        if len(reports) > 30:
            del reports[0 : len(reports) - 30]
        self._save()

    def summary(self) -> str:
        stats = self.data.get("action_stats", {})
        if not stats:
            return "No historical actions recorded yet."
        parts = []
        for action, values in sorted(stats.items()): # type: ignore
            ok = values.get("ok", 0)
            failed = values.get("failed", 0)
            total = ok + failed
            rate = (ok / total * 100.0) if total else 0.0
            parts.append(f"{action}: ok={ok}, failed={failed}, success={rate:.1f}%")
        return " | ".join(parts)

    def latest_report(self) -> Optional[Dict[str, Any]]:
        reports = self.data.get("recent_reports", [])
        if not reports:
            return None
        return reports[-1] # type: ignore


class SelfHealingDevOpsAgent:
    BASE_PRIORITY = {
        "rollback": 95.0,
        "restart_service": 80.0,
        "scale": 70.0,
        "clear_cache": 55.0,
        "observe_only": 10.0,
    }

    def __init__(self, platform: MockDeploymentPlatform, memory: AgentMemory):
        self.platform = platform
        self.memory = memory

    def diagnose(self, state: DeploymentState) -> List[str]:
        findings: List[str] = []
        if state.status in {"degraded", "failed"}:
            findings.append(f"Service health is {state.status}.")
        if state.error_rate >= 5.0:
            findings.append(f"High error rate: {state.error_rate:.2f}%.")
        if state.latency_ms >= 900:
            findings.append(f"Latency is high: {state.latency_ms}ms.")
        if state.cpu_percent >= 85.0:
            findings.append(f"CPU pressure detected: {state.cpu_percent:.1f}%.")
        if state.memory_percent >= 88.0:
            findings.append(f"Memory pressure detected: {state.memory_percent:.1f}%.")
        if state.ready_replicas < state.desired_replicas:
            findings.append(
                f"Replica mismatch: ready={state.ready_replicas}, desired={state.desired_replicas}."
            )
        if not findings:
            findings.append("No critical issues detected.")
        return findings

    def plan_actions(self, state: DeploymentState, findings: List[str]) -> List[Action]:
        del findings
        actions: List[Action] = []

        if state.status == "failed" or state.ready_replicas == 0:
            target = self._previous_patch_version(state.version)
            actions.append(Action("rollback", "Service failed; roll back to last patch.", {"target_version": target}))

        if state.status == "degraded" and state.error_rate >= 1.5:
            actions.append(Action("restart_service", "Service degraded with elevated errors.", {}))

        if state.error_rate > 7.0:
            actions.append(Action("restart_service", "Error rate is too high.", {}))

        if state.memory_percent > 90.0:
            actions.append(Action("restart_service", "Possible memory leak pattern.", {}))

        if state.ready_replicas < state.desired_replicas and state.consecutive_failures >= 2:
            target = self._previous_patch_version(state.version)
            actions.append(Action("rollback", "Replica failures are persistent.", {"target_version": target}))

        if state.latency_ms > 1000 and state.cpu_percent > 80.0:
            actions.append(
                Action("scale", "High latency and CPU indicate capacity issue.", {"replicas": state.desired_replicas + 1})
            )

        if state.ready_replicas < state.desired_replicas and state.cpu_percent >= 70.0:
            actions.append(
                Action("scale", "Capacity is low while replicas are below desired.", {"replicas": state.desired_replicas + 1})
            )

        if state.error_rate > 3.0 and state.latency_ms > 650:
            actions.append(Action("clear_cache", "Cache corruption likely affecting latency and errors.", {}))

        if state.status == "degraded" and state.latency_ms > 450:
            actions.append(Action("clear_cache", "Latency regression in degraded mode.", {}))

        if state.status == "degraded" and not actions:
            actions.append(Action("restart_service", "Fallback remediation for degraded state.", {}))

        if not actions:
            return [Action("observe_only", "System is healthy, no remediation needed.", {})]

        deduped: Dict[str, Action] = {}
        for action in actions:
            key = f"{action.name}:{json.dumps(action.params, sort_keys=True)}"
            deduped[key] = action
        return list(deduped.values())

    def choose_action(self, candidates: List[Action]) -> Action:
        best = candidates[0]
        best_score = self._action_score(best)
        for action in candidates[1:]:
            score = self._action_score(action)
            if score > best_score:
                best = action
                best_score = score
        return best

    def _action_score(self, action: Action) -> float:
        base = self.BASE_PRIORITY.get(action.name, 25.0)
        stats = self.memory.stats_for(action.name)
        ok = stats.get("ok", 0)
        failed = stats.get("failed", 0)
        total = ok + failed
        success_rate = (ok / total) if total else 0.55
        return base + success_rate * 20.0 - failed * 0.35

    def execute(self, service: str, action: Action) -> ActionResult:
        if action.name == "rollback":
            target = str(action.params.get("target_version", "1.0.0"))
            return self.platform.rollback(service, target)
        if action.name == "restart_service":
            return self.platform.restart_service(service)
        if action.name == "scale":
            replicas = int(action.params.get("replicas", 3)) # type: ignore
            return self.platform.scale(service, replicas)
        if action.name == "clear_cache":
            return self.platform.clear_cache(service)
        return ActionResult(action="observe_only", ok=True, details="No action executed.")

    def run_healing_cycle(self, service: str, inject_event: bool = False) -> Dict[str, object]:
        if inject_event:
            self.platform.inject_random_event(service)
        before = self.platform.get_state(service)
        findings = self.diagnose(before)
        plan = self.plan_actions(before, findings)
        chosen = self.choose_action(plan)
        result = self.execute(service, chosen)
        after = self.platform.get_state(service)
        improved = self._is_improved(before, after)

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": service,
            "before": asdict(before),
            "findings": findings,
            "chosen_action": asdict(chosen),
            "action_result": asdict(result),
            "after": asdict(after),
            "improved": improved,
        }
        self.memory.record(chosen.name, result.ok and improved, report)
        return report

    def _risk_score(self, state: DeploymentState) -> float:
        replica_gap = max(0, state.desired_replicas - state.ready_replicas)
        status_penalty = 0.0
        if state.status == "degraded":
            status_penalty = 20.0
        elif state.status == "failed":
            status_penalty = 50.0

        return (
            state.error_rate * 5.0
            + (state.latency_ms / 40.0)
            + max(0.0, state.cpu_percent - 75.0) * 1.3
            + max(0.0, state.memory_percent - 80.0) * 1.0
            + replica_gap * 18.0
            + state.consecutive_failures * 5.0
            + status_penalty
        )

    def _is_improved(self, before: DeploymentState, after: DeploymentState) -> bool:
        return self._risk_score(after) <= self._risk_score(before) - 2.0 or after.status == "healthy"

    @staticmethod
    def _previous_patch_version(version: str) -> str:
        parts = version.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return "1.0.0"
        major, minor, patch = [int(part) for part in parts]
        if patch > 0:
            patch -= 1
        elif minor > 0:
            minor -= 1
            patch = 9
        elif major > 0:
            major -= 1
            minor = 9
            patch = 9
        return f"{major}.{minor}.{patch}"


class DevOpsChatbot:
    def __init__(self, agent: SelfHealingDevOpsAgent, platform: MockDeploymentPlatform, service: str):
        self.agent = agent
        self.platform = platform
        self.service = service

    def run(self) -> None:
        print("AI DevOps Agent Chatbot (Python only)")
        print("Type: status, heal, simulate, auto 5, memory, report, help, quit")

        while True:
            try:
                message = input("you> ").strip()
            except EOFError:
                print()
                return
            if not message:
                continue

            lowered = message.lower()
            if lowered in {"quit", "exit"}:
                print("bot> Session closed.")
                return
            if lowered == "help":
                print("bot> Commands: status | heal | simulate | auto <cycles> | memory | report | quit")
                continue
            if lowered == "status":
                print(f"bot> {format_state(self.platform.get_state(self.service))}")
                continue
            if lowered == "simulate":
                event = self.platform.inject_random_event(self.service)
                print(f"bot> Simulated event: {event}")
                print(f"bot> {format_state(self.platform.get_state(self.service))}")
                continue
            if lowered.startswith("auto"):
                parts = lowered.split()
                cycles = 3
                if len(parts) > 1 and parts[1].isdigit():
                    cycles = min(20, max(1, int(parts[1])))
                print(f"bot> Running {cycles} autonomous cycles...")
                for index in range(1, cycles + 1):
                    report = self.agent.run_healing_cycle(self.service, inject_event=True)
                    summary = summarize_report(report)
                    print(f"bot> Cycle {index}: {summary}")
                continue
            if lowered == "heal":
                report = self.agent.run_healing_cycle(self.service, inject_event=False)
                print(f"bot> {summarize_report(report)}")
                continue
            if lowered == "memory":
                print(f"bot> {self.agent.memory.summary()}")
                continue
            if lowered == "report":
                latest = self.agent.memory.latest_report()
                if latest is None:
                    print("bot> No report available yet. Run heal or auto first.")
                else:
                    print(f"bot> {format_detailed_report(latest)}")
                continue

            if any(word in lowered for word in ["deploy", "broken", "fix", "incident", "down"]):
                report = self.agent.run_healing_cycle(self.service, inject_event=True)
                print(f"bot> Triggered incident triage. {summarize_report(report)}")
                continue

            print("bot> I did not understand. Try: status, heal, simulate, auto 5, memory, help, quit")


def format_state(state: DeploymentState) -> str:
    return (
        f"{state.service} v{state.version} | status={state.status} | "
        f"errors={state.error_rate:.2f}% | latency={state.latency_ms}ms | "
        f"cpu={state.cpu_percent:.1f}% | mem={state.memory_percent:.1f}% | "
        f"replicas={state.ready_replicas}/{state.desired_replicas}"
    )


def summarize_report(report: Dict[str, object]) -> str:
    action = report["chosen_action"]["name"] # type: ignore
    reason = report["chosen_action"]["reason"] # type: ignore
    improved = report["improved"]
    after_state = DeploymentState(**report["after"]) # type: ignore
    return (
        f"Action={action}; reason={reason}; improved={improved}; "
        f"now {format_state(after_state)}"
    )


def format_detailed_report(report: Dict[str, Any]) -> str:
    before_state = DeploymentState(**report["before"])
    after_state = DeploymentState(**report["after"])
    action = report["chosen_action"]["name"]
    reason = report["chosen_action"]["reason"]
    improved = report["improved"]
    findings = "; ".join(report.get("findings", []))
    return (
        f"timestamp={report['timestamp']} | action={action} | improved={improved}\n"
        f"reason={reason}\n"
        f"findings={findings}\n"
        f"before={format_state(before_state)}\n"
        f"after={format_state(after_state)}"
    )


def build_agent(state_path: Path, memory_path: Path) -> tuple[SelfHealingDevOpsAgent, MockDeploymentPlatform]:
    state_store = JsonFileStore(
        state_path,
        default_data={
            "services": {},
        },
    )
    memory_store = JsonFileStore(
        memory_path,
        default_data={
            "action_stats": {},
            "recent_reports": [],
        },
    )
    platform = MockDeploymentPlatform(state_store)
    memory = AgentMemory(memory_store)
    return SelfHealingDevOpsAgent(platform, memory), platform


def run_loop(
    agent: SelfHealingDevOpsAgent,
    service: str,
    cycles: int,
    interval_seconds: float,
    inject_event: bool,
) -> None:
    for index in range(1, cycles + 1):
        report = agent.run_healing_cycle(service, inject_event=inject_event)
        print(f"[{index}/{cycles}] {summarize_report(report)}")
        if index < cycles:
            time.sleep(interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Python-only autonomous AI DevOps agent with self-healing deployment actions."
    )
    parser.add_argument("--service", default="payments-api", help="Target service name.")
    parser.add_argument("--state-file", default="deployment_state.json", help="Path to persisted deployment state.")
    parser.add_argument("--memory-file", default="agent_memory.json", help="Path to persisted agent memory.")
    parser.add_argument("--seed", type=int, default=7, help="Random seed for repeatable simulations.")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("chat", help="Interactive chatbot mode (default).")

    status_parser = subparsers.add_parser("status", help="Print current deployment state.")
    status_parser.add_argument("--simulate", action="store_true", help="Inject a random event before reading status.")

    heal_parser = subparsers.add_parser("heal", help="Run one self-healing cycle.")
    heal_parser.add_argument("--simulate", action="store_true", help="Inject a random event before healing.")

    loop_parser = subparsers.add_parser("loop", help="Run autonomous loop for multiple cycles.")
    loop_parser.add_argument("--cycles", type=int, default=10, help="Number of cycles.")
    loop_parser.add_argument("--interval", type=float, default=2.0, help="Seconds between cycles.")
    loop_parser.add_argument("--simulate", action="store_true", help="Inject random events each cycle.")

    subparsers.add_parser("memory", help="Show memory statistics from past actions.")
    report_parser = subparsers.add_parser("report", help="Show the latest healing report.")
    report_parser.add_argument("--json", action="store_true", help="Print full report as JSON.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    random.seed(args.seed)

    state_path = Path(args.state_file)
    memory_path = Path(args.memory_file)
    agent, platform = build_agent(state_path=state_path, memory_path=memory_path)
    platform.ensure_service(args.service)

    command = args.command or "chat"

    if command == "status":
        if args.simulate:
            event = platform.inject_random_event(args.service)
            print(f"Simulated event: {event}")
        print(format_state(platform.get_state(args.service)))
        return

    if command == "heal":
        report = agent.run_healing_cycle(args.service, inject_event=args.simulate)
        print(summarize_report(report))
        return

    if command == "loop":
        cycles = max(1, min(200, int(args.cycles)))
        interval = max(0.0, float(args.interval))
        run_loop(agent, args.service, cycles=cycles, interval_seconds=interval, inject_event=args.simulate)
        return

    if command == "memory":
        print(agent.memory.summary())
        return

    if command == "report":
        latest = agent.memory.latest_report()
        if latest is None:
            print("No report available yet. Run heal or loop first.")
            return
        if args.json:
            print(json.dumps(latest, indent=2))
        else:
            print(format_detailed_report(latest))
        return

    chatbot = DevOpsChatbot(agent, platform, args.service)
    chatbot.run()


if __name__ == "__main__":
    main()
