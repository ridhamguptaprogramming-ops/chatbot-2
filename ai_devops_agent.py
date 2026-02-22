from __future__ import annotations

import argparse
import json
import math
import os
import random
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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


@dataclass
class RankedAction:
    action: Action
    score: float
    blocked: bool
    block_reason: str


@dataclass
class AgentConfig:
    base_priority: Dict[str, float]
    cooldown_seconds: Dict[str, int]
    rate_limit_per_5m: Dict[str, int]
    severity_thresholds: Dict[str, float]
    action_penalty: Dict[str, float]
    exploration_strength: float

    @classmethod
    def default(cls) -> "AgentConfig":
        return cls(
            base_priority={
                "rollback": 95.0,
                "restart_service": 80.0,
                "scale": 70.0,
                "clear_cache": 55.0,
                "observe_only": 10.0,
            },
            cooldown_seconds={
                "rollback": 180,
                "restart_service": 45,
                "scale": 20,
                "clear_cache": 20,
                "observe_only": 0,
            },
            rate_limit_per_5m={
                "rollback": 1,
                "restart_service": 5,
                "scale": 8,
                "clear_cache": 8,
                "observe_only": 999,
            },
            severity_thresholds={
                "critical": 130.0,
                "high": 85.0,
                "medium": 45.0,
            },
            action_penalty={
                "rollback": 10.0,
                "restart_service": 2.0,
                "scale": 1.0,
                "clear_cache": 0.8,
                "observe_only": 0.0,
            },
            exploration_strength=3.2,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_priority": dict(self.base_priority),
            "cooldown_seconds": dict(self.cooldown_seconds),
            "rate_limit_per_5m": dict(self.rate_limit_per_5m),
            "severity_thresholds": dict(self.severity_thresholds),
            "action_penalty": dict(self.action_penalty),
            "exploration_strength": float(self.exploration_strength),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AgentConfig":
        base = cls.default()

        def _merge_float_map(default_map: Dict[str, float], raw_value: Any) -> Dict[str, float]:
            merged = dict(default_map)
            if isinstance(raw_value, dict):
                for key, value in raw_value.items():
                    try:
                        merged[str(key)] = float(value)
                    except (TypeError, ValueError):
                        continue
            return merged

        def _merge_int_map(default_map: Dict[str, int], raw_value: Any) -> Dict[str, int]:
            merged = dict(default_map)
            if isinstance(raw_value, dict):
                for key, value in raw_value.items():
                    try:
                        merged[str(key)] = int(value)
                    except (TypeError, ValueError):
                        continue
            return merged

        exploration = base.exploration_strength
        try:
            if "exploration_strength" in payload:
                exploration = max(0.0, float(payload["exploration_strength"]))
        except (TypeError, ValueError):
            exploration = base.exploration_strength

        return cls(
            base_priority=_merge_float_map(base.base_priority, payload.get("base_priority")),
            cooldown_seconds=_merge_int_map(base.cooldown_seconds, payload.get("cooldown_seconds")),
            rate_limit_per_5m=_merge_int_map(base.rate_limit_per_5m, payload.get("rate_limit_per_5m")),
            severity_thresholds=_merge_float_map(base.severity_thresholds, payload.get("severity_thresholds")),
            action_penalty=_merge_float_map(base.action_penalty, payload.get("action_penalty")),
            exploration_strength=exploration,
        )


def load_agent_config(config_file: Optional[Path]) -> AgentConfig:
    if config_file is None:
        return AgentConfig.default()
    try:
        payload = json.loads(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Config file not found: {config_file}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file: {config_file}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Config file root must be a JSON object.")
    return AgentConfig.from_dict(payload)


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

    def action_total(self, action_name: str) -> int:
        stats = self.stats_for(action_name)
        return int(stats.get("ok", 0)) + int(stats.get("failed", 0))

    def global_action_total(self) -> int:
        stats = self.data.get("action_stats", {})
        total = 0
        if isinstance(stats, dict):
            for values in stats.values():
                if isinstance(values, dict):
                    total += int(values.get("ok", 0)) + int(values.get("failed", 0))
        return total

    def _history(self) -> List[Dict[str, Any]]:
        return self.data.setdefault("action_history", []) # type: ignore

    @staticmethod
    def _parse_timestamp(raw_value: Any) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(str(raw_value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def record_action(self, service: str, action_name: str, ok: bool) -> None:
        stats = self.stats_for(action_name)
        if ok:
            stats["ok"] += 1
        else:
            stats["failed"] += 1

        history = self._history()
        history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "service": service,
                "action": action_name,
                "ok": ok,
            }
        )
        if len(history) > 800:
            del history[0 : len(history) - 800]
        self._save()

    def append_report(self, report: Dict[str, Any]) -> None:
        reports: List[Dict[str, Any]] = self.data.setdefault("recent_reports", []) # type: ignore
        reports.append(report)
        if len(reports) > 60:
            del reports[0 : len(reports) - 60]
        self._save()

    def record(self, action_name: str, ok: bool, report: Dict[str, object]) -> None:
        service = str(report.get("service", "unknown"))
        self.record_action(service=service, action_name=action_name, ok=ok)
        self.append_report(dict(report))

    def recent_action_count(self, service: str, action_name: str, within_seconds: int) -> int:
        history = self._history()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=within_seconds)
        total = 0
        for item in reversed(history):
            if item.get("service") != service:
                continue
            if item.get("action") != action_name:
                continue
            timestamp = self._parse_timestamp(item.get("timestamp"))
            if timestamp is None:
                continue
            if timestamp < cutoff:
                break
            total += 1
        return total

    def seconds_since_last_action(self, service: str, action_name: str) -> Optional[float]:
        history = self._history()
        now = datetime.now(timezone.utc)
        for item in reversed(history):
            if item.get("service") != service:
                continue
            if item.get("action") != action_name:
                continue
            timestamp = self._parse_timestamp(item.get("timestamp"))
            if timestamp is None:
                continue
            delta = now - timestamp
            return max(0.0, delta.total_seconds())
        return None

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
    def __init__(
        self,
        platform: MockDeploymentPlatform,
        memory: AgentMemory,
        config: Optional[AgentConfig] = None,
    ):
        self.platform = platform
        self.memory = memory
        self.config = config or AgentConfig.default()
        self.base_priority = dict(self.config.base_priority)
        self.action_cooldown_seconds = dict(self.config.cooldown_seconds)
        self.action_rate_limit_per_5m = dict(self.config.rate_limit_per_5m)
        self.severity_thresholds = dict(self.config.severity_thresholds)
        self.action_penalty = dict(self.config.action_penalty)
        self.exploration_strength = float(self.config.exploration_strength)

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

    def classify_severity(self, state: DeploymentState) -> str:
        risk = self._risk_score(state)
        critical_threshold = float(self.severity_thresholds.get("critical", 130.0))
        high_threshold = float(self.severity_thresholds.get("high", 85.0))
        medium_threshold = float(self.severity_thresholds.get("medium", 45.0))
        if state.status == "failed" or risk >= critical_threshold:
            return "critical"
        if risk >= high_threshold:
            return "high"
        if risk >= medium_threshold:
            return "medium"
        return "low"

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

        if state.status == "degraded" and state.error_rate >= 2.5 and state.latency_ms >= 300:
            actions.append(Action("clear_cache", "Moderate degradation with elevated latency/error trend.", {}))

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

    def _policy_block_reason(self, service: str, action_name: str) -> str:
        if action_name == "observe_only":
            return ""

        cooldown = self.action_cooldown_seconds.get(action_name, 0)
        if cooldown > 0:
            since_last = self.memory.seconds_since_last_action(service, action_name)
            if since_last is not None and since_last < cooldown:
                wait_for = int(cooldown - since_last)
                return f"cooldown_active_wait_{wait_for}s"

        max_in_window = self.action_rate_limit_per_5m.get(action_name, 999)
        recent_count = self.memory.recent_action_count(service, action_name, within_seconds=300)
        if recent_count >= max_in_window:
            return f"rate_limited_5m_{recent_count}/{max_in_window}"

        return ""

    def rank_actions(self, service: str, state: DeploymentState, candidates: List[Action]) -> List[RankedAction]:
        severity = self.classify_severity(state)
        severity_bonus = {"low": 0.0, "medium": 4.0, "high": 8.0, "critical": 12.0}[severity]
        ranked: List[RankedAction] = []

        for action in candidates:
            score = self._action_score(action)
            if action.name != "observe_only":
                score += severity_bonus
            block_reason = self._policy_block_reason(service=service, action_name=action.name)
            ranked.append(
                RankedAction(
                    action=action,
                    score=score,
                    blocked=bool(block_reason),
                    block_reason=block_reason,
                )
            )

        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked

    def choose_action(self, ranked_actions: List[RankedAction]) -> Action:
        for ranked in ranked_actions:
            if not ranked.blocked:
                return ranked.action
        return Action("observe_only", "All remediation actions blocked by safety policy.", {})

    def _action_score(self, action: Action) -> float:
        base = self.base_priority.get(action.name, 25.0)
        stats = self.memory.stats_for(action.name)
        ok = stats.get("ok", 0)
        failed = stats.get("failed", 0)
        attempts = ok + failed
        success_rate = (ok / attempts) if attempts else 0.55
        global_attempts = max(1, self.memory.global_action_total())
        exploration_bonus = self.exploration_strength * math.sqrt(math.log(global_attempts + 1) / (attempts + 1))
        penalty = float(self.action_penalty.get(action.name, 0.0))
        return base + success_rate * 20.0 + exploration_bonus - failed * 0.35 - penalty

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

    def run_healing_cycle(
        self,
        service: str,
        inject_event: bool = False,
        max_actions: int = 1,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        max_actions = max(1, min(5, int(max_actions)))
        event = ""
        if inject_event:
            event = self.platform.inject_random_event(service)

        before = self.platform.get_state(service)
        initial_risk = self._risk_score(before)
        severity = self.classify_severity(before)
        current_state = before

        steps: List[Dict[str, Any]] = []
        for step_number in range(1, max_actions + 1):
            findings = self.diagnose(current_state)
            candidates = self.plan_actions(current_state, findings)
            ranked = self.rank_actions(service=service, state=current_state, candidates=candidates)
            chosen = self.choose_action(ranked)

            if dry_run:
                action_result = ActionResult(
                    action="observe_only",
                    ok=True,
                    details="Dry-run mode, no action executed.",
                )
                next_state = current_state
                step_improved = False
            else:
                action_result = self.execute(service, chosen)
                next_state = self.platform.get_state(service)
                step_improved = self._is_improved(current_state, next_state)
                if chosen.name != "observe_only":
                    self.memory.record_action(
                        service=service,
                        action_name=chosen.name,
                        ok=action_result.ok and step_improved,
                    )

            steps.append(
                {
                    "step": step_number,
                    "state_before": asdict(current_state),
                    "findings": findings,
                    "ranked_candidates": [
                        {
                            "name": ranked_action.action.name,
                            "reason": ranked_action.action.reason,
                            "params": ranked_action.action.params,
                            "score": round(ranked_action.score, 3),
                            "blocked": ranked_action.blocked,
                            "block_reason": ranked_action.block_reason,
                        }
                        for ranked_action in ranked
                    ],
                    "chosen_action": asdict(chosen),
                    "action_result": asdict(action_result),
                    "state_after": asdict(next_state),
                    "step_improved": step_improved,
                }
            )

            current_state = next_state
            if dry_run or chosen.name == "observe_only":
                break
            if current_state.status == "healthy" and step_improved:
                break

        after = current_state
        final_risk = self._risk_score(after)
        improved = self._is_improved(before, after)
        first_findings = steps[0]["findings"] if steps else []
        last_step = steps[-1] if steps else {}
        primary_step = {}
        for step in steps:
            chosen_name = step.get("chosen_action", {}).get("name")
            if chosen_name and chosen_name != "observe_only":
                primary_step = step
                break
        if not primary_step:
            primary_step = last_step

        report: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": service,
            "event": event,
            "severity": severity,
            "before": asdict(before),
            "initial_risk_score": round(initial_risk, 3),
            "final_risk_score": round(final_risk, 3),
            "risk_delta": round(initial_risk - final_risk, 3),
            "dry_run": dry_run,
            "steps": steps,
            "findings": first_findings,
            "chosen_action": primary_step.get("chosen_action", asdict(Action("observe_only", "No action selected.", {}))),
            "action_result": primary_step.get(
                "action_result",
                asdict(ActionResult(action="observe_only", ok=True, details="No action executed.")),
            ),
            "terminal_action": last_step.get("chosen_action", asdict(Action("observe_only", "No action selected.", {}))),
            "after": asdict(after),
            "improved": improved,
        }
        self.memory.append_report(report)
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
        print("Type: status, diagnose, heal, simulate, auto 5, memory, report, help, quit")

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
                print("bot> Commands: status | diagnose | heal | simulate | auto <cycles> [max_actions] | memory | report | quit")
                continue
            if lowered == "status":
                print(f"bot> {format_state(self.platform.get_state(self.service))}")
                continue
            if lowered == "diagnose":
                report = self.agent.run_healing_cycle(
                    self.service,
                    inject_event=False,
                    max_actions=2,
                    dry_run=True,
                )
                print(f"bot> {format_detailed_report(report)}")
                continue
            if lowered == "simulate":
                event = self.platform.inject_random_event(self.service)
                print(f"bot> Simulated event: {event}")
                print(f"bot> {format_state(self.platform.get_state(self.service))}")
                continue
            if lowered.startswith("auto"):
                parts = lowered.split()
                cycles = 3
                max_actions = 2
                if len(parts) > 1 and parts[1].isdigit():
                    cycles = min(20, max(1, int(parts[1])))
                if len(parts) > 2 and parts[2].isdigit():
                    max_actions = min(5, max(1, int(parts[2])))
                print(f"bot> Running {cycles} autonomous cycles...")
                for index in range(1, cycles + 1):
                    report = self.agent.run_healing_cycle(
                        self.service,
                        inject_event=True,
                        max_actions=max_actions,
                    )
                    summary = summarize_report(report)
                    print(f"bot> Cycle {index}: {summary}")
                continue
            if lowered == "heal":
                report = self.agent.run_healing_cycle(
                    self.service,
                    inject_event=False,
                    max_actions=2,
                )
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
                report = self.agent.run_healing_cycle(
                    self.service,
                    inject_event=True,
                    max_actions=2,
                )
                print(f"bot> Triggered incident triage. {summarize_report(report)}")
                continue

            print("bot> I did not understand. Try: status, diagnose, heal, simulate, auto 5, memory, help, quit")


def format_state(state: DeploymentState) -> str:
    return (
        f"{state.service} v{state.version} | status={state.status} | "
        f"errors={state.error_rate:.2f}% | latency={state.latency_ms}ms | "
        f"cpu={state.cpu_percent:.1f}% | mem={state.memory_percent:.1f}% | "
        f"replicas={state.ready_replicas}/{state.desired_replicas}"
    )


def summarize_report(report: Dict[str, Any]) -> str:
    chosen = report.get("chosen_action", {})
    action = chosen.get("name", "observe_only")
    reason = chosen.get("reason", "")
    improved = report.get("improved")
    severity = report.get("severity", "unknown")
    risk_delta = float(report.get("risk_delta", 0.0))
    steps = report.get("steps", [])
    after_state = DeploymentState(**report["after"])
    return (
        f"severity={severity}; steps={len(steps)}; action={action}; "
        f"risk_delta={risk_delta:.2f}; improved={improved}; reason={reason}; "
        f"now {format_state(after_state)}"
    )


def format_detailed_report(report: Dict[str, Any]) -> str:
    before_state = DeploymentState(**report["before"])
    after_state = DeploymentState(**report["after"])
    action = report["chosen_action"]["name"]
    reason = report["chosen_action"]["reason"]
    improved = report["improved"]
    findings = "; ".join(report.get("findings", []))
    steps = report.get("steps", [])
    step_summaries = []
    for step in steps:
        chosen_name = step.get("chosen_action", {}).get("name", "observe_only")
        improved_flag = step.get("step_improved")
        ranked = step.get("ranked_candidates", [])
        ranked_preview = ", ".join(
            f"{item.get('name')}[{item.get('score')}]"
            + ("(blocked)" if item.get("blocked") else "")
            for item in ranked[:3]
        )
        step_summaries.append(
            f"step={step.get('step')} chosen={chosen_name} step_improved={improved_flag} ranked={ranked_preview}"
        )

    return (
        f"timestamp={report['timestamp']} | severity={report.get('severity')} | action={action} | improved={improved}\n"
        f"reason={reason}\n"
        f"findings={findings}\n"
        f"risk={report.get('initial_risk_score')} -> {report.get('final_risk_score')} (delta={report.get('risk_delta')})\n"
        f"before={format_state(before_state)}\n"
        f"after={format_state(after_state)}\n"
        f"steps={' | '.join(step_summaries) if step_summaries else 'no-steps'}"
    )


def build_agent(
    state_path: Path,
    memory_path: Path,
    config: Optional[AgentConfig] = None,
) -> tuple[SelfHealingDevOpsAgent, MockDeploymentPlatform]:
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
            "action_history": [],
            "recent_reports": [],
        },
    )
    platform = MockDeploymentPlatform(state_store)
    memory = AgentMemory(memory_store)
    return SelfHealingDevOpsAgent(platform, memory, config=config), platform


def run_loop(
    agent: SelfHealingDevOpsAgent,
    service: str,
    cycles: int,
    interval_seconds: float,
    inject_event: bool,
    max_actions: int,
) -> None:
    for index in range(1, cycles + 1):
        report = agent.run_healing_cycle(service, inject_event=inject_event, max_actions=max_actions)
        print(f"[{index}/{cycles}] {summarize_report(report)}")
        if index < cycles:
            time.sleep(interval_seconds)


def run_benchmark(
    service: str,
    episodes: int,
    cycles_per_episode: int,
    max_actions: int,
    simulate: bool,
    seed: int,
    config: AgentConfig,
) -> Dict[str, Any]:
    episodes = max(1, min(200, int(episodes)))
    cycles_per_episode = max(1, min(100, int(cycles_per_episode)))
    max_actions = max(1, min(5, int(max_actions)))

    total_cycles = 0
    improved_cycles = 0
    recovered_cycles = 0
    total_risk_delta = 0.0
    total_steps = 0
    blocked_policy_steps = 0
    severity_counts: Dict[str, int] = {}
    action_counts: Dict[str, int] = {}

    for episode_index in range(episodes):
        with tempfile.TemporaryDirectory(prefix="devops_agent_bench_") as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            memory_path = Path(temp_dir) / "memory.json"
            episode_seed = seed + episode_index * 9973
            random.seed(episode_seed)
            agent, platform = build_agent(state_path=state_path, memory_path=memory_path, config=config)
            platform.ensure_service(service)

            for _ in range(cycles_per_episode):
                report = agent.run_healing_cycle(
                    service=service,
                    inject_event=simulate,
                    max_actions=max_actions,
                )
                total_cycles += 1
                if report.get("improved"):
                    improved_cycles += 1

                before_status = report.get("before", {}).get("status")
                after_status = report.get("after", {}).get("status")
                if before_status in {"degraded", "failed"} and after_status == "healthy":
                    recovered_cycles += 1

                risk_delta = float(report.get("risk_delta", 0.0))
                total_risk_delta += risk_delta
                steps = report.get("steps", [])
                total_steps += len(steps)

                severity = str(report.get("severity", "unknown"))
                severity_counts[severity] = severity_counts.get(severity, 0) + 1

                for step in steps:
                    chosen_name = step.get("chosen_action", {}).get("name", "observe_only")
                    if chosen_name != "observe_only":
                        action_counts[chosen_name] = action_counts.get(chosen_name, 0) + 1
                    ranked = step.get("ranked_candidates", [])
                    if chosen_name == "observe_only" and ranked:
                        top = ranked[0]
                        if top.get("blocked"):
                            blocked_policy_steps += 1

    improved_rate = (improved_cycles / total_cycles * 100.0) if total_cycles else 0.0
    recovered_rate = (recovered_cycles / total_cycles * 100.0) if total_cycles else 0.0
    avg_risk_delta = (total_risk_delta / total_cycles) if total_cycles else 0.0
    avg_steps = (total_steps / total_cycles) if total_cycles else 0.0
    quality_score = (
        improved_rate * 0.60
        + recovered_rate * 0.35
        + avg_risk_delta * 8.0
        - blocked_policy_steps * 0.45
        - max(0.0, avg_steps - 1.0) * 2.2
    )

    return {
        "episodes": episodes,
        "cycles_per_episode": cycles_per_episode,
        "total_cycles": total_cycles,
        "simulate": simulate,
        "max_actions": max_actions,
        "improved_cycles": improved_cycles,
        "improved_rate_percent": round(improved_rate, 2),
        "recovered_to_healthy_cycles": recovered_cycles,
        "recovered_rate_percent": round(recovered_rate, 2),
        "average_risk_delta": round(avg_risk_delta, 3),
        "average_steps_per_cycle": round(avg_steps, 3),
        "blocked_policy_steps": blocked_policy_steps,
        "quality_score": round(quality_score, 3),
        "severity_counts": severity_counts,
        "action_counts": action_counts,
    }


def _clamp_float(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _mutated_config(base: AgentConfig, rng: random.Random) -> AgentConfig:
    payload = base.to_dict()

    for action in payload["base_priority"]:
        jitter = rng.uniform(0.82, 1.20)
        payload["base_priority"][action] = round(float(payload["base_priority"][action]) * jitter, 3)

    for action in payload["cooldown_seconds"]:
        base_value = int(payload["cooldown_seconds"][action])
        new_value = _clamp_int(int(round(base_value * rng.uniform(0.7, 1.4))), 0, 300)
        payload["cooldown_seconds"][action] = new_value

    for action in payload["rate_limit_per_5m"]:
        base_value = int(payload["rate_limit_per_5m"][action])
        if action == "observe_only":
            payload["rate_limit_per_5m"][action] = max(100, base_value)
            continue
        new_value = _clamp_int(int(round(base_value * rng.uniform(0.75, 1.35))), 1, 30)
        payload["rate_limit_per_5m"][action] = new_value

    for action in payload["action_penalty"]:
        base_value = float(payload["action_penalty"][action])
        new_value = _clamp_float(base_value * rng.uniform(0.65, 1.35), 0.0, 30.0)
        payload["action_penalty"][action] = round(new_value, 3)

    medium = _clamp_float(float(payload["severity_thresholds"]["medium"]) + rng.uniform(-8.0, 8.0), 25.0, 70.0)
    high = _clamp_float(float(payload["severity_thresholds"]["high"]) + rng.uniform(-10.0, 10.0), medium + 12.0, 120.0)
    critical = _clamp_float(
        float(payload["severity_thresholds"]["critical"]) + rng.uniform(-14.0, 14.0),
        high + 12.0,
        180.0,
    )
    payload["severity_thresholds"]["medium"] = round(medium, 3)
    payload["severity_thresholds"]["high"] = round(high, 3)
    payload["severity_thresholds"]["critical"] = round(critical, 3)

    exploration = _clamp_float(float(payload["exploration_strength"]) * rng.uniform(0.35, 1.9), 0.0, 12.0)
    payload["exploration_strength"] = round(exploration, 3)

    return AgentConfig.from_dict(payload)


def run_autotune(
    service: str,
    episodes: int,
    cycles_per_episode: int,
    max_actions: int,
    simulate: bool,
    seed: int,
    base_config: AgentConfig,
    trials: int,
) -> Dict[str, Any]:
    trials = _clamp_int(trials, 2, 80)
    rng = random.Random(seed)

    candidates: List[AgentConfig] = [base_config]
    for _ in range(trials - 1):
        candidates.append(_mutated_config(base_config, rng))

    leaderboard: List[Dict[str, Any]] = []
    best_entry: Optional[Dict[str, Any]] = None

    for index, candidate in enumerate(candidates, start=1):
        benchmark_seed = seed + index * 100_003
        result = run_benchmark(
            service=service,
            episodes=episodes,
            cycles_per_episode=cycles_per_episode,
            max_actions=max_actions,
            simulate=simulate,
            seed=benchmark_seed,
            config=candidate,
        )
        entry = {
            "trial": index,
            "seed": benchmark_seed,
            "quality_score": result["quality_score"],
            "improved_rate_percent": result["improved_rate_percent"],
            "recovered_rate_percent": result["recovered_rate_percent"],
            "average_risk_delta": result["average_risk_delta"],
            "blocked_policy_steps": result["blocked_policy_steps"],
            "average_steps_per_cycle": result["average_steps_per_cycle"],
            "config": candidate.to_dict(),
            "benchmark": result,
        }
        leaderboard.append(entry)
        if best_entry is None or float(entry["quality_score"]) > float(best_entry["quality_score"]):
            best_entry = entry

    leaderboard.sort(key=lambda item: float(item["quality_score"]), reverse=True)
    assert best_entry is not None

    top_n = min(5, len(leaderboard))
    top_trials = [
        {
            "trial": trial["trial"],
            "quality_score": trial["quality_score"],
            "improved_rate_percent": trial["improved_rate_percent"],
            "recovered_rate_percent": trial["recovered_rate_percent"],
            "average_risk_delta": trial["average_risk_delta"],
            "blocked_policy_steps": trial["blocked_policy_steps"],
        }
        for trial in leaderboard[:top_n]
    ]

    return {
        "service": service,
        "trials": trials,
        "episodes": episodes,
        "cycles_per_episode": cycles_per_episode,
        "max_actions": max_actions,
        "simulate": simulate,
        "seed": seed,
        "best_trial": best_entry["trial"],
        "best_quality_score": best_entry["quality_score"],
        "best_config": best_entry["config"],
        "best_benchmark": best_entry["benchmark"],
        "top_trials": top_trials,
        "leaderboard": leaderboard,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Python-only autonomous AI DevOps agent with self-healing deployment actions."
    )
    parser.add_argument("--service", default="payments-api", help="Target service name.")
    parser.add_argument("--state-file", default="deployment_state.json", help="Path to persisted deployment state.")
    parser.add_argument("--memory-file", default="agent_memory.json", help="Path to persisted agent memory.")
    parser.add_argument("--seed", type=int, default=7, help="Random seed for repeatable simulations.")
    parser.add_argument("--config-file", default="", help="Optional JSON file for advanced policy/scoring config.")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("chat", help="Interactive chatbot mode (default).")

    status_parser = subparsers.add_parser("status", help="Print current deployment state.")
    status_parser.add_argument("--simulate", action="store_true", help="Inject a random event before reading status.")

    heal_parser = subparsers.add_parser("heal", help="Run one self-healing cycle.")
    heal_parser.add_argument("--simulate", action="store_true", help="Inject a random event before healing.")
    heal_parser.add_argument("--max-actions", type=int, default=2, help="Maximum remediation steps in one cycle.")
    heal_parser.add_argument("--dry-run", action="store_true", help="Evaluate plan but do not execute actions.")

    diagnose_parser = subparsers.add_parser("diagnose", help="Diagnose and plan without executing actions.")
    diagnose_parser.add_argument("--simulate", action="store_true", help="Inject a random event before diagnosis.")
    diagnose_parser.add_argument("--max-actions", type=int, default=2, help="Maximum planning depth.")

    loop_parser = subparsers.add_parser("loop", help="Run autonomous loop for multiple cycles.")
    loop_parser.add_argument("--cycles", type=int, default=10, help="Number of cycles.")
    loop_parser.add_argument("--interval", type=float, default=2.0, help="Seconds between cycles.")
    loop_parser.add_argument("--simulate", action="store_true", help="Inject random events each cycle.")
    loop_parser.add_argument("--max-actions", type=int, default=2, help="Maximum remediation steps per cycle.")

    subparsers.add_parser("memory", help="Show memory statistics from past actions.")
    report_parser = subparsers.add_parser("report", help="Show the latest healing report.")
    report_parser.add_argument("--json", action="store_true", help="Print full report as JSON.")

    benchmark_parser = subparsers.add_parser("benchmark", help="Run simulation benchmark and print reliability metrics.")
    benchmark_parser.add_argument("--episodes", type=int, default=20, help="Number of benchmark episodes.")
    benchmark_parser.add_argument("--cycles", type=int, default=8, help="Cycles per episode.")
    benchmark_parser.add_argument("--max-actions", type=int, default=2, help="Maximum remediation actions per cycle.")
    benchmark_parser.add_argument("--json", action="store_true", help="Print benchmark result as JSON.")
    simulate_group = benchmark_parser.add_mutually_exclusive_group()
    simulate_group.add_argument(
        "--simulate",
        dest="simulate",
        action="store_true",
        default=True,
        help="Inject random incidents during benchmark (default).",
    )
    simulate_group.add_argument(
        "--no-simulate",
        dest="simulate",
        action="store_false",
        help="Run benchmark without injecting incidents.",
    )

    autotune_parser = subparsers.add_parser(
        "autotune",
        help="Search policy/scoring configuration space and select the best config via benchmark quality score.",
    )
    autotune_parser.add_argument("--trials", type=int, default=12, help="Number of candidate configs to evaluate.")
    autotune_parser.add_argument("--episodes", type=int, default=16, help="Episodes per trial.")
    autotune_parser.add_argument("--cycles", type=int, default=8, help="Cycles per episode.")
    autotune_parser.add_argument("--max-actions", type=int, default=2, help="Max remediation actions per cycle.")
    autotune_parser.add_argument("--json", action="store_true", help="Print full autotune result as JSON.")
    autotune_parser.add_argument(
        "--write-config",
        default="",
        help="Optional file path to write the best config JSON.",
    )
    autotune_sim_group = autotune_parser.add_mutually_exclusive_group()
    autotune_sim_group.add_argument(
        "--simulate",
        dest="simulate",
        action="store_true",
        default=True,
        help="Inject random incidents during autotune benchmark (default).",
    )
    autotune_sim_group.add_argument(
        "--no-simulate",
        dest="simulate",
        action="store_false",
        help="Run autotune without injecting incidents.",
    )

    subparsers.add_parser("config", help="Print the active merged agent config.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    random.seed(args.seed)

    config_path = Path(args.config_file) if args.config_file else None
    try:
        config = load_agent_config(config_path)
    except ValueError as err:
        raise SystemExit(str(err))

    command = args.command or "chat"
    if command == "config":
        print(json.dumps(config.to_dict(), indent=2))
        return

    state_path = Path(args.state_file)
    memory_path = Path(args.memory_file)
    agent, platform = build_agent(state_path=state_path, memory_path=memory_path, config=config)
    platform.ensure_service(args.service)

    if command == "status":
        if args.simulate:
            event = platform.inject_random_event(args.service)
            print(f"Simulated event: {event}")
        print(format_state(platform.get_state(args.service)))
        return

    if command == "heal":
        report = agent.run_healing_cycle(
            args.service,
            inject_event=args.simulate,
            max_actions=max(1, min(5, int(args.max_actions))),
            dry_run=bool(args.dry_run),
        )
        print(summarize_report(report))
        return

    if command == "diagnose":
        report = agent.run_healing_cycle(
            args.service,
            inject_event=args.simulate,
            max_actions=max(1, min(5, int(args.max_actions))),
            dry_run=True,
        )
        print(format_detailed_report(report))
        return

    if command == "loop":
        cycles = max(1, min(200, int(args.cycles)))
        interval = max(0.0, float(args.interval))
        max_actions = max(1, min(5, int(args.max_actions)))
        run_loop(
            agent,
            args.service,
            cycles=cycles,
            interval_seconds=interval,
            inject_event=args.simulate,
            max_actions=max_actions,
        )
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

    if command == "benchmark":
        benchmark = run_benchmark(
            service=args.service,
            episodes=max(1, int(args.episodes)),
            cycles_per_episode=max(1, int(args.cycles)),
            max_actions=max(1, min(5, int(args.max_actions))),
            simulate=bool(args.simulate),
            seed=int(args.seed),
            config=config,
        )
        if args.json:
            print(json.dumps(benchmark, indent=2))
        else:
            print(
                "Benchmark Summary\n"
                f"episodes={benchmark['episodes']}, cycles_per_episode={benchmark['cycles_per_episode']}, "
                f"total_cycles={benchmark['total_cycles']}\n"
                f"improved_rate={benchmark['improved_rate_percent']}%, "
                f"recovered_rate={benchmark['recovered_rate_percent']}%\n"
                f"avg_risk_delta={benchmark['average_risk_delta']}, "
                f"avg_steps={benchmark['average_steps_per_cycle']}, "
                f"blocked_policy_steps={benchmark['blocked_policy_steps']}, "
                f"quality_score={benchmark['quality_score']}\n"
                f"severity_counts={benchmark['severity_counts']}\n"
                f"action_counts={benchmark['action_counts']}"
            )
        return

    if command == "autotune":
        autotune = run_autotune(
            service=args.service,
            trials=max(2, int(args.trials)),
            episodes=max(1, int(args.episodes)),
            cycles_per_episode=max(1, int(args.cycles)),
            max_actions=max(1, min(5, int(args.max_actions))),
            simulate=bool(args.simulate),
            seed=int(args.seed),
            base_config=config,
        )

        if args.write_config:
            output_path = Path(args.write_config)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(autotune["best_config"], indent=2), encoding="utf-8")

        if args.json:
            print(json.dumps(autotune, indent=2))
        else:
            print(
                "Autotune Summary\n"
                f"trials={autotune['trials']}, best_trial={autotune['best_trial']}, "
                f"best_quality_score={autotune['best_quality_score']}\n"
                f"best_improved_rate={autotune['best_benchmark']['improved_rate_percent']}%, "
                f"best_recovered_rate={autotune['best_benchmark']['recovered_rate_percent']}%, "
                f"best_avg_risk_delta={autotune['best_benchmark']['average_risk_delta']}\n"
                f"top_trials={autotune['top_trials']}"
            )
            if args.write_config:
                print(f"Best config written to {args.write_config}")
        return

    chatbot = DevOpsChatbot(agent, platform, args.service)
    chatbot.run()


if __name__ == "__main__":
    main()
