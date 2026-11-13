"""Public data-transfer objects and small domain helpers for bmem.

The module is intentionally dependency-light besides Pydantic.  It is imported by
every transport (HTTP, MCP, CLI and adapters), so it contains no I/O and no
store/runtime coupling.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KIND_VALUES = ("episode", "fact", "decision", "method", "preference")
SOURCE_TYPE_VALUES = (
    "user_declaration",
    "user_dialogue",
    "verification",
    "agent_inference",
    "external",
    "injection_reference",
)
SENSITIVITY_VALUES = ("normal", "private")
TARGET_VALUES = ("local", "cloud")
CANDIDATE_ACTIONS = ("ADD", "UPDATE", "NOOP", "DISPUTE", "QUARANTINE")

MemoryKind = Literal["episode", "fact", "decision", "method", "preference"]
SourceType = Literal[
    "user_declaration",
    "user_dialogue",
    "verification",
    "agent_inference",
    "external",
    "injection_reference",
]
Sensitivity = Literal["normal", "private"]
Target = Literal["local", "cloud"]

_MAX_BODY = 100_000


def now_iso() -> str:
    """Return a normalized UTC ISO-8601 timestamp.

    All timestamps persisted by bmem use the same precision (microseconds) so
    lexical comparison in SQLite remains chronological.
    """

    return (
        datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def iso_utc(value: datetime) -> str:
    """Normalize a datetime to bmem's canonical UTC ISO representation."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, treating a naive value as UTC."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# Credential redaction
# ---------------------------------------------------------------------------

_REDACTED = "[REDACTED]"

_PRIVATE_KEY_RE = re.compile(
    r"(-----BEGIN[^\n]*PRIVATE KEY-----)(.*?)(-----END[^\n]*PRIVATE KEY-----)",
    re.S,
)
_BEARER_RE = re.compile(
    r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=\-]{6,})"
)
_AUTHORIZATION_HEADER_RE = re.compile(
    r"(?i)(\b(?:proxy-)?authorization\s*[:=]\s*)"
    r"((?:basic|bearer|digest|token|apikey|negotiate)\s+)?"
    r"([^\s,;]+)"
)
_COOKIE_HEADER_RE = re.compile(
    r"(?i)(\b(?:set-)?cookie\s*[:=]\s*)([^\r\n]+)"
)
_CONNECTION_PASSWORD_RE = re.compile(
    r"(?i)(\b(?:postgres(?:ql)?|mysql|redis|amqp|mongodb(?:\+srv)?|https?)"
    r"://[^:\s/@]+:)([^@\s/]+)(@)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:api[\s_-]?key|secret[\s_-]?key|access[\s_-]?key|private[\s_-]?key|"
    r"secret|password|passwd|pwd|token|access[\s_-]?token|refresh[\s_-]?token|"
    r"auth[\s_-]?token|session[\s_-]?token|client[\s_-]?secret|credentials?)"
    r"\s*[:=]\s*)([\"']?)([^\s\"',;}\]]{3,})(\2)"
)
_PREFIXED_TOKEN_RE = re.compile(
    r"(?i)(^|[\s\"'=:,])((?:"
    r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{6,}|"
    r"ghp_[A-Za-z0-9]{6,}|github_pat_[A-Za-z0-9_]{6,}|"
    r"xox[abp]-[A-Za-z0-9-]{6,}|"
    r"AKIA[0-9A-Z]{12,}|"
    r"ASIA[0-9A-Z]{12,}|"
    r"AIza[0-9A-Za-z_-]{8,}|"
    r"glpat-[A-Za-z0-9_-]{6,}|"
    r"hf_[A-Za-z0-9]{6,}"
    r"))"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]*){2}\b"
)
_PRIVATE_VALUE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "secret_key",
        "access_key",
        "private_key",
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "session_token",
        "client_secret",
        "authorization",
        "cookie",
        "set-cookie",
        "credentials",
    }
)


def redact(text: str) -> str:
    """Redact common credential shapes from text before persistence/logging."""

    if not isinstance(text, str) or not text:
        return text
    value = _PRIVATE_KEY_RE.sub(
        lambda m: f"{m.group(1)}{_REDACTED}{m.group(3)}", text
    )
    value = _BEARER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", value)
    value = _AUTHORIZATION_HEADER_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2) or ''}{_REDACTED}", value
    )
    value = _COOKIE_HEADER_RE.sub(
        lambda m: f"{m.group(1)}{_REDACTED}", value
    )
    value = _CONNECTION_PASSWORD_RE.sub(
        lambda m: f"{m.group(1)}{_REDACTED}{m.group(3)}", value
    )
    value = _SECRET_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}{m.group(2)}", value
    )
    value = _PREFIXED_TOKEN_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", value)
    value = _JWT_RE.sub(_REDACTED, value)
    return value


def _secret_key(name: Any) -> bool:
    normalized = str(name).strip().lower().replace(" ", "_").replace("-", "_")
    return normalized in _PRIVATE_VALUE_KEYS or normalized.endswith(
        ("_token", "_secret", "_password", "_api_key", "_private_key")
    )


def redact_value(value: Any) -> Any:
    """Recursively redact strings and sensitive mapping keys."""

    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if _secret_key(key):
                out[key] = _REDACTED
            else:
                out[key] = redact_value(item)
        return out
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Domain error
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """An error safe to surface through HTTP/CLI without private content."""

    def __init__(self, status_code: int, message: str):
        self.status_code = int(status_code)
        self.message = str(message)
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover - defensive, no content
        return f"DomainError(status_code={self.status_code}, message={self.message!r})"


# ---------------------------------------------------------------------------
# DTO validation helpers
# ---------------------------------------------------------------------------


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON serializable") from exc
    return value


def _json_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON serializable") from exc
    return value


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class EventInput(BaseModel):
    """Raw event accepted from a harness, hook, importer or MCP client."""

    model_config = ConfigDict(extra="ignore")

    body: str = Field(min_length=1, max_length=_MAX_BODY)
    harness: str = "manual"
    device_id: str = "local"
    native_event_id: str | None = None
    operation_id: str | None = None
    source_type: SourceType = "user_dialogue"
    occurred_at: datetime | None = None
    session_id: str = "manual"
    project: str = ""
    sensitivity: Sensitivity = "normal"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("body must not be empty")
        if len(value) > _MAX_BODY:
            raise ValueError("body is too long")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_utc(cls, value: datetime | None) -> datetime | None:
        return _normalize_datetime(value)

    @field_validator("metadata")
    @classmethod
    def _metadata_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value, "metadata")

    @model_validator(mode="after")
    def _fill_identities(self) -> "EventInput":
        if not self.native_event_id:
            self.native_event_id = str(uuid.uuid4())
        if not self.operation_id:
            self.operation_id = str(uuid.uuid4())
        if self.occurred_at is None:
            self.occurred_at = datetime.now(UTC)
        return self


class DeclarationInput(EventInput):
    """Explicit user declaration / correction (the immediate T1 path)."""

    source_type: Literal["user_declaration"] = "user_declaration"
    kind: MemoryKind = "fact"
    fact_key: str | None = None
    memory_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    conditions: dict[str, Any] = Field(default_factory=dict)
    derived_from: list[str] = Field(default_factory=list)

    @field_validator("fact_key")
    @classmethod
    def _fact_key_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not str(value).strip():
            raise ValueError("fact_key must not be empty")
        return value

    @field_validator("conditions")
    @classmethod
    def _conditions_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value, "conditions")

    @field_validator("derived_from")
    @classmethod
    def _derived_list(cls, value: list[str]) -> list[str]:
        value = _json_list(value, "derived_from")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("derived_from entries must be non-empty strings")
        return value

    @model_validator(mode="after")
    def _correction_requires_revision(self) -> "DeclarationInput":
        if self.memory_id and self.expected_revision is None:
            raise ValueError("memory_id corrections require expected_revision")
        return self


class RecallInput(BaseModel):
    """Recall request validated before retrieval and injection budgeting."""

    model_config = ConfigDict(extra="ignore")

    query: str = ""
    harness: str = "manual"
    session_id: str = "manual"
    turn_id: str = ""
    task: str = ""
    context_epoch: str = ""
    budget: int = Field(default=2000, ge=0, le=10_000)
    target: Target = "cloud"
    conditions: dict[str, Any] = Field(default_factory=dict)
    project: str = ""

    @field_validator("conditions")
    @classmethod
    def _conditions_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value, "conditions")


class CandidateInput(BaseModel):
    """A structured model candidate validated at the model boundary.

    Trust and sensitivity are deliberately absent: the store derives those from
    the source event and a candidate can never elevate either of them.
    """

    model_config = ConfigDict(extra="ignore")

    body: str = Field(min_length=1, max_length=_MAX_BODY)
    kind: MemoryKind
    fact_key: str | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    derived_from: list[str] = Field(default_factory=list)
    action: Literal["ADD", "UPDATE", "NOOP", "DISPUTE", "QUARANTINE"]
    memory_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)

    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("body must not be empty")
        return value

    @field_validator("fact_key")
    @classmethod
    def _fact_key_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not str(value).strip():
            raise ValueError("fact_key must not be empty")
        return value

    @field_validator("conditions")
    @classmethod
    def _conditions_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_object(value, "conditions")

    @field_validator("derived_from")
    @classmethod
    def _derived_list(cls, value: list[str]) -> list[str]:
        value = _json_list(value, "derived_from")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("derived_from entries must be non-empty strings")
        return value


def validation_message(model: str, exc: Exception) -> str:
    """Build a content-free validation message from a Pydantic error."""

    errors = getattr(exc, "errors", None)
    if callable(errors):
        pieces: list[str] = []
        for item in errors():
            loc = ".".join(str(part) for part in item.get("loc", ())) or "input"
            msg = redact(str(item.get("msg", "invalid value")))
            pieces.append(f"{loc}: {msg}")
        if pieces:
            return f"invalid {model}: " + "; ".join(pieces[:8])
    return f"invalid {model}"


__all__ = [
    "CANDIDATE_ACTIONS",
    "CandidateInput",
    "DeclarationInput",
    "DomainError",
    "EventInput",
    "KIND_VALUES",
    "MemoryKind",
    "RecallInput",
    "SENSITIVITY_VALUES",
    "SOURCE_TYPE_VALUES",
    "Sensitivity",
    "SourceType",
    "TARGET_VALUES",
    "Target",
    "iso_utc",
    "now_iso",
    "parse_iso",
    "redact",
    "redact_value",
    "validation_message",
]
