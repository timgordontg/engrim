"""Defensive reader for Copilot CLI's undocumented ``events.jsonl`` format."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engrim.hosts.copilot.wiring import home

COMPATIBILITY_KEY = "copilot:transcript_compatibility"
SUPPORTED_STREAM_VERSION = 1


def cursor_key(session_id: str) -> str:
    return f"copilot:log_offset:{session_id}"


def state_key(session_id: str) -> str:
    return f"copilot:turn_state:{session_id}"


def newest_event_files(limit: int = 6) -> list[Path]:
    root = Path(home()) / "session-state"
    if not root.is_dir():
        return []
    paths = (path for path in root.glob("*/events.jsonl") if path.is_file())
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


@dataclass
class StreamMetadata:
    git_root: str | None = None
    cwd: str | None = None


@dataclass
class TurnState:
    stream_version: Any = None
    copilot_version: str | None = None

    @classmethod
    def from_json(cls, value: str | None) -> "TurnState":
        if not value:
            return cls()
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            stream_version=data.get("stream_version"),
            copilot_version=(
                data.get("copilot_version")
                if isinstance(data.get("copilot_version"), str)
                else None
            ),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "stream_version": self.stream_version,
                "copilot_version": self.copilot_version,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class AssistantMessage:
    timestamp: Any
    message_id: str
    content: str
    raw: str


@dataclass(frozen=True)
class SchemaError:
    code: str
    event_type: str | None
    fields: tuple[str, ...]
    stream_version: Any
    copilot_version: str | None
    fingerprint: str

    def marker(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "event_type": self.event_type,
            "fields": list(self.fields),
            "stream_version": self.stream_version,
            "copilot_version": self.copilot_version,
            "fingerprint": self.fingerprint,
        }


@dataclass
class ReadBatch:
    messages: list[AssistantMessage] = field(default_factory=list)
    offset: int = 0
    state: TurnState = field(default_factory=TurnState)
    compatible_assistant: bool = False
    error: SchemaError | None = None


def _field_names(event: object) -> tuple[str, ...]:
    if not isinstance(event, dict):
        return ()
    names = [str(key) for key in event]
    data = event.get("data")
    if isinstance(data, dict):
        names.extend(f"data.{key}" for key in data)
    return tuple(sorted(names))


def _schema_error(code: str, event: object, state: TurnState) -> SchemaError:
    event_type = event.get("type") if isinstance(event, dict) else None
    if not isinstance(event_type, str):
        event_type = None
    fields = _field_names(event)
    structural = {
        "code": code,
        "event_type": event_type,
        "fields": fields,
        "stream_version": state.stream_version,
        "copilot_version": state.copilot_version,
    }
    fingerprint = hashlib.sha256(
        json.dumps(structural, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return SchemaError(
        code=code,
        event_type=event_type,
        fields=fields,
        stream_version=state.stream_version,
        copilot_version=state.copilot_version,
        fingerprint=fingerprint,
    )


def read_events(path: Path, offset: int, state: TurnState | None = None) -> ReadBatch:
    """Read complete records from ``offset``, retaining the first unsafe record for retry."""
    current = state or TurnState()
    batch = ReadBatch(offset=offset, state=current)
    with path.open("rb") as stream:
        stream.seek(offset)
        while True:
            line_offset = stream.tell()
            raw_bytes = stream.readline()
            if not raw_bytes:
                break
            if not raw_bytes.endswith(b"\n"):
                batch.offset = line_offset
                break
            try:
                raw = raw_bytes[:-1].removesuffix(b"\r").decode("utf-8")
                event = json.loads(raw)
            except (UnicodeDecodeError, ValueError):
                batch.offset = line_offset
                batch.error = _schema_error("invalid_json", None, current)
                break
            if not isinstance(event, dict):
                batch.offset = line_offset
                batch.error = _schema_error("event_not_object", event, current)
                break

            event_type = event.get("type")
            data = event.get("data")
            if event_type == "session.start":
                if not isinstance(data, dict):
                    batch.offset = line_offset
                    batch.error = _schema_error("session_start_data_type", event, current)
                    break
                version = data.get("version")
                current.stream_version = (
                    version if isinstance(version, (int, str)) else type(version).__name__
                )
                current.copilot_version = (
                    data.get("copilotVersion")[:64]
                    if isinstance(data.get("copilotVersion"), str)
                    else None
                )
                if version is not None and version != SUPPORTED_STREAM_VERSION:
                    batch.offset = line_offset
                    batch.error = _schema_error("unsupported_stream_version", event, current)
                    break
            elif event_type == "assistant.message":
                if not isinstance(data, dict):
                    batch.offset = line_offset
                    batch.error = _schema_error("assistant_data_type", event, current)
                    break
                content = data.get("content")
                message_id = data.get("messageId")
                if not isinstance(content, str):
                    batch.offset = line_offset
                    batch.error = _schema_error("assistant_content_type", event, current)
                    break
                if not isinstance(message_id, str) or not message_id:
                    batch.offset = line_offset
                    batch.error = _schema_error("assistant_message_id", event, current)
                    break
                if not data.get("parentToolCallId"):
                    batch.compatible_assistant = True
                if not data.get("parentToolCallId") and content.strip():
                    batch.messages.append(
                        AssistantMessage(
                            timestamp=event.get("timestamp"),
                            message_id=message_id,
                            content=content.strip(),
                            raw=raw,
                        )
                    )

            batch.offset = stream.tell()
    return batch


def read_metadata(path: Path) -> StreamMetadata:
    """Read only through ``session.start`` to establish version and workspace ownership."""
    metadata = StreamMetadata()
    with path.open("rb") as stream:
        raw_bytes = stream.readline()
        if not raw_bytes.endswith(b"\n"):
            return metadata
        try:
            event = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return metadata
        if not isinstance(event, dict) or event.get("type") != "session.start":
            return metadata
        data = event.get("data")
        if not isinstance(data, dict):
            return metadata
        context = data.get("context")
        if not isinstance(context, dict):
            context = {}
        metadata.git_root = (
            context.get("gitRoot") if isinstance(context.get("gitRoot"), str) else None
        )
        cwd = context.get("cwd") or data.get("cwd") or event.get("cwd")
        metadata.cwd = cwd if isinstance(cwd, str) else None
    return metadata
