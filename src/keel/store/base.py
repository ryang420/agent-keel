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


class ReviewNotRetryable(StoreError):
    """Only a failed memory review can be retried."""


class ReviewSourceGone(StoreError):
    """The session a memory review read no longer exists in that generation."""


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
    learning: bool = False


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
    pruned_id: int | None = None
    tools: tuple[str, ...] | None = None
    generation: str | None = None


@dataclass(frozen=True)
class StoredMessage:
    id: int
    session_id: str
    run_id: str | None
    message: Message
    created_at: float


@dataclass(frozen=True)
class HistoryRow:
    """A search candidate: an indexed message, with its session's title and the status of the run that wrote it."""
    message_id: int
    session_id: str
    title: str | None
    role: str
    text: str            # the redacted searchable text
    created_at: float
    run_status: str | None  # None when the message has no run (imported)
    hits: int            # distinct query tokens it contains


@dataclass(frozen=True)
class HistoryMessage:
    """A stored message read for context, with the status of the run that wrote it."""
    id: int
    session_id: str
    message: Message
    created_at: float
    run_status: str | None


@dataclass(frozen=True)
class MemorySource:
    """Who committed a memory change. Set from trusted context, never from model arguments."""
    kind: str  # "foreground", "review", "consumer" or "legacy"
    run_id: str | None = None
    session_id: str | None = None
    message_id: int | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class MemoryItem:
    """An active memory entry with the source of its latest change."""
    id: str
    target: str
    text: str
    version: int
    created_at: float
    updated_at: float
    source_kind: str | None
    run_id: str | None
    session_id: str | None
    message_id: int | None


@dataclass(frozen=True)
class ReviewJob:
    """A memory review job as operators see it: bounds, status and usage, never transcript text."""
    id: str
    profile_id: str
    session_id: str
    status: str  # "pending", "running", "done", "failed" or "discarded"
    provider: str
    model: str
    source_from: int  # exclusive
    source_through: int  # inclusive
    cursor_message: int
    cursor_offset: int
    attempts: int
    error_code: str | None
    error: str | None
    usage: list[dict]
    created_at: float
    updated_at: float
    finished_at: float | None


@dataclass(frozen=True)
class ReviewClaim:
    """A leased job. The token fences every later write: a worker whose lease lapsed cannot commit."""
    job: ReviewJob
    token: str
    generation: str
    profile_incarnation: str


@dataclass(frozen=True)
class RunFeedback:
    run_id: str
    session_id: str
    rating: str  # "up" or "down"
    comment: str | None
    observation_id: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class SkillOutcome:
    """One learned skill version over the statistics window: runs that loaded it, their ratings and end states."""
    loads: int
    up: int
    down: int
    completed: int
    failed: int
    cancelled: int


@dataclass(frozen=True)
class Observation:
    """A lesson observation: what a review, feedback or a skill request saw, awaiting or past the dream."""
    id: str
    session_id: str
    topic: str
    situation: str
    approach: str
    outcome: str
    evidence: tuple[int, ...]
    loaded: tuple[tuple[str, int], ...]  # (learned skill id, version) loaded by the cited runs
    state: str
    review_job_id: str | None
    run_id: str | None
    consumed_by: str | None
    created_at: float


@dataclass(frozen=True)
class SkillSource:
    """Who changed a learned skill. Set from trusted context, never from model arguments."""
    kind: str  # "foreground", "dream", "consumer" or "automatic"
    run_id: str | None = None
    session_id: str | None = None
    message_id: int | None = None
    tool_call_id: str | None = None
    job_id: str | None = None


@dataclass(frozen=True)
class LearnedSkill:
    id: str
    name: str
    description: str
    category: str | None
    body: str
    version: int
    state: str  # "active", "suspended" or "retired"
    origin: str  # "foreground", "dream" or "consumer"
    consumer_changed_at: float | None
    created_at: float
    updated_at: float

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description, "category": self.category,
                "state": self.state, "version": self.version}


@dataclass(frozen=True)
class LearnedSkillVersion:
    version: int
    name: str
    description: str
    category: str | None
    body: str
    change: str
    source_kind: str
    run_id: str | None
    session_id: str | None
    job_id: str | None
    rationale: str | None
    created_at: float
    message_ids: tuple[int, ...]
    observation_ids: tuple[str, ...]


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
    capped: bool = False


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
