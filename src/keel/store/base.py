"""What the store holds, and how it fails."""

from __future__ import annotations

from dataclasses import dataclass

from keel.models import Message, Usage

ACTIVE = ("queued", "running", "stopping")
TERMINAL = ("completed", "failed", "cancelled")
TERMINAL_EVENT = {"completed": "run.completed", "failed": "run.failed", "cancelled": "run.cancelled"}
TITLE_RANK = {"derived": 0, "llm": 1, "user": 2}
WORKER_LOST_MESSAGE = "The worker running this run stopped responding."


class StoreError(Exception):
    pass


class UnknownRun(ValueError):
    """An event write named a run that does not exist; nothing was written."""


class TerminalEventType(ValueError):
    """A terminal event may only be written by the run transition that commits it."""


class SessionNotFound(StoreError):
    pass


class SessionBusy(StoreError):
    pass


class TitleInUse(StoreError):
    pass


class ProfileTenantMismatch(StoreError):
    pass


class ProfileDeletionBusy(StoreError):
    """The profile is being deleted; the caller's write must not proceed."""


class ProfileDeletionConflict(StoreError):
    """A deletion operation could not perform its expected transition."""


class ProfileIncarnationMismatch(StoreError):
    """A delayed writer belongs to an older incarnation of a deterministic profile id."""


class UniqueViolation(Exception):
    """Raised by the database adapter when a unique constraint rejects a write."""


class OwnershipError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    id: str
    models: list[dict]
    sandbox: dict
    soul: str
    skills: list[str]
    updated_at: float
    memory: bool = False
    tenant_id: int | None = None
    incarnation: str | None = None


@dataclass(frozen=True)
class Session:
    id: str
    profile_id: str
    title: str | None
    title_source: str | None
    created_at: float
    last_active_at: float
    message_count: int
    system_prompt: str | None = None
    provider: str | None = None
    model: str | None = None
    cwd: str | None = None
    summary: str | None = None
    summary_head_id: int | None = None
    summary_tail_id: int | None = None
    context_tokens: int | None = None
    context_message_id: int | None = None
    compaction_failed_at: float | None = None
    turns_since_memory: int = 0
    pruned_id: int | None = None


@dataclass(frozen=True)
class StoredMessage:
    id: int
    session_id: str
    run_id: str | None
    message: Message
    created_at: float


@dataclass(frozen=True)
class ImportResult:
    """What SqlStore.import_session did with one conversation."""

    status: str  # "imported" or "exists"
    profile_created: bool
    message_ids: tuple[int, ...]
    title: str | None = None


@dataclass(frozen=True)
class Run:
    id: str
    profile_id: str
    session_id: str
    status: str
    provider: str
    model: str
    reasoning_effort: str | None
    input: list[dict]
    instructions: str | None
    output: str
    usage: Usage
    error_code: str | None
    error: str | None
    lease_owner: str | None
    lease_expires_at: float | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    current_exec_id: str | None = None
    profile_incarnation: str | None = None


@dataclass(frozen=True)
class Event:
    run_id: str
    seq: int
    type: str
    data: dict
    at: float


@dataclass(frozen=True)
class RunTransition:
    """A terminal transition's outcome: the run, and the event this call committed (None when it did not win)."""

    run: Run
    event: Event | None
