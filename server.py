from __future__ import annotations

import argparse
import json
import re
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ai_devops_agent import (
    AgentConfig,
    JsonFileStore,
    build_agent,
    format_detailed_report,
    format_analytics_report,
    format_state,
    load_agent_config,
    summarize_report,
)


class BackendService:
    def __init__(
        self,
        service: str,
        state_file: Path,
        memory_file: Path,
        chat_file: Path,
        config: Optional[AgentConfig],
        max_chat_messages: int = 300,
    ):
        self.service = service
        self.max_chat_messages = max_chat_messages
        self.lock = threading.RLock()

        self.agent, self.platform = build_agent(state_path=state_file, memory_path=memory_file, config=config)
        self.platform.ensure_service(self.service)

        self.chat_store = JsonFileStore(chat_file, default_data={"messages": [], "next_id": 1})
        self.chat_data = self.chat_store.load()
        self._normalize_chat_data()

        self._autopilot_stop = threading.Event()
        self._autopilot_thread: Optional[threading.Thread] = None
        self._autopilot = {
            "running": False,
            "started_at": "",
            "stopped_at": "",
            "interval_seconds": 8.0,
            "max_actions": 2,
            "inject_event": True,
            "target_cycles": 0,
            "cycles_completed": 0,
            "last_cycle_at": "",
            "last_summary": "",
            "last_error": "",
        }

    def _normalize_chat_data(self) -> None:
        messages = self.chat_data.get("messages")
        if not isinstance(messages, list):
            messages = []
        cleaned: List[Dict[str, Any]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "assistant"))
            text = str(item.get("text", ""))
            timestamp = str(item.get("timestamp", datetime.now(timezone.utc).isoformat()))
            message_id = int(item.get("id", len(cleaned) + 1))
            cleaned.append(
                {
                    "id": message_id,
                    "role": role,
                    "text": text,
                    "timestamp": timestamp,
                    "kind": str(item.get("kind", "chat")),
                }
            )
        self.chat_data["messages"] = cleaned[-self.max_chat_messages :]
        next_id = self.chat_data.get("next_id")
        if not isinstance(next_id, int) or next_id < 1:
            next_id = (cleaned[-1]["id"] + 1) if cleaned else 1
        self.chat_data["next_id"] = next_id
        self._save_chat()

    def _save_chat(self) -> None:
        self.chat_store.save(self.chat_data)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _state_snapshot_locked(self) -> Dict[str, Any]:
        state = self.platform.get_state(self.service)
        severity = self.agent.classify_severity(state)
        risk_score = round(self.agent._risk_score(state), 3)
        return {
            "service": state.service,
            "version": state.version,
            "status": state.status,
            "error_rate": round(state.error_rate, 3),
            "latency_ms": state.latency_ms,
            "cpu_percent": round(state.cpu_percent, 2),
            "memory_percent": round(state.memory_percent, 2),
            "ready_replicas": state.ready_replicas,
            "desired_replicas": state.desired_replicas,
            "severity": severity,
            "risk_score": risk_score,
            "formatted": format_state(state),
        }

    def get_state_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return self._state_snapshot_locked()

    def get_autopilot_status(self) -> Dict[str, Any]:
        with self.lock:
            running = bool(
                self._autopilot.get("running")
                and self._autopilot_thread is not None
                and self._autopilot_thread.is_alive()
            )
            if not running:
                self._autopilot["running"] = False
            return {"autopilot": dict(self._autopilot)}

    def get_analytics(self, window: int = 40) -> Dict[str, Any]:
        with self.lock:
            safe_window = max(1, min(600, int(window)))
            analytics = self.agent.memory.analytics(service=self.service, window=safe_window)
            return {"analytics": analytics}

    def _append_message(self, role: str, text: str, kind: str = "chat") -> Dict[str, Any]:
        next_id = int(self.chat_data.get("next_id", 1))
        message = {
            "id": next_id,
            "role": role,
            "text": text,
            "kind": kind,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.chat_data["next_id"] = next_id + 1
        messages: List[Dict[str, Any]] = self.chat_data.setdefault("messages", [])  # type: ignore
        messages.append(message)
        if len(messages) > self.max_chat_messages:
            del messages[0 : len(messages) - self.max_chat_messages]
        self._save_chat()
        return message

    @staticmethod
    def _clamp_float(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    @staticmethod
    def _clamp_int(value: int, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, value))

    def _autopilot_loop(self) -> None:
        while not self._autopilot_stop.is_set():
            with self.lock:
                interval_seconds = float(self._autopilot.get("interval_seconds", 8.0))
                max_actions = int(self._autopilot.get("max_actions", 2))
                inject_event = bool(self._autopilot.get("inject_event", True))
                target_cycles = int(self._autopilot.get("target_cycles", 0))
                completed = int(self._autopilot.get("cycles_completed", 0))
                if target_cycles > 0 and completed >= target_cycles:
                    self._autopilot["running"] = False
                    self._autopilot["stopped_at"] = self._utc_now()
                    break

                cycle_number = completed + 1
                try:
                    report = self.agent.run_healing_cycle(
                        self.service,
                        inject_event=inject_event,
                        max_actions=max_actions,
                        dry_run=False,
                    )
                    summary = summarize_report(report)
                    self._autopilot["cycles_completed"] = cycle_number
                    self._autopilot["last_cycle_at"] = self._utc_now()
                    self._autopilot["last_summary"] = summary
                    self._autopilot["last_error"] = ""
                    self._append_message("assistant", f"[autopilot] cycle {cycle_number}: {summary}", kind="autopilot")
                except Exception as err:
                    self._autopilot["last_error"] = str(err)
                    self._autopilot["running"] = False
                    self._autopilot["stopped_at"] = self._utc_now()
                    self._append_message("assistant", f"[autopilot] error: {err}", kind="autopilot")
                    break

                if target_cycles > 0 and cycle_number >= target_cycles:
                    self._autopilot["running"] = False
                    self._autopilot["stopped_at"] = self._utc_now()
                    self._append_message(
                        "assistant",
                        f"[autopilot] completed {target_cycles} cycle(s) and stopped.",
                        kind="autopilot",
                    )
                    break

            if self._autopilot_stop.wait(interval_seconds):
                break

        with self.lock:
            if self._autopilot.get("running"):
                self._autopilot["running"] = False
                self._autopilot["stopped_at"] = self._utc_now()

    def start_autopilot(
        self,
        interval_seconds: float = 8.0,
        max_actions: int = 2,
        inject_event: bool = True,
        cycles: int = 0,
    ) -> Dict[str, Any]:
        with self.lock:
            currently_running = bool(
                self._autopilot.get("running")
                and self._autopilot_thread is not None
                and self._autopilot_thread.is_alive()
            )
            if currently_running:
                return {"started": False, "reason": "already_running", "autopilot": dict(self._autopilot)}

            safe_interval = self._clamp_float(float(interval_seconds), 1.0, 300.0)
            safe_actions = self._clamp_int(int(max_actions), 1, 5)
            safe_cycles = self._clamp_int(int(cycles), 0, 2000)

            self._autopilot_stop.clear()
            self._autopilot.update(
                {
                    "running": True,
                    "started_at": self._utc_now(),
                    "stopped_at": "",
                    "interval_seconds": round(safe_interval, 2),
                    "max_actions": safe_actions,
                    "inject_event": bool(inject_event),
                    "target_cycles": safe_cycles,
                    "cycles_completed": 0,
                    "last_cycle_at": "",
                    "last_summary": "",
                    "last_error": "",
                }
            )
            self._append_message(
                "assistant",
                (
                    f"[autopilot] started: interval={safe_interval:.2f}s, max_actions={safe_actions}, "
                    f"inject_event={bool(inject_event)}, target_cycles={safe_cycles or 'unbounded'}"
                ),
                kind="autopilot",
            )

            self._autopilot_thread = threading.Thread(target=self._autopilot_loop, name="autopilot-loop", daemon=True)
            self._autopilot_thread.start()
            return {"started": True, "autopilot": dict(self._autopilot)}

    def stop_autopilot(self, wait_seconds: float = 2.0) -> Dict[str, Any]:
        lock_owned_by_current = bool(getattr(self.lock, "_is_owned", lambda: False)())
        thread: Optional[threading.Thread]
        with self.lock:
            running = bool(
                self._autopilot.get("running")
                and self._autopilot_thread is not None
                and self._autopilot_thread.is_alive()
            )
            if not running:
                self._autopilot["running"] = False
                return {"stopped": False, "reason": "not_running", "autopilot": dict(self._autopilot)}
            thread = self._autopilot_thread
            self._autopilot_stop.set()

        # If caller already holds self.lock (e.g. process_message), joining here can starve worker shutdown.
        if lock_owned_by_current:
            return {"stopped": False, "reason": "stop_requested", "autopilot": dict(self._autopilot)}

        if thread is not None:
            thread.join(timeout=max(0.1, float(wait_seconds)))

        with self.lock:
            still_running = bool(thread is not None and thread.is_alive())
            self._autopilot["running"] = still_running
            if not still_running:
                self._autopilot["stopped_at"] = self._utc_now()
                self._append_message("assistant", "[autopilot] stopped by user.", kind="autopilot")
                self._autopilot_thread = None
                return {"stopped": True, "autopilot": dict(self._autopilot)}
            return {"stopped": False, "reason": "stop_timeout", "autopilot": dict(self._autopilot)}

    def shutdown(self) -> None:
        self.stop_autopilot(wait_seconds=2.0)

    def get_history(self, limit: int) -> List[Dict[str, Any]]:
        with self.lock:
            safe_limit = max(1, min(500, int(limit)))
            messages: List[Dict[str, Any]] = self.chat_data.get("messages", [])  # type: ignore
            return messages[-safe_limit:]

    def clear_history(self) -> Dict[str, Any]:
        with self.lock:
            messages: List[Dict[str, Any]] = self.chat_data.get("messages", [])  # type: ignore
            cleared = len(messages)
            self.chat_data["messages"] = []
            self.chat_data["next_id"] = 1
            reply_message = self._append_message(
                "assistant",
                "Chat history cleared. Ask for status, summary, diagnose, heal, report, analytics, or autopilot.",
                kind="system",
            )
            snapshot = self._state_snapshot_locked()
            return {
                "cleared": cleared,
                "reply": reply_message["text"],
                "timestamp": reply_message["timestamp"],
                "state": snapshot,
            }

    @staticmethod
    def _parse_auto_command(message: str) -> tuple[int, int]:
        match = re.match(r"^\s*auto(?:\s+(\d+))?(?:\s+(\d+))?\s*$", message, re.IGNORECASE)
        if not match:
            return (3, 2)
        raw_cycles, raw_actions = match.groups()
        cycles = int(raw_cycles) if raw_cycles else 3
        max_actions = int(raw_actions) if raw_actions else 2
        return (max(1, min(20, cycles)), max(1, min(5, max_actions)))

    @staticmethod
    def _parse_autopilot_start(message: str) -> tuple[float, int, int]:
        match = re.match(
            r"^\s*autopilot\s+start(?:\s+(\d+(?:\.\d+)?))?(?:\s+(\d+))?(?:\s+(\d+))?\s*$",
            message,
            re.IGNORECASE,
        )
        if not match:
            return (8.0, 2, 0)
        raw_interval, raw_actions, raw_cycles = match.groups()
        interval = float(raw_interval) if raw_interval else 8.0
        max_actions = int(raw_actions) if raw_actions else 2
        cycles = int(raw_cycles) if raw_cycles else 0
        return (interval, max_actions, cycles)

    def _summary_text_locked(self) -> str:
        snapshot = self._state_snapshot_locked()
        return (
            "Deployment Summary\n"
            f"Service: {snapshot['service']} v{snapshot['version']}\n"
            f"Health: {snapshot['status']} ({snapshot['severity']})\n"
            f"Risk Score: {snapshot['risk_score']}\n"
            f"Error Rate: {snapshot['error_rate']}%\n"
            f"Latency: {snapshot['latency_ms']}ms\n"
            f"CPU: {snapshot['cpu_percent']}% | Memory: {snapshot['memory_percent']}%\n"
            f"Replicas: {snapshot['ready_replicas']}/{snapshot['desired_replicas']}"
        )

    def process_message(self, message: str, max_actions: int = 2) -> Dict[str, Any]:
        safe_max_actions = max(1, min(5, int(max_actions)))
        user_text = str(message or "").strip()
        if len(user_text) > 2000:
            user_text = user_text[:2000]

        with self.lock:
            lowered = user_text.lower()
            if lowered in {"", " "}:
                reply = (
                    "Please type a command. Try: status, summary, diagnose, heal, simulate, auto 5, report, "
                    "memory, analytics, autopilot status."
                )
                assistant_message = self._append_message("assistant", reply, kind="system")
                return {
                    "reply": reply,
                    "timestamp": assistant_message["timestamp"],
                    "state": self._state_snapshot_locked(),
                }

            if lowered in {"clear", "clear chat", "clear history"}:
                return self.clear_history()

            self._append_message("user", user_text, kind="user")
            reply = ""

            if "help" in lowered:
                reply = (
                    "Commands: status, summary, diagnose, heal, simulate, auto <cycles> <max_actions>, "
                    "autopilot start <interval_s> <max_actions> <cycles>, autopilot status, autopilot stop, "
                    "report, memory, analytics <window>, clear."
                )
            elif re.match(r"^\s*autopilot\s+start(?:\s+\d+(?:\.\d+)?)?(?:\s+\d+)?(?:\s+\d+)?\s*$", lowered):
                interval, parsed_actions, cycles = self._parse_autopilot_start(user_text)
                result = self.start_autopilot(
                    interval_seconds=interval,
                    max_actions=parsed_actions,
                    inject_event=True,
                    cycles=cycles,
                )
                if result.get("started"):
                    meta = result["autopilot"]
                    reply = (
                        "Autopilot started.\n"
                        f"interval={meta['interval_seconds']}s, max_actions={meta['max_actions']}, "
                        f"target_cycles={meta['target_cycles'] or 'unbounded'}"
                    )
                else:
                    reply = "Autopilot is already running."
            elif re.match(r"^\s*autopilot\s+stop\s*$", lowered):
                result = self.stop_autopilot(wait_seconds=2.0)
                if result.get("stopped"):
                    reply = "Autopilot stopped."
                elif result.get("reason") == "stop_requested":
                    reply = "Autopilot stop requested. It will stop shortly."
                elif result.get("reason") == "not_running":
                    reply = "Autopilot is not running."
                else:
                    reply = "Autopilot stop requested, but worker is still shutting down."
            elif re.match(r"^\s*autopilot(?:\s+status)?\s*$", lowered):
                meta = self.get_autopilot_status()["autopilot"]
                reply = (
                    "Autopilot Status\n"
                    f"running={meta['running']}\n"
                    f"interval={meta['interval_seconds']}s\n"
                    f"max_actions={meta['max_actions']}\n"
                    f"target_cycles={meta['target_cycles'] or 'unbounded'}\n"
                    f"cycles_completed={meta['cycles_completed']}\n"
                    f"last_cycle_at={meta['last_cycle_at'] or '-'}\n"
                    f"last_error={meta['last_error'] or '-'}"
                )
            elif re.match(r"^\s*auto(?:\s+\d+)?(?:\s+\d+)?\s*$", lowered):
                cycles, parsed_actions = self._parse_auto_command(user_text)
                summaries: List[str] = []
                for index in range(1, cycles + 1):
                    report = self.agent.run_healing_cycle(
                        self.service,
                        inject_event=True,
                        max_actions=parsed_actions,
                        dry_run=False,
                    )
                    summaries.append(f"{index}. {summarize_report(report)}")
                reply = "Autonomous run complete.\n" + "\n".join(summaries)
            elif re.match(r"^\s*status\s*$", lowered):
                reply = self._state_snapshot_locked()["formatted"]
            elif re.match(r"^\s*summary\s*$", lowered):
                reply = self._summary_text_locked()
            elif re.match(r"^\s*simulate(?:\s+incident)?\s*$", lowered):
                event = self.platform.inject_random_event(self.service)
                reply = f"Incident simulated: {event}\n{self._state_snapshot_locked()['formatted']}"
            elif re.match(r"^\s*diagnose\s*$", lowered):
                report = self.agent.run_healing_cycle(
                    self.service,
                    inject_event=False,
                    max_actions=safe_max_actions,
                    dry_run=True,
                )
                reply = format_detailed_report(report)
            elif re.match(r"^\s*(heal|fix)(?:\s+now)?\s*$", lowered):
                report = self.agent.run_healing_cycle(
                    self.service,
                    inject_event=False,
                    max_actions=safe_max_actions,
                    dry_run=False,
                )
                reply = summarize_report(report)
            elif "report" in lowered:
                latest = self.agent.memory.latest_report()
                if latest is None:
                    reply = "No report available yet. Run heal or auto first."
                else:
                    reply = format_detailed_report(latest)
            elif "memory" in lowered:
                reply = self.agent.memory.summary()
            elif "analytics" in lowered or "insight" in lowered or "stability" in lowered or "trend" in lowered:
                parsed_window = 40
                match = re.search(r"(\d+)", lowered)
                if match:
                    parsed_window = max(1, min(600, int(match.group(1))))
                analytics = self.agent.memory.analytics(self.service, window=parsed_window)
                reply = format_analytics_report(analytics)
            elif "error" in lowered:
                snapshot = self._state_snapshot_locked()
                reply = f"Current error rate is {snapshot['error_rate']:.2f}%."
            elif "latency" in lowered:
                snapshot = self._state_snapshot_locked()
                reply = f"Current latency is {snapshot['latency_ms']}ms."
            else:
                reply = (
                    "I can help with status, summary, diagnose, heal, simulate incidents, auto runs, autopilot, "
                    "report, memory, and analytics."
                )

            assistant_message = self._append_message("assistant", reply, kind="assistant")
            return {
                "reply": reply,
                "timestamp": assistant_message["timestamp"],
                "state": self._state_snapshot_locked(),
            }


class AppRequestHandler(BaseHTTPRequestHandler):
    backend: Optional[BackendService] = None
    root_dir = Path(__file__).resolve().parent

    @staticmethod
    def _normalized_path(path: str) -> str:
        normalized = re.sub(r"^/api(?:/api)+", "/api", path)
        if len(normalized) > 1:
            normalized = normalized.rstrip("/")
        return normalized or "/"

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, status_code: int, content_type: str, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc
        if content_length < 0 or content_length > 2_000_000:
            raise ValueError("Invalid request size.")
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request JSON must be an object.")
        return payload

    def _serve_index(self) -> None:
        index_path = self.root_dir / "index.html"
        if not index_path.exists():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "index.html not found."})
            return
        self._send_text(HTTPStatus.OK, "text/html; charset=utf-8", index_path.read_text(encoding="utf-8"))

    def do_GET(self) -> None:
        backend = self.backend
        if backend is None:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Backend not initialized."})
            return

        parsed = urlparse(self.path)
        path = self._normalized_path(parsed.path)

        if path in {"/", "/index.html"}:
            self._serve_index()
            return

        if path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()})
            return

        if path == "/api/state":
            self._send_json(HTTPStatus.OK, {"state": backend.get_state_snapshot()})
            return

        if path == "/api/autopilot":
            self._send_json(HTTPStatus.OK, backend.get_autopilot_status())
            return

        if path == "/api/analytics":
            query = parse_qs(parsed.query)
            raw_window = query.get("window", ["40"])[0]
            try:
                window = int(raw_window)
            except ValueError:
                window = 40
            self._send_json(HTTPStatus.OK, backend.get_analytics(window=window))
            return

        if path == "/api/history":
            query = parse_qs(parsed.query)
            raw_limit = query.get("limit", ["120"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = 120
            self._send_json(HTTPStatus.OK, {"messages": backend.get_history(limit)})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found."})

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        backend = self.backend
        if backend is None:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Backend not initialized."})
            return

        parsed = urlparse(self.path)
        path = self._normalized_path(parsed.path)

        try:
            if path == "/api/chat":
                payload = self._read_json_body()
                message = str(payload.get("message", ""))
                max_actions = int(payload.get("max_actions", 2))
                response = backend.process_message(message=message, max_actions=max_actions)
                self._send_json(HTTPStatus.OK, response)
                return

            if path == "/api/history/clear":
                response = backend.clear_history()
                self._send_json(HTTPStatus.OK, response)
                return

            if path == "/api/autopilot/start":
                payload = self._read_json_body()
                interval_seconds = float(payload.get("interval_seconds", 8.0))
                max_actions = int(payload.get("max_actions", 2))
                inject_event = bool(payload.get("inject_event", True))
                cycles = int(payload.get("cycles", 0))
                response = backend.start_autopilot(
                    interval_seconds=interval_seconds,
                    max_actions=max_actions,
                    inject_event=inject_event,
                    cycles=cycles,
                )
                self._send_json(HTTPStatus.OK, response)
                return

            if path == "/api/autopilot/stop":
                response = backend.stop_autopilot(wait_seconds=2.0)
                self._send_json(HTTPStatus.OK, response)
                return
        except ValueError as err:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(err)})
            return
        except Exception as err:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Server error: {err}"})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found."})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep runtime logs concise.
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")


def parse_server_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Python backend server for AI DevOps chatbot UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the HTTP server.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind the HTTP server.")
    parser.add_argument("--service", default="payments-api", help="Service name for the DevOps agent.")
    parser.add_argument("--state-file", default="deployment_state.json", help="Path to deployment state JSON.")
    parser.add_argument("--memory-file", default="agent_memory.json", help="Path to agent memory JSON.")
    parser.add_argument("--chat-file", default="chat_history.json", help="Path to persisted chat history JSON.")
    parser.add_argument("--config-file", default="", help="Optional JSON config for advanced policy/scoring.")
    parser.add_argument(
        "--max-chat-messages",
        type=int,
        default=300,
        help="Maximum number of chat messages persisted.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_server_args()
    config_path = Path(args.config_file) if args.config_file else None

    try:
        config = load_agent_config(config_path)
    except ValueError as err:
        raise SystemExit(str(err))

    backend = BackendService(
        service=args.service,
        state_file=Path(args.state_file),
        memory_file=Path(args.memory_file),
        chat_file=Path(args.chat_file),
        config=config,
        max_chat_messages=max(50, min(2000, int(args.max_chat_messages))),
    )

    AppRequestHandler.backend = backend
    server = ThreadingHTTPServer((args.host, int(args.port)), AppRequestHandler)
    print(f"Backend running on http://{args.host}:{args.port}")
    print(
        "Routes: GET /, GET /api/state, GET /api/analytics, GET /api/autopilot, GET /api/history, "
        "POST /api/chat, POST /api/history/clear, POST /api/autopilot/start, POST /api/autopilot/stop"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        backend.shutdown()
        server.server_close()
        print("Backend server stopped.")


if __name__ == "__main__":
    main()
