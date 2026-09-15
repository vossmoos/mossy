"""One pass at a time over one Wrike task, on this machine.

the per-task lock. Loaded by file path. The locks live in a throwaway directory — the conftest's autouse
fixture points ``WRIKE_TASK_LOCK_DIR`` at one — so the operator's own lock directory is never
touched, and ``TestItNeverTouchesTheOperatorsDirectory`` makes that an assertion.

Written after five mutations to the lock survived the whole suite of 550: the lock always
granted, the lock made shared, the one-machine limit deleted, and each writer made to skip it.
Nothing tested it at all.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


locking = _load("task_lock")
TASK = "MAAAAAEXmain"
OTHER = "MAAAAAEXgerm"


def _holder(lock_dir, task=TASK):
    """A second **process** holding the lock until told to let go."""
    module_path = str(_SCRIPTS / "task_lock.py")
    code = (
        "import importlib.util,sys\n"
        f"spec=importlib.util.spec_from_file_location('task_lock',{module_path!r})\n"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
        f"with m.task_lock({task!r}, lock_dir={str(lock_dir)!r}) as state:\n"
        "    print(state, flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    return proc, proc.stdout.readline().strip()


def _release(proc):
    proc.stdin.write("\n")
    proc.stdin.flush()
    proc.wait(timeout=10)


class TestOneTaskOnePass:
    def test_the_first_pass_holds_it(self, tmp_path):
        with locking.task_lock(TASK, lock_dir=tmp_path) as state:
            assert state == locking.HELD

    def test_a_second_pass_in_this_process_is_told_busy(self, tmp_path):
        """``flock`` is per open file, so a second open in the same process conflicts too —
        which is what makes the thread-level flow tests meaningful."""
        with locking.task_lock(TASK, lock_dir=tmp_path) as first:
            with locking.task_lock(TASK, lock_dir=tmp_path) as second:
                assert (first, second) == (locking.HELD, locking.BUSY)

    def test_it_is_released_when_the_pass_ends(self, tmp_path):
        with locking.task_lock(TASK, lock_dir=tmp_path):
            pass
        with locking.task_lock(TASK, lock_dir=tmp_path) as again:
            assert again == locking.HELD

    def test_it_is_released_when_the_pass_raises(self, tmp_path):
        with pytest.raises(RuntimeError):
            with locking.task_lock(TASK, lock_dir=tmp_path):
                raise RuntimeError("the pass died")
        with locking.task_lock(TASK, lock_dir=tmp_path) as again:
            assert again == locking.HELD

    def test_two_tasks_do_not_block_each_other(self, tmp_path):
        with locking.task_lock(TASK, lock_dir=tmp_path) as a:
            with locking.task_lock(OTHER, lock_dir=tmp_path) as b:
                assert (a, b) == (locking.HELD, locking.HELD)

    def test_another_process_holding_it_makes_this_pass_busy(self, tmp_path):
        """The real case: one run holds the task and a command is run by hand."""
        proc, state = _holder(tmp_path)
        try:
            assert state == locking.HELD
            with locking.task_lock(TASK, lock_dir=tmp_path) as mine:
                assert mine == locking.BUSY
        finally:
            _release(proc)
        with locking.task_lock(TASK, lock_dir=tmp_path) as after:
            assert after == locking.HELD

    def test_this_process_holding_it_makes_another_busy(self, tmp_path):
        with locking.task_lock(TASK, lock_dir=tmp_path):
            proc, state = _holder(tmp_path)
            _release(proc)
        assert state == locking.BUSY

    def test_the_lock_file_is_never_deleted(self, tmp_path):
        """Removing it would race with the next process opening it by name."""
        with locking.task_lock(TASK, lock_dir=tmp_path):
            pass
        assert locking.lock_path(TASK, lock_dir=tmp_path).exists()


class TestUnavailableIsSaidNotHidden:
    """A lock that cannot be created is not a lock that is held. Reporting it as held would be
    the lock claiming to cover what it does not — worse than having no lock."""

    @pytest.fixture()
    def uncreatable(self, tmp_path):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x")
        return blocker / "locks"

    def test_an_uncreatable_lock_is_unavailable_not_held(self, uncreatable):
        with locking.task_lock(TASK, lock_dir=uncreatable) as state:
            assert state == locking.UNAVAILABLE
            assert state != locking.HELD

    def test_the_pass_still_runs_and_the_result_says_why(self, uncreatable):
        result = locking.locked_run(TASK, lambda: {"ok": True}, lock_dir=uncreatable)
        assert result["ok"] is True
        assert result["lock"] == locking.UNAVAILABLE
        assert result["lock_detail"] == locking.LOCK_UNAVAILABLE

    def test_a_held_lock_is_stamped_as_held(self, tmp_path):
        result = locking.locked_run(TASK, lambda: {"ok": True}, lock_dir=tmp_path)
        assert result["lock"] == locking.HELD
        assert "lock_detail" not in result

    def test_a_busy_lock_never_runs_the_work(self, tmp_path):
        ran = []
        with locking.task_lock(TASK, lock_dir=tmp_path):
            result = locking.locked_run(TASK, lambda: ran.append(1) or {"ok": True},
                                        lock_dir=tmp_path, busy_base={"posted": 0})
        assert ran == []
        assert result["reason"] == locking.TASK_IS_BUSY
        assert result["lock"] == locking.BUSY
        assert result["applied"] is False
        assert result["posted"] == 0

    def test_no_task_means_no_lock_and_the_work_still_runs(self, tmp_path):
        """Not a notification: nothing to lock and nothing will be written."""
        result = locking.locked_run(None, lambda: {"ok": True}, lock_dir=tmp_path)
        assert result == {"ok": True}


class TestTheLimitIsWrittenDown:
    """It does not cover two machines, and it says so with those words."""

    def test_the_constant_says_it(self):
        assert "does not cover two machines" in locking.ONE_MACHINE_ONLY

    def test_the_module_says_it(self):
        assert "IT DOES NOT COVER TWO MACHINES" in locking.__doc__

    def test_a_busy_answer_carries_it_to_whoever_reads_the_result(self):
        assert locking.ONE_MACHINE_ONLY in locking.busy_result(TASK)["detail"]

    def test_what_it_does_cover_is_named_too(self):
        assert "run by hand while the duty runs" in locking.ONE_MACHINE_ONLY


class TestItNeverTouchesTheOperatorsDirectory:
    def test_the_suite_locks_somewhere_else(self):
        default = Path("~/.local/share/mossy/task-locks").expanduser()
        assert locking.lock_directory() != default

    def test_an_odd_id_cannot_escape_the_directory(self, tmp_path):
        path = locking.lock_path("../../etc/passwd", lock_dir=tmp_path)
        assert path.parent == tmp_path
