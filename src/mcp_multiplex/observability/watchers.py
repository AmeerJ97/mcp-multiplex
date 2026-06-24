"""Observe-only config watchers and periodic audit fallback."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mcp_multiplex.adapters import (
    DiscoveredConfigPath,
    parse_claude_code_config,
    parse_cline_config,
    parse_codex_config,
    parse_gemini_config,
    parse_opencode_config,
)
from mcp_multiplex.observability.audit import EventRecord, EventStore
from mcp_multiplex.observability.config_audit import IngestionResult, ingest_observed_entries
from mcp_multiplex.schemas import ObservedEntry

AuditTrigger = Literal["startup", "file_change", "periodic"]


@dataclass(frozen=True)
class WatchedConfigPath:
    """A supported agent config path observed by the daemon."""

    agent_id: str
    agent_kind: str
    path: Path
    format: str
    precedence: int = 0
    is_project_shared: bool = False

    @classmethod
    def from_discovered(
        cls,
        discovered: DiscoveredConfigPath,
        *,
        agent_id: str | None = None,
    ) -> WatchedConfigPath:
        """Build a watched path from read-only config discovery output."""
        return cls(
            agent_id=agent_id or f"agent_{discovered.agent_kind}_user_default",
            agent_kind=discovered.agent_kind,
            path=Path(discovered.path),
            format=discovered.format,
            precedence=discovered.precedence,
            is_project_shared=discovered.is_project_shared,
        )


@dataclass(frozen=True)
class FileSignature:
    """File state used by the polling watcher."""

    mtime_ns: int
    size: int
    digest: str


@dataclass(frozen=True)
class WatchEvent:
    """A watcher observation that triggered an audit run."""

    trigger: AuditTrigger
    paths: list[str]
    run_id: str
    result: IngestionResult
    trigger_event: EventRecord

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger": self.trigger,
            "paths": self.paths,
            "run_id": self.run_id,
            "result": self.result.to_dict(),
            "trigger_event": self.trigger_event.to_dict(),
        }


@dataclass
class PendingChange:
    """A pending file change waiting for debounce stability."""

    first_seen_at: float
    signature: FileSignature | None


@dataclass
class PollingAuditWatcher:
    """Polling watcher with debounce and periodic full-audit fallback."""

    connection: sqlite3.Connection
    targets: list[WatchedConfigPath]
    debounce_seconds: float = 0.5
    periodic_seconds: float = 300.0
    actor: str = "daemon"
    _last_signatures: dict[str, FileSignature | None] = field(default_factory=dict, init=False)
    _pending: dict[str, PendingChange] = field(default_factory=dict, init=False)
    _next_periodic_at: float | None = field(default=None, init=False)
    _run_counter: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.targets = sorted(self.targets, key=lambda item: (str(item.path), item.agent_id))
        self._last_signatures = {
            str(target.path): file_signature(target.path) for target in self.targets
        }
        self._next_periodic_at = None

    def run_startup_audit(self, *, now: float = 0.0, timestamp: str | None = None) -> WatchEvent:
        """Run the daemon startup audit over all watched paths."""
        self._next_periodic_at = now + self.periodic_seconds
        return self._run_audit(self.targets, trigger="startup", timestamp=timestamp)

    def poll(self, *, now: float, timestamp: str | None = None) -> list[WatchEvent]:
        """Poll watched files and return audit runs that were triggered."""
        events: list[WatchEvent] = []
        changed_targets = self._stable_changed_targets(now)
        if changed_targets:
            events.append(
                self._run_audit(changed_targets, trigger="file_change", timestamp=timestamp)
            )
        if self._next_periodic_at is None:
            self._next_periodic_at = now + self.periodic_seconds
        if now >= self._next_periodic_at:
            events.append(self._run_audit(self.targets, trigger="periodic", timestamp=timestamp))
            self._next_periodic_at = now + self.periodic_seconds
        return events

    def _stable_changed_targets(self, now: float) -> list[WatchedConfigPath]:
        stable_paths: set[str] = set()
        for target in self.targets:
            path_key = str(target.path)
            signature = file_signature(target.path)
            if signature == self._last_signatures.get(path_key):
                pending = self._pending.get(path_key)
                if pending is not None and now - pending.first_seen_at >= self.debounce_seconds:
                    stable_paths.add(path_key)
                    self._last_signatures[path_key] = pending.signature
                    del self._pending[path_key]
                continue
            pending = self._pending.get(path_key)
            if pending is None or pending.signature != signature:
                self._pending[path_key] = PendingChange(first_seen_at=now, signature=signature)
                continue
            if now - pending.first_seen_at >= self.debounce_seconds:
                stable_paths.add(path_key)
                self._last_signatures[path_key] = signature
                del self._pending[path_key]
        return [target for target in self.targets if str(target.path) in stable_paths]

    def _run_audit(
        self,
        targets: list[WatchedConfigPath],
        *,
        trigger: AuditTrigger,
        timestamp: str | None,
    ) -> WatchEvent:
        self._run_counter += 1
        run_id = f"watch_{trigger}_{self._run_counter:06d}"
        result = run_config_audit(
            self.connection,
            targets,
            trigger=trigger,
            run_id=run_id,
            actor=self.actor,
            timestamp=timestamp,
        )
        trigger_event = EventStore(self.connection).query(event_type="audit.triggered")[-1]
        return WatchEvent(
            trigger=trigger,
            paths=[str(target.path) for target in targets],
            run_id=run_id,
            result=result,
            trigger_event=trigger_event,
        )


def run_config_audit(
    connection: sqlite3.Connection,
    targets: list[WatchedConfigPath],
    *,
    trigger: AuditTrigger,
    run_id: str,
    actor: str = "daemon",
    timestamp: str | None = None,
) -> IngestionResult:
    """Parse watched config files, ingest observations, and emit trigger events."""
    sorted_targets = sorted(targets, key=lambda item: (str(item.path), item.agent_id))
    event_store = EventStore(connection)
    event_store.append(
        event_id=_trigger_event_id(run_id),
        event_type="audit.triggered",
        actor=actor,
        result="success",
        payload={
            "trigger": trigger,
            "paths": [str(target.path) for target in sorted_targets],
            "agent_ids": [target.agent_id for target in sorted_targets],
        },
        timestamp=timestamp,
    )
    observed_entries: list[ObservedEntry] = []
    for target in sorted_targets:
        observed_entries.extend(parse_watched_config(target))
    return ingest_observed_entries(
        connection,
        observed_entries,
        actor=actor,
        run_id=f"{run_id}:observed",
        timestamp=timestamp,
    )


def parse_watched_config(target: WatchedConfigPath) -> list[ObservedEntry]:
    """Parse one watched first-wave agent config into observed entries."""
    if target.agent_kind == "codex":
        return parse_codex_config(target.path, agent_id=target.agent_id).observed_entries
    if target.agent_kind == "claude_code":
        return parse_claude_code_config(target.path, agent_id=target.agent_id).observed_entries
    if target.agent_kind == "gemini":
        return parse_gemini_config(target.path, agent_id=target.agent_id).observed_entries
    if target.agent_kind == "cline":
        return parse_cline_config(target.path, agent_id=target.agent_id).observed_entries
    if target.agent_kind == "opencode":
        return parse_opencode_config(target.path, agent_id=target.agent_id).observed_entries
    raise ValueError(f"unsupported watched agent_kind: {target.agent_kind}")


def file_signature(path: Path) -> FileSignature | None:
    """Return file signature or None when a watched file is absent."""
    if not path.is_file():
        return None
    stat = path.stat()
    return FileSignature(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        digest=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _trigger_event_id(run_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0audit.triggered".encode()).hexdigest()
    return f"evt_{digest[:24]}"
