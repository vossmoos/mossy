# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""One pass at a time over one Wrike task, on this machine.

The duplicate this closes is not a retry — retries are covered, by the Jira label
(the create step) and by the comment anchor (the comment copy). It is **two passes overlapping**, and it was
measured before it was written: two concurrent passes over one task produced **two Jira issues
and two copies of every comment**, none of which can be deleted.

The reason neither guard catches it is that both read before they write, and nothing holds the
gap. Pass A searches, finds nothing; pass B searches, finds nothing; both create. Pass A reads
the anchors, sees none; pass B reads the anchors, sees none; both post. The label and the anchor
are written by the very step that is racing, so they cannot arbitrate it.

★ **IT DOES NOT COVER TWO MACHINES.** This is one lock file on one filesystem, taken with
``flock``. Two processes on this machine are serialised; two machines running this flow against
the same Jira are not, and neither is a network filesystem, where ``flock`` is unreliable. Said
plainly because a lock that is believed to cover more than it does is worse than no lock: it
turns "we know this can happen" into "we solved that", and the duplicate it then lets through is
one nobody is looking for.

What it does cover is what actually happens here: **two overlapping runs, or a
command run by hand while another run is going.** That is the whole population of real passes
today — there is one machine.

**It refuses rather than waits.** A pass that cannot get the lock returns saying the task is
busy and writes nothing. Waiting would queue a second pass behind the first only to have it
discover, correctly, that everything it wanted to do is already done — and a queue of those is
how a process run every five minutes ends up with passes stacked on each other.

The lock file is never deleted. Removing it would race with the next process opening it by name,
and an empty file per task is nothing: they are a few bytes each and they are the lock's
identity.

Environment:

    WRIKE_TASK_LOCK_DIR   default ~/.local/share/mossy/task-locks

Self-contained: no ``import mossy``, no ``python-dotenv``, no database, no network. It touches
one empty file per task and nothing else.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
from pathlib import Path
from typing import Any, Iterator

_DEFAULT_LOCK_DIR = "~/.local/share/mossy/task-locks"

#: Said wherever the lock could be mistaken for a guarantee. Kept as one string so the modules
#: that take the lock report the same limit the lock states about itself.
ONE_MACHINE_ONLY = (
    "This lock covers one machine. Two overlapping passes here — the duty running twice, or a "
    "command run by hand while the duty runs — are serialised. It does not cover two machines, "
    "and it does not cover a network filesystem, where flock is unreliable. On those, two "
    "passes can still create a duplicate issue or copy a comment twice."
)

#: What a caller reports when somebody else holds the lock.
TASK_IS_BUSY = "task_is_busy"

#: The three states ``task_lock`` yields. Strings, not a bool: "held" and "could not be taken
#: at all" are different facts, and a bool would have to lie about one of them.
HELD = "held"
BUSY = "busy"
UNAVAILABLE = "unavailable"

#: Said when the lock could not be created, so the pass is running unserialised and knows it.
LOCK_UNAVAILABLE = (
    "The per-task lock could not be created, so this pass ran without it. The Jira label and "
    "the comment anchor still guard every retry; what is not guarded is another pass running at "
    "the same moment."
)


def lock_directory() -> Path:
    return Path(os.environ.get("WRIKE_TASK_LOCK_DIR") or _DEFAULT_LOCK_DIR).expanduser()


def lock_path(task_api_id: str, *, lock_dir: Path | str | None = None) -> Path:
    """One file per task. The name is the task id, with anything unusual replaced.

    A Wrike API id is letters and digits, so the substitution never fires on real input. It is
    here because two ids that collapsed to one name would **share** a lock, which over-locks —
    a pass waits that need not have. The other direction, two tasks that should share a lock and
    do not, is the one that duplicates, and a substitution cannot cause it.
    """
    directory = Path(lock_dir).expanduser() if lock_dir else lock_directory()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_api_id).strip()) or "_"
    return directory / f"{safe}.lock"


@contextlib.contextmanager
def task_lock(task_api_id: str, *, lock_dir: Path | str | None = None) -> Iterator[str]:
    """Hold the lock for one task, or say why not. Yields ``HELD``, ``BUSY`` or ``UNAVAILABLE``.

    ``BUSY`` is an ordinary state of the world, like an unconfigured client: another pass has
    the task, and the caller's answer is to stop and write nothing, not to fail.

    ``UNAVAILABLE`` means the lock file could not be created at all — no directory, a read-only
    filesystem. The caller **proceeds** and **says so**. Refusing to work would stop the flow
    dead on a machine where nothing is racing, and the guards underneath (the Jira label and the
    comment anchor) still hold for everything except a genuine overlap. What must not happen is
    the third option, proceeding while reporting the lock as held — that is a lock claiming to
    cover what it does not, which is worse than no lock.

    Must wrap the **read as well as the write**: the race is between deciding and acting, so a
    lock taken just before the write leaves the gap it was meant to close.
    """
    path = lock_path(task_api_id, lock_dir=lock_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        yield UNAVAILABLE
        return
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield BUSY
            return
        try:
            yield HELD
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def locked_run(task_api_id: str | None, work, *, lock_dir: Path | str | None = None,
               busy_base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run ``work()`` inside the task's lock and stamp the result with what the lock was.

    The one place both writers go through, so the create step and the comment copy cannot drift in how they treat
    a busy or unavailable lock. ``work`` must do the deciding **and** the writing.
    """
    if not task_api_id:
        return work()
    with task_lock(task_api_id, lock_dir=lock_dir) as state:
        if state == BUSY:
            return busy_result(task_api_id, busy_base)
        result = work()
    result["lock"] = state
    if state == UNAVAILABLE:
        result["lock_detail"] = LOCK_UNAVAILABLE
    return result


def busy_result(task_api_id: str, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """The answer a caller returns when the lock is held elsewhere. Nothing was written."""
    return {
        **(base or {}),
        "applied": False,
        "reason": TASK_IS_BUSY,
        "lock": BUSY,
        "task_api_id": str(task_api_id).strip(),
        "detail": (
            "Another pass is already working on this task on this machine, so nothing was "
            "written. " + ONE_MACHINE_ONLY
        ),
    }


__all__ = [
    "BUSY",
    "HELD",
    "LOCK_UNAVAILABLE",
    "ONE_MACHINE_ONLY",
    "TASK_IS_BUSY",
    "UNAVAILABLE",
    "busy_result",
    "locked_run",
    "lock_directory",
    "lock_path",
    "task_lock",
]
