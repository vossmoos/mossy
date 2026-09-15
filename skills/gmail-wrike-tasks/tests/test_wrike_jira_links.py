"""The register: which Jira issue belongs to a Wrike task, and which comments were already copied.

the label search. Loaded by file path. Every test gets its own DuckDB file under ``tmp_path``, so the real
register under ``~/.local/share`` is never opened — a test that wrote there would leave rows
behind that a later run would read as real links.

``TestRecordingACommentTwice`` is the one that guards the point of the whole table: the
notification e-mail carries no comment id, so the only way to know what is new is to fetch the
task's comments and subtract what is recorded. A pass that dies half way and runs again must not
be able to copy a comment into Jira twice.

``TestTheTableIsAnIndexNotTheTruth`` pins a sentence rather than a behaviour, deliberately: this
module stores and reads, and cannot enforce the rule. What it can do is make sure a caller
reading a row is told, in the row, that the row is not proof.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "wrike_jira_links.py"
)
_spec = importlib.util.spec_from_file_location("wrike_jira_links", _MODULE_PATH)
links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(links)

TASK = "MAAAAAEXmain"
OTHER_TASK = "MAAAAAEXgerm"


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "wrike-jira-links.duckdb"


@pytest.fixture(autouse=True)
def _never_the_real_register(tmp_path, monkeypatch):
    """Even a test that forgets ``db_path=`` writes into its own file, never ~/.local/share."""
    monkeypatch.setenv("WRIKE_JIRA_LINKS_DB", str(tmp_path / "fallback.duckdb"))


class TestTheLink:
    def test_a_task_remembers_its_issue(self, db):
        assert links.remember_link(TASK, "PROJ-99", "https://x/browse/PROJ-99", db_path=db)["ok"]
        row = links.linked_issue(TASK, db_path=db)
        assert row["jira_key"] == "PROJ-99"
        assert row["jira_url"] == "https://x/browse/PROJ-99"

    def test_an_unknown_task_is_none(self, db):
        assert links.linked_issue("MAAAAANOPE", db_path=db) is None

    def test_none_means_nothing_remembered_not_no_such_issue(self, db):
        """Stated as a test because the distinction is the whole design: this file can be new,
        or the row can have been written on another machine."""
        assert links.linked_issue(TASK, db_path=db) is None
        links.remember_link(TASK, "PROJ-99", db_path=db)
        assert links.linked_issue(TASK, db_path=db) is not None

    def test_two_tasks_do_not_collide(self, db):
        links.remember_link(TASK, "PROJ-1", db_path=db)
        links.remember_link(OTHER_TASK, "PROJ-2", db_path=db)
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == "PROJ-1"
        assert links.linked_issue(OTHER_TASK, db_path=db)["jira_key"] == "PROJ-2"

    def test_relinking_replaces_rather_than_accumulates(self, db):
        """One task, one issue. Keeping the old key would be exactly the dead row the module
        warns about, and the caller would have no way to tell which of two is current."""
        links.remember_link(TASK, "PROJ-1", db_path=db)
        links.remember_link(TASK, "PROJ-2", db_path=db)
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == "PROJ-2"

    @pytest.mark.parametrize("task,key", [("", "PROJ-1"), ("  ", "PROJ-1"), (TASK, ""), (TASK, "  ")])
    def test_a_blank_half_is_refused(self, db, task, key):
        assert links.remember_link(task, key, db_path=db)["ok"] is False
        assert links.linked_issue(TASK, db_path=db) is None


class TestTheTableIsAnIndexNotTheTruth:
    def test_a_row_never_claims_the_issue_still_exists(self, db):
        links.remember_link(TASK, "PROJ-99", db_path=db)
        row = links.linked_issue(TASK, db_path=db)
        assert row["still_exists_in_jira"] is None
        assert row["detail"] == links.TABLE_IS_AN_INDEX

    def test_the_rule_says_what_to_do_with_a_404(self):
        """Not "the row is dead: open a new issue" — the decision refuses exactly that
        (link_unconfirmable), and the rule attached to every row must not say the opposite."""
        assert "404" in links.TABLE_IS_AN_INDEX
        assert "never, on its own, a reason to open a new issue" in links.TABLE_IS_AN_INDEX
        assert "the row is dead: open a new issue" not in links.TABLE_IS_AN_INDEX

    def test_the_rule_is_written_in_the_module_and_not_only_here(self):
        header = _MODULE_PATH.read_text(encoding="utf-8").split('"""')[1]
        assert "index, not the truth" in header

    def test_the_module_names_the_unauthenticated_trap_on_that_rule(self):
        """Acting on a 404 with a bad token would open a duplicate issue per task: an
        unauthenticated client is served 404 by GET /issue/{key} too."""
        header = _MODULE_PATH.read_text(encoding="utf-8").split('"""')[1]
        assert "unauthenticated" in header


class TestRecordingACommentTwice:
    def test_the_first_time_records(self, db):
        result = links.remember_comment(TASK, "c1", db_path=db)
        assert result["ok"] is True and result["recorded"] is True

    def test_the_second_time_does_not_duplicate(self, db):
        links.remember_comment(TASK, "c1", db_path=db)
        again = links.remember_comment(TASK, "c1", db_path=db)
        assert again["ok"] is True
        assert again["recorded"] is False
        assert again["reason"] == "already_recorded"
        assert links.copied_comment_ids(TASK, db_path=db) == {"c1"}

    def test_ten_times_still_leaves_one_row(self, db):
        for _ in range(10):
            links.remember_comment(TASK, "c1", db_path=db)
        assert links.copied_comment_ids(TASK, db_path=db) == {"c1"}

    def test_the_same_comment_id_under_two_tasks_is_two_rows(self, db):
        """Comment ids are only unique within a task as far as we know, so the key is the pair."""
        links.remember_comment(TASK, "c1", db_path=db)
        links.remember_comment(OTHER_TASK, "c1", db_path=db)
        assert links.copied_comment_ids(TASK, db_path=db) == {"c1"}
        assert links.copied_comment_ids(OTHER_TASK, db_path=db) == {"c1"}

    @pytest.mark.parametrize("task,comment", [("", "c1"), (TASK, ""), (TASK, "   ")])
    def test_a_blank_half_is_refused(self, db, task, comment):
        assert links.remember_comment(task, comment, db_path=db)["ok"] is False


class TestWhatIsStillToCopy:
    def test_nothing_recorded_means_everything_is_new(self, db):
        comments = [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]
        assert [c["id"] for c in
                links.comments_not_yet_copied(TASK, comments, db_path=db)] == ["c1", "c2", "c3"]

    def test_recorded_ones_drop_out_and_the_order_is_kept(self, db):
        links.remember_comment(TASK, "c2", db_path=db)
        comments = [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]
        assert [c["id"] for c in
                links.comments_not_yet_copied(TASK, comments, db_path=db)] == ["c1", "c3"]

    def test_all_recorded_means_nothing_to_copy(self, db):
        for cid in ("c1", "c2"):
            links.remember_comment(TASK, cid, db_path=db)
        assert links.comments_not_yet_copied(TASK, [{"id": "c1"}, {"id": "c2"}], db_path=db) == []

    def test_another_tasks_record_does_not_filter_this_one(self, db):
        links.remember_comment(OTHER_TASK, "c1", db_path=db)
        assert [c["id"] for c in
                links.comments_not_yet_copied(TASK, [{"id": "c1"}], db_path=db)] == ["c1"]

    def test_a_comment_with_no_id_is_kept_not_dropped(self, db):
        """It cannot be matched against the record, and losing it in silence is worse than the
        operator seeing one they may already have."""
        out = links.comments_not_yet_copied(TASK, [{"text": "no id here"}], db_path=db)
        assert len(out) == 1

    def test_a_non_list_is_empty_rather_than_an_exception(self, db):
        assert links.comments_not_yet_copied(TASK, None, db_path=db) == []
        assert links.comments_not_yet_copied(TASK, {"id": "c1"}, db_path=db) == []


class TestItsOwnFile:
    def test_the_default_is_not_the_dedupe_cache(self, monkeypatch):
        """``dedupe.duckdb`` is a cache with a three-working-day window; this is a register that
        must still be readable a year later. Two lifetimes, two files."""
        monkeypatch.delenv("WRIKE_JIRA_LINKS_DB", raising=False)
        path = links.database_path()
        assert path.name == "wrike-jira-links.duckdb"
        assert "dedupe" not in str(path)

    def test_the_path_comes_from_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WRIKE_JIRA_LINKS_DB", str(tmp_path / "elsewhere.duckdb"))
        assert links.database_path() == tmp_path / "elsewhere.duckdb"

    def test_it_creates_the_directory_it_needs(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c.duckdb"
        assert links.remember_link(TASK, "PROJ-1", db_path=deep)["ok"] is True
        assert deep.exists()

    def test_a_read_never_creates_the_file(self, tmp_path):
        """the Wrike → Jira flow piece 3. Measured before: a test-mode describe created the file and both
        tables. A read on a register that does not exist yet answers "nothing recorded" and
        leaves nothing behind; only a write creates it."""
        deep = tmp_path / "a" / "b" / "c.duckdb"
        assert links.linked_issue(TASK, db_path=deep) is None
        assert links.copied_comment_ids(TASK, db_path=deep) == set()
        assert links.comments_not_yet_copied(TASK, [{"id": "C1"}], db_path=deep) == [{"id": "C1"}]
        assert not deep.exists()
        assert not (tmp_path / "a").exists()

    def test_the_two_tables_are_separate(self):
        assert links.LINK_TABLE != links.COMMENT_TABLE
        assert links.LINK_TABLE in links.CREATE_SQL
        assert links.COMMENT_TABLE in links.CREATE_SQL


class TestItTalksToNothing:
    def test_it_imports_no_transport(self):
        import re

        source = _MODULE_PATH.read_text(encoding="utf-8")
        imported = set(re.findall(r"^\s*import\s+([a-z_.0-9]+)", source, re.MULTILINE))
        assert imported == {"datetime", "duckdb", "os"}

    def test_it_is_self_contained(self):
        import re

        source = _MODULE_PATH.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:import|from)\s+(mossy|dotenv)\b", source, re.MULTILINE)

    def test_a_write_failure_is_reported_not_raised(self, tmp_path):
        """A store that cannot be opened must not take the flow down with it — **for writes**.

        the per-task lock changed the other half of this test. It used to assert that ``linked_issue``
        answered ``None`` for an unopenable store, which is the defect: ``None`` is "no row",
        and a caller creates a Jira issue on it. The reads now raise; see
        ``TestAnUnreadableStoreIsNotAnEmptyOne``.
        """
        not_a_dir = tmp_path / "file"
        not_a_dir.write_text("x")
        result = links.remember_link(TASK, "PROJ-1", db_path=not_a_dir / "inside.duckdb")
        assert result["ok"] is False
        assert result["reason"] == "database_error"


class TestAnUnreadableStoreIsNotAnEmptyOne:
    """the per-task lock. The defect this class was written against was **in this module**.

    ``linked_issue`` returned ``None`` and ``copied_comment_ids`` returned an empty set when the
    database could not be read at all — the same collapse this flow has been naming since
    the label search, one level down and inside its own house. ``None`` means "no row"; an empty set means
    "nothing copied yet". Neither of them means "I could not look", and a caller that acts on
    the difference opens a duplicate issue or copies a comment twice, permanently.

    Measured on 11 September 2026: DuckDB takes an exclusive lock on the file, so a second
    process is refused outright. With the old behaviour a task whose row was sitting in the
    table came back as ``linked_issue -> None`` and ``copied_comment_ids -> set()``.
    """

    @pytest.fixture()
    def unreadable(self, tmp_path):
        """A path that cannot be opened: its parent is a file, so even mkdir fails."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x")
        return blocker / "inside.duckdb"

    def test_linked_issue_raises_instead_of_saying_nothing_is_remembered(self, unreadable):
        with pytest.raises(links.LinkStoreError):
            links.linked_issue(TASK, db_path=unreadable)

    def test_copied_comment_ids_raises_instead_of_returning_an_empty_set(self, unreadable):
        with pytest.raises(links.LinkStoreError):
            links.copied_comment_ids(TASK, db_path=unreadable)

    def test_comments_not_yet_copied_does_not_swallow_it_either(self, unreadable):
        """The one a caller actually calls. If this flattened the failure, the noise made
        upstairs would be lost on the last step."""
        with pytest.raises(links.LinkStoreError):
            links.comments_not_yet_copied(TASK, [{"id": "C1"}], db_path=unreadable)

    def test_no_row_still_means_none_and_not_an_error(self, db):
        """The distinction is the whole point: absence stays absence."""
        assert links.linked_issue(TASK, db_path=db) is None
        assert links.copied_comment_ids(TASK, db_path=db) == set()

    def test_the_error_says_which_task_and_carries_the_cause(self, unreadable):
        with pytest.raises(links.LinkStoreError) as exc:
            links.linked_issue(TASK, db_path=unreadable)
        assert "register" in str(exc.value).lower()
        assert exc.value.__cause__ is not None

    def test_writes_still_report_rather_than_raise(self, unreadable):
        """Unchanged, and deliberately different from the reads: a write that fails has already
        not happened, and the caller's next move is to report it, not to guess. A read that
        fails is an answer the caller is about to believe."""
        assert links.remember_link(TASK, "PROJ-1", db_path=unreadable)["ok"] is False
        assert links.remember_comment(TASK, "C1", db_path=unreadable)["ok"] is False

    def test_a_store_held_by_another_process_is_unreadable_not_empty(self, db):
        """The measured case, with a real second process holding the file.

        This is what happens when two runs overlap. The row is there;
        DuckDB refuses the second connection; and before the per-task lock that arrived as "no link".
        """
        import subprocess
        import sys

        links.remember_link(TASK, "PROJ-900", db_path=db)
        links.remember_comment(TASK, "C1", db_path=db)
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == "PROJ-900"

        holder = subprocess.Popen(
            [sys.executable, "-c",
             # ``held =`` matters: without a reference the connection is collected at once
             # and the lock goes with it, which makes the holder hold nothing.
             f"import duckdb,sys;held=duckdb.connect({str(db)!r});print('ready',flush=True);"
             "sys.stdin.readline();held.close()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "ready"
            with pytest.raises(links.LinkStoreError):
                links.linked_issue(TASK, db_path=db)
            with pytest.raises(links.LinkStoreError):
                links.copied_comment_ids(TASK, db_path=db)
        finally:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=10)

        # And it reads normally again once the other process lets go.
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == "PROJ-900"
        assert links.copied_comment_ids(TASK, db_path=db) == {"C1"}
