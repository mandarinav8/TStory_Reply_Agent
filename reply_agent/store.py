"""Locked, atomic persistence and duplicate suppression."""

import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Final, assert_never, override
from uuid import uuid4
from zoneinfo import ZoneInfo

import portalocker

from .models import (
    Action,
    Attempt,
    Connection,
    Ledger,
    Receipt,
    Request,
    RunOutcome,
    RunReceipt,
    RunRecord,
    RunTargets,
    Visit,
)

SEOUL: Final = ZoneInfo("Asia/Seoul")
DAILY_LIMIT: Final = 100


class BlockedError(Exception):
    """Keep exception traceback mutable for contextmanager propagation."""

    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @override
    def __str__(self) -> str:
        return self.reason


@contextmanager
def locked(directory: Path) -> Generator[None]:
    """Serialize all ledger mutations across processes."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "ledger.lock").open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            yield
        finally:
            portalocker.unlock(handle)


def read(directory: Path) -> Ledger:
    """Read existing state without recovering corrupt records silently."""
    path = directory / "ledger.json"
    return (
        Ledger.model_validate_json(path.read_text(encoding="utf-8"))
        if path.exists()
        else Ledger()
    )


def save(directory: Path, ledger: Ledger) -> None:
    """Persist before browser actions; atomic replacement survives interruption."""
    temporary = directory / "ledger.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        _ = handle.write(ledger.model_dump_json(indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    _ = temporary.replace(directory / "ledger.json")


def mark_visit(directory: Path, url: str) -> Visit:
    """Record a candidate URL before the browser opens it."""
    with locked(directory):
        ledger = read(directory)
        now = datetime.now(SEOUL)
        today = now.date()
        if any(
            item.url == url and item.visited_at.astimezone(SEOUL).date() == today
            for item in ledger.visits
        ):
            raise BlockedError("Article was already visited today")
        visit = Visit(url=url, visited_at=now)
        save(directory, ledger.model_copy(update={"visits": (*ledger.visits, visit)}))
        return visit


def visited_today(directory: Path, url: str) -> bool:
    """Check today's visit ledger without opening the article."""
    now = datetime.now(SEOUL).date()
    with locked(directory):
        ledger = read(directory)
        return any(
            item.url == url and item.visited_at.astimezone(SEOUL).date() == now
            for item in ledger.visits
        )


def reserve(directory: Path, request: Request) -> Attempt:
    """Reserve once, enforcing identity, action keys and daily budgets."""
    with locked(directory):
        if (directory / "STOP").exists():
            raise BlockedError("STOP file is present")
        expected = Connection.model_validate_json(
            (directory / "connection.json").read_text(encoding="utf-8"),
        )
        if request.connection != expected:
            raise BlockedError("Browser/account identity does not match local config")
        ledger = read(directory)
        if request.run_id is None:
            raise BlockedError("Start a scheduled run before reserving actions")
        run = next((item for item in ledger.runs if item.id == request.run_id), None)
        if run is None or run.receipt is not None:
            raise BlockedError("Scheduled run is missing or already closed")
        if run.targets is None:
            raise BlockedError("Legacy run has no declared targets")
        if any(item.receipt is None for item in ledger.attempts):
            raise BlockedError("Pending action: inspect the browser before continuing")
        if any(item.request.key == request.key for item in ledger.attempts):
            raise BlockedError("Action already reserved; automatic retry is forbidden")
        confirmed = sum(
            item.request.run_id == run.id
            and item.request.action == request.action
            and item.receipt is not None
            and item.receipt.outcome == "confirmed"
            for item in ledger.attempts
        )
        if confirmed >= run.targets.for_action(request.action):
            raise BlockedError("Run action target already reached or excluded")
        now = datetime.now(SEOUL)
        today = [
            item
            for item in ledger.attempts
            if item.created_at.astimezone(SEOUL).date() == now.date()
        ]
        if sum(item.request.action == request.action for item in today) >= DAILY_LIMIT:
            raise BlockedError("Daily action budget reached")
        attempt = Attempt(id=uuid4(), created_at=now, request=request)
        save(
            directory,
            ledger.model_copy(update={"attempts": (*ledger.attempts, attempt)}),
        )
        return attempt


def start_run(directory: Path, slot: datetime, targets: RunTargets) -> RunRecord:
    """Persist explicit goals before any browser action is reserved."""
    with locked(directory):
        if (directory / "STOP").exists():
            raise BlockedError("STOP file is present")
        ledger = read(directory)
        if any(item.receipt is None for item in ledger.runs):
            raise BlockedError("Previous scheduled run is still open")
        if any(item.slot == slot for item in ledger.runs):
            raise BlockedError("Scheduled slot already recorded")
        run = RunRecord(
            id=uuid4(), slot=slot, started_at=datetime.now(SEOUL), targets=targets
        )
        save(directory, ledger.model_copy(update={"runs": (*ledger.runs, run)}))
        return run


def finish_run(directory: Path, receipt: RunReceipt) -> None:
    """Close a scheduled run with its target counts and stop reason."""
    if receipt.termination is None:
        raise BlockedError("A machine-readable run termination is required")
    with locked(directory):
        ledger = read(directory)
        target = next((item for item in ledger.runs if item.id == receipt.run_id), None)
        if target is None:
            raise BlockedError("Unknown scheduled run ID")
        if target.receipt is not None:
            raise BlockedError("Scheduled run already closed")
        attempts = [x for x in ledger.attempts if x.request.run_id == target.id]
        counts = tuple(
            sum(
                x.request.action == action
                and x.receipt is not None
                and x.receipt.outcome == "confirmed"
                for x in attempts
            )
            for action in Action
        )
        if counts != (
            receipt.confirmed_comments,
            receipt.confirmed_likes,
            receipt.confirmed_subscriptions,
        ):
            raise BlockedError("Run counts do not match confirmed action receipts")
        match receipt.outcome:
            case RunOutcome.COMPLETED:
                if target.targets is None:
                    raise BlockedError("Legacy run has no declared targets")
                if any(x.receipt is None for x in attempts) or any(
                    count < target.targets.for_action(action)
                    for action, count in zip(Action, counts, strict=True)
                ):
                    raise BlockedError("Declared run targets have not been confirmed")
            case RunOutcome.EXHAUSTED | RunOutcome.BLOCKED | RunOutcome.CANCELLED:
                pass
            case unreachable:
                assert_never(unreachable)
        completed = target.model_copy(update={"receipt": receipt})
        save(
            directory,
            ledger.model_copy(
                update={
                    "runs": tuple(
                        completed if item.id == target.id else item
                        for item in ledger.runs
                    )
                }
            ),
        )


def finish(directory: Path, receipt: Receipt) -> None:
    """Record confirmation or uncertainty without enabling a second submission."""
    with locked(directory):
        ledger = read(directory)
        target = next(
            (item for item in ledger.attempts if item.id == receipt.attempt_id), None
        )
        if target is None:
            raise BlockedError("Unknown attempt ID")
        if target.receipt is not None:
            raise BlockedError("Attempt already closed")
        completed = target.model_copy(update={"receipt": receipt})
        save(
            directory,
            ledger.model_copy(
                update={
                    "attempts": tuple(
                        completed if item.id == target.id else item
                        for item in ledger.attempts
                    )
                }
            ),
        )
