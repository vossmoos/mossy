"""The reading layer must declare it ran, and the writer must close every ticket.

Both come from the end-to-end run through the duty, where nothing failed and nothing worked:
the reader was skipped on every ticket and its empty placeholders travelled onward, so the run
could never link by name; and five decisions never reached the writer, so five tickets were
never tagged and came back on every sweep.
"""

from __future__ import annotations

import pytest

from _config import all_processed_tags, outcome_tags, processed_tag
from verify_signals import ReadingLayerMissing, verify

REGIONS = {"own_text": "Guten Tag\n\nAnna Muster\nMuster AG", "signature": "Anna Muster\nMuster AG",
           "quoted": ""}


class TestTheReaderMustDeclareItLooked:
    def test_a_payload_the_reader_never_touched_is_refused(self):
        with pytest.raises(ReadingLayerMissing):
            verify({"signals": {"emailDomain": "muster.example.ch", "companyNameCandidates": []}},
                   REGIONS)

    def test_the_extractors_own_output_is_refused_unchanged(self):
        """It emits the flag as false, because it reads no prose."""
        from extract_company_signals import extract
        payload = extract({"id": 1, "description": "<div>Hallo<br>Muster AG</div>",
                           "description_text": "Hallo Muster AG"},
                          {"id": 1, "email": "anna@muster.example.ch"})
        assert payload["signals"]["readSignalsAttempted"] is False
        with pytest.raises(ReadingLayerMissing):
            verify(payload, payload["regions"])

    def test_looking_and_finding_nothing_is_accepted(self):
        """A ticket with no signature is legitimate and must reach no_signals honestly."""
        result = verify({"signals": {"readSignalsAttempted": True, "emailDomain": "muster.example.ch",
                                     "companyNameCandidates": [],
                                     "reason": "Keine Signatur im Ticket."}}, REGIONS)
        assert result["ok"] is True
        assert result["verified"]["companyNameCandidates"] == []

    def test_the_two_cases_are_distinguishable(self):
        """Closing one silence by opening another would be no improvement."""
        looked = verify({"signals": {"readSignalsAttempted": True,
                                     "companyNameCandidates": []}}, REGIONS)
        assert looked["verified"]["companyNameCandidates"] == []
        with pytest.raises(ReadingLayerMissing):
            verify({"signals": {"companyNameCandidates": []}}, REGIONS)

    def test_the_flag_is_not_verified_against_the_text(self):
        """It is a declaration about the process, not a claim about the ticket."""
        result = verify({"signals": {"readSignalsAttempted": True}}, REGIONS)
        assert result["discarded"] == []
        assert result["verified"]["readSignalsAttempted"] is True

    @pytest.mark.parametrize("value", [False, None, "true", 1, "yes"])
    def test_only_a_real_true_counts(self, value):
        """A truthy near-miss must not pass for a declaration."""
        with pytest.raises(ReadingLayerMissing):
            verify({"signals": {"readSignalsAttempted": value}}, REGIONS)

    def test_the_refusal_says_what_to_do(self):
        with pytest.raises(ReadingLayerMissing) as exc:
            verify({"signals": {}}, REGIONS)
        message = str(exc.value)
        assert "readSignalsAttempted" in message
        assert "companyNameCandidates" in message
        assert "no signature" in message, "the legitimate empty case must be explained"


class TestTheWriterRefusesUnreadSignals:
    def test_it_will_not_write_on_a_decision_nobody_read(self, account, run_apply):
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(
            "<div>Hallo<br><br>Freundliche Grüsse<br>Anna Muster<br>Muster Sanitär AG</div>",
            contact)
        result, code = run_apply(ticket, {
            "decision": "create", "reason": "Grund.",
            "signals": {"emailDomain": "muster-sanitaer.example.ch",
                        "companyNameCandidates": ["Muster Sanitär AG"]},
            "create": {"name": "Muster Sanitär AG"}, "company_name": "Muster Sanitär AG"})
        assert code != 0 and result["ok"] is False
        assert result["missing_step"] == "reading_layer"
        assert account.writes == [], "it wrote on signals nobody had read"


class TestEveryOutcomeTagCountsAsFinished:
    """The pre-check skipped only the linked tag, so declined tickets were queued forever."""

    def test_all_four_tags_mean_processed(self):
        assert processed_tag() in all_processed_tags()
        for tag in outcome_tags().values():
            assert tag in all_processed_tags()

    def test_the_declining_tags_are_distinct_from_the_linked_one(self):
        assert processed_tag() not in set(outcome_tags().values())


class TestThePreCheckSkipsFinishedTickets:
    """Without this the duty queues a decided ticket on every sweep, forever."""

    @pytest.fixture()
    def planner(self, monkeypatch):
        import plan_company_check
        from fake_freshdesk import FakeFreshdesk

        fake = FakeFreshdesk()

        class _Client:
            @classmethod
            def from_env(cls):
                return fake

        monkeypatch.setattr(plan_company_check, "FreshdeskClient", _Client)
        monkeypatch.setenv("LINKING_ENABLED", "true")
        monkeypatch.setenv("LINKING_GROUP_IDS", "1")
        return fake, plan_company_check.plan

    @pytest.mark.parametrize("tag", ["mossy-company-linked", "mossy-company-no-signals",
                                     "mossy-company-no-match", "mossy-company-ambiguous"])
    def test_every_outcome_tag_stops_a_ticket_being_queued_again(self, planner, tag):
        fake, plan = planner
        contact = fake.add_contact("anna@muster.example.ch")
        ticket = fake.add_ticket("<div>Hallo</div>", contact, tags=[tag])
        result = plan([ticket])
        assert result["planned"] == [], f"{tag} did not stop a second queueing"
        assert result["skipped"][0]["reason"] == "already_processed"
        assert tag in result["skipped"][0]["detail"]

    def test_an_untagged_ticket_is_still_queued(self, planner):
        fake, plan = planner
        contact = fake.add_contact("anna@muster.example.ch")
        ticket = fake.add_ticket("<div>Hallo</div>", contact)
        assert [p["ticket_id"] for p in plan([ticket])["planned"]] == [ticket]

    def test_an_unrelated_tag_does_not_stop_it(self, planner):
        fake, plan = planner
        contact = fake.add_contact("anna@muster.example.ch")
        ticket = fake.add_ticket("<div>Hallo</div>", contact, tags=["dringend"])
        assert [p["ticket_id"] for p in plan([ticket])["planned"]] == [ticket]

    def test_a_linked_contact_still_costs_no_task(self, planner):
        fake, plan = planner
        company = fake.add_company("Muster AG", ["muster.example.ch"])
        contact = fake.add_contact("anna@muster.example.ch", company_id=company)
        ticket = fake.add_ticket("<div>Hallo</div>", contact)
        assert plan([ticket])["skipped"][0]["reason"] == "already_linked"


class TestTheLinkCheckStatesTheNextStep:
    """A step nobody states is a step somebody skips.

    Measured through the duty: on a ticket whose body named no company, the flow stopped at the
    link check. The agent read the ticket, judged there was nothing to find, and never ran the
    rest — so nothing decided, nothing was tagged, and the ticket came back on every sweep.
    """

    @pytest.fixture()
    def link_check(self, monkeypatch):
        import check_contact_link
        from fake_freshdesk import FakeFreshdesk

        fake = FakeFreshdesk()

        class _Client:
            @classmethod
            def from_env(cls):
                return fake

        monkeypatch.setattr(check_contact_link, "FreshdeskClient", _Client)
        monkeypatch.setenv("LINKING_ENABLED", "true")
        monkeypatch.setenv("LINKING_GROUP_IDS", "1")
        return fake, check_contact_link

    def _payload(self, fake, module, capsys, **kw):
        import json as j
        import sys as s
        contact = fake.add_contact("a@muster.example.ch", company_id=kw.pop("company_id", None))
        ticket = fake.add_ticket("<div>Hallo</div>", contact, **kw)
        s.argv = ["check_contact_link.py", "--ticket-id", str(ticket)]
        module.main()
        return j.loads(capsys.readouterr().out)

    def test_an_unlinked_in_scope_ticket_is_told_to_continue(self, link_check, capsys):
        fake, module = link_check
        payload = self._payload(fake, module, capsys)
        assert payload["stop"] is False
        assert "extract_company_signals.py" in payload["next_step"]

    def test_the_instruction_names_the_writer_as_the_ending(self, link_check, capsys):
        fake, module = link_check
        payload = self._payload(fake, module, capsys)
        assert "apply_link.py" in payload["next_step"]
        assert "end of the flow" in payload["next_step"], (
            "the writer is the ending, not one option among steps"
        )

    def test_it_never_sends_the_agent_through_a_separate_decide_step(self, link_check, capsys):
        """Deciding and writing are one call. Naming link_decision.py here would put back the
        place the flow used to stop at — a decision reached, and nothing written."""
        fake, module = link_check
        payload = self._payload(fake, module, capsys)
        assert "link_decision" not in payload["next_step"]
        assert "apply_link.py --signals" in payload["next_step"]

    def test_a_ticket_with_no_company_in_it_is_still_told_to_continue(self, link_check, capsys):
        """'Nothing to find' is a conclusion for the end of the flow, not a reason to stop."""
        fake, module = link_check
        payload = self._payload(fake, module, capsys)
        assert payload["stop"] is False
        assert "no_signals" in payload["next_step"]

    def test_a_linked_contact_is_told_to_stop(self, link_check, capsys):
        fake, module = link_check
        company = fake.add_company("Muster AG", ["muster.example.ch"])
        payload = self._payload(fake, module, capsys, company_id=company)
        assert payload["stop"] is True
        assert "stop" in payload["next_step"].lower()

    def test_an_out_of_scope_ticket_is_told_to_stop(self, link_check, capsys):
        fake, module = link_check
        payload = self._payload(fake, module, capsys, group_id=999)
        assert payload["stop"] is True


class TestTheDirectorySearchMustBeDeclared:
    """An empty candidate list is only an answer if somebody looked.

    Measured through the duty: the matcher found the contact's company and scored it 1.0, and
    the decision arrived with no candidates because the agent rebuilt the payload by hand. It
    decided to create a company that already existed. The writer's re-derivation refused it —
    the system working, but a poor place to learn that the second step was thrown away.
    """

    def test_a_payload_without_the_declaration_is_refused(self):
        from link_decision import DirectorySearchMissing, decide
        with pytest.raises(DirectorySearchMissing):
            decide({"verified": {"companyNameCandidates": ["Neu AG"]}, "candidates": []})

    def test_searching_and_finding_nothing_is_accepted(self):
        from link_decision import decide
        result = decide({"verified": {"companyNameCandidates": ["Neu AG"]},
                         "candidates": [], "searched": ["directory listing (14 companies)"]})
        assert result["decision"] in ("create", "no_match")

    def test_the_refusal_says_to_pass_the_matchers_output_through(self):
        from link_decision import DirectorySearchMissing, decide
        with pytest.raises(DirectorySearchMissing) as exc:
            decide({"verified": {}, "candidates": []})
        assert "match_company.py" in str(exc.value)

    def test_the_writer_refuses_a_decision_that_skipped_the_search(self, account, run_apply):
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(
            "<div>Hallo<br><br>Freundliche Grüsse<br>Anna Muster<br>Muster Sanitär AG</div>",
            contact)
        import apply_link
        original = apply_link.decide
        try:
            # The writer builds its own payload when re-deriving; strip the declaration to
            # prove the refusal is reached rather than assumed.
            apply_link.decide = lambda payload: original(
                {k: v for k, v in payload.items() if k != "searched"})
            result, code = run_apply(ticket, {
                "decision": "create", "reason": "Grund.",
                "signals": {"readSignalsAttempted": True, "emailDomain": "muster-sanitaer.example.ch",
                            "companyNameCandidates": ["Muster Sanitär AG"]},
                "create": {"name": "Muster Sanitär AG"}, "company_name": "Muster Sanitär AG"})
        finally:
            apply_link.decide = original
        assert code != 0 and result["missing_step"] == "directory_search"
        assert account.writes == []


class TestDecidingAndWritingAreOneStep:
    """There is no deciding without writing, because there is nothing to decide with.

    A separate decide step was a place to stop. An agent that had reached a decision writing
    nothing saw no reason to call the writer, so five tickets in one run were decided, never
    tagged, and came back on every sweep. Three rounds of clearer instructions moved the agent
    three steps further along and never to the end, so the step is gone instead.
    """

    SIGNATURE = ("<div>Guten Tag<br><br>Freundliche Grüsse<br>Anna Muster<br>"
                 "Muster Sanitär AG<br>Musterstrasse 12<br>6000 Luzern</div>")

    def _signals(self, **extra):
        return {"readSignalsAttempted": True, "emailDomain": "muster-sanitaer.example.ch",
                "companyNameCandidates": ["Muster Sanitär AG"], **extra}

    def _run(self, run_apply, ticket, signals):
        return run_apply(ticket, None, signals=signals)

    def test_signals_alone_are_enough_to_link_and_write(self, account, run_apply):
        company = account.add_company("Muster Sanitär AG", ["muster-sanitaer.example.ch"])
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(self.SIGNATURE, contact)
        result, code = self._run(run_apply, ticket, self._signals())
        assert code == 0 and result["linked"] is True
        assert account.contacts[contact]["company_id"] == company

    def test_a_ticket_that_names_no_company_is_still_closed(self, account, run_apply):
        """The case that used to be abandoned: nothing to write, so nothing was called."""
        contact = account.add_contact("privat@freemail.example.ch")
        ticket = account.add_ticket("<div>Meine Dusche tropft.</div>", contact)
        result, code = self._run(run_apply, ticket, {
            "readSignalsAttempted": True, "emailDomain": "freemail.example.ch",
            "emailDomainIsGeneric": True, "companyNameCandidates": []})
        assert code == 0
        assert result["decision"] == "no_signals"
        assert result["tagged"] is True
        assert "mossy-company-no-signals" in account.tickets[ticket]["tags"]

    def test_an_ambiguous_ticket_is_still_closed(self, account, run_apply):
        account.add_company("Feld Haustechnik AG", ["feld-a.example"])
        account.add_company("Feld Haustechnik GmbH", ["feld-b.example"])
        contact = account.add_contact("hans@privat.example.ch")
        ticket = account.add_ticket(
            "<div>Guten Tag<br><br>Freundliche Grüsse<br>Hans<br>Feld Haustechnik AG</div>",
            contact)
        result, code = self._run(run_apply, ticket, {
            "readSignalsAttempted": True, "emailDomain": "privat.example.ch",
            "companyNameCandidates": ["Feld Haustechnik AG"]})
        assert code == 0
        assert result["decision"] == "ambiguous"
        assert "mossy-company-ambiguous" in account.tickets[ticket]["tags"]
        assert account.contacts[contact]["company_id"] is None

    def test_the_trace_still_says_what_was_decided_and_why(self, account, run_apply):
        """Nobody else records the decision now, so this result is the whole trace."""
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(self.SIGNATURE, contact)
        result, _ = self._run(run_apply, ticket, self._signals())
        assert result["decision"] == "create"
        assert result["reason"] and result["reason"][0].isupper(), "the German reason is missing"
        assert result["evidence"], "the evidence behind the decision is missing"
        assert result["threshold"] is not None
        assert "candidates" in result

    def test_a_declined_ticket_carries_its_evidence_too(self, account, run_apply):
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(self.SIGNATURE, contact)
        account.add_company("Muster Sanitär AG", ["a.example"])
        account.add_company("Muster Sanitaer AG", ["b.example"])
        result, _ = self._run(run_apply, ticket, self._signals())
        assert result["decision"] == "ambiguous"
        assert result["evidence"] and result["candidates"]
        assert result["reason"]

    def test_signals_nobody_read_are_still_refused(self, account, run_apply):
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(self.SIGNATURE, contact)
        result, code = self._run(run_apply, ticket, {
            "emailDomain": "muster-sanitaer.example.ch",
            "companyNameCandidates": ["Muster Sanitär AG"]})
        assert code != 0 and result["missing_step"] == "reading_layer"
        assert account.writes == []


class TestEachOutputIsTheNextInput:
    """The chain has to actually connect, or the instructions are a trap.

    Measured through the duty: the flow said to hand the matcher's output to the writer, and
    the matcher's output carried candidates but not the signals they were found from. The
    writer refused it with 'no signals could be read'. The agent had followed the instructions
    exactly, which is the worst way for a flow to fail.
    """

    @pytest.fixture()
    def matcher(self, monkeypatch):
        import match_company
        from fake_freshdesk import FakeFreshdesk

        fake = FakeFreshdesk()

        class _Client:
            @classmethod
            def from_env(cls):
                return fake

        monkeypatch.setattr(match_company, "FreshdeskClient", _Client)
        monkeypatch.setenv("LINKING_ENABLED", "true")
        monkeypatch.setenv("LINKING_GROUP_IDS", "1")
        return fake, match_company

    def _match(self, module, capsys, signals, extra_argv=()):
        import json as j
        import sys as s
        s.argv = ["match_company.py", "--ticket-id", "1", "--signals", j.dumps(signals),
                  *extra_argv]
        module.main()
        return j.loads(capsys.readouterr().out)

    def test_the_matcher_carries_the_signals_forward(self, matcher, capsys):
        _fake, module = matcher
        signals = {"readSignalsAttempted": True, "emailDomain": "muster.example.ch",
                   "companyNameCandidates": ["Muster AG"]}
        out = self._match(module, capsys, signals)
        assert out["signals"] == signals, "the next step cannot run without them"
        assert "searched" in out and "candidates" in out

    def test_the_matcher_output_is_a_valid_writer_payload(self, matcher, capsys):
        """Exactly what the flow tells the agent to do."""
        from _signals import signals_from
        _fake, module = matcher
        out = self._match(module, capsys, {"readSignalsAttempted": True,
                                           "emailDomain": "muster.example.ch",
                                           "companyNameCandidates": ["Muster AG"]})
        assert signals_from(out)["companyNameCandidates"] == ["Muster AG"]

    @pytest.mark.parametrize("flag", ["--regions", "--provenance"])
    def test_the_matcher_takes_the_blocks_a_caller_already_holds(self, matcher, capsys, flag):
        """It needs neither, but exiting 2 on them looked like a crash in the ledger."""
        _fake, module = matcher
        out = self._match(module, capsys,
                          {"readSignalsAttempted": True, "emailDomain": "muster.example.ch"},
                          extra_argv=[flag, '{"own_text": "Guten Tag"}'])
        assert out["ok"] is True

    def test_the_verifier_takes_them_too(self):
        """The same call shape, one step earlier."""
        import json as j
        import subprocess
        import sys
        from pathlib import Path
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        payload = j.dumps({"readSignalsAttempted": True, "emailDomain": "muster.example.ch"})
        regions = j.dumps({"own_text": "Guten Tag", "signature": "", "quoted": ""})
        result = subprocess.run(
            [sys.executable, "verify_signals.py", "--ticket-id", "1", "--signals", payload,
             "--regions", regions, "--provenance", "{}"],
            cwd=scripts, capture_output=True, text=True,
            env={**__import__("os").environ, "LINKING_LEDGER_DIR": "/tmp/mutation-ledger"},
        )
        assert result.returncode == 0, result.stderr[-200:]
        assert j.loads(result.stdout)["ok"] is True


class TestAFailedAttemptIsRetried:
    """The dedupe key has to expire, or one bad turn kills a ticket until a restart.

    Measured through the duty: four tickets each got exactly one attempt, each ended in a
    different place, and 27 sweeps later none had been retried. The key was per ticket and
    never changed, so the runtime remembered it for the life of the process and dropped every
    later request. A key per ticket and window is what lets it expire.
    """

    @pytest.fixture()
    def planner(self, monkeypatch):
        import plan_company_check
        from fake_freshdesk import FakeFreshdesk

        fake = FakeFreshdesk()

        class _Client:
            @classmethod
            def from_env(cls):
                return fake

        monkeypatch.setattr(plan_company_check, "FreshdeskClient", _Client)
        monkeypatch.setenv("LINKING_ENABLED", "true")
        monkeypatch.setenv("LINKING_GROUP_IDS", "1")
        return fake, plan_company_check

    def _ticket(self, fake):
        contact = fake.add_contact("anna@muster.example.ch")
        return fake.add_ticket("<div>Hallo</div>", contact)

    def test_the_key_changes_between_windows(self, planner, monkeypatch):
        fake, module = planner
        ticket = self._ticket(fake)
        monkeypatch.setattr(module, "_window", lambda: 100)
        first = module.plan([ticket])["planned"][0]["dedupe_key"]
        monkeypatch.setattr(module, "_window", lambda: 101)
        second = module.plan([ticket])["planned"][0]["dedupe_key"]
        assert first != second, "a ticket whose attempt died could never be queued again"

    def test_the_key_is_stable_inside_one_window(self, planner, monkeypatch):
        """Within a window it must still collapse repeats — the sweep runs every minute."""
        fake, module = planner
        ticket = self._ticket(fake)
        monkeypatch.setattr(module, "_window", lambda: 100)
        assert module.plan([ticket])["planned"][0]["dedupe_key"] == \
            module.plan([ticket])["planned"][0]["dedupe_key"]

    def test_the_key_still_names_the_ticket(self, planner, monkeypatch):
        fake, module = planner
        ticket = self._ticket(fake)
        monkeypatch.setattr(module, "_window", lambda: 100)
        assert str(ticket) in module.plan([ticket])["planned"][0]["dedupe_key"]

    def test_two_tickets_never_share_a_key(self, planner, monkeypatch):
        fake, module = planner
        a, b = self._ticket(fake), self._ticket(fake)
        monkeypatch.setattr(module, "_window", lambda: 100)
        keys = [p["dedupe_key"] for p in module.plan([a, b])["planned"]]
        assert len(set(keys)) == 2

    def test_the_window_length_is_configurable(self, monkeypatch):
        import plan_company_check
        monkeypatch.setenv("LINKING_RETRY_WINDOW_SECONDS", "60")
        short = plan_company_check._window()
        monkeypatch.setenv("LINKING_RETRY_WINDOW_SECONDS", "3600")
        assert plan_company_check._window() != short

    def test_a_nonsense_window_falls_back_rather_than_crashing(self, monkeypatch):
        import plan_company_check
        monkeypatch.setenv("LINKING_RETRY_WINDOW_SECONDS", "not-a-number")
        assert isinstance(plan_company_check._window(), int)

    def test_the_durable_marker_is_still_the_tag_not_the_key(self, planner, monkeypatch):
        """The window only expires memory. What stops a finished ticket coming back is the tag."""
        fake, module = planner
        contact = fake.add_contact("anna@muster.example.ch")
        ticket = fake.add_ticket("<div>Hallo</div>", contact, tags=["mossy-company-no-match"])
        monkeypatch.setattr(module, "_window", lambda: 999)
        assert module.plan([ticket])["planned"] == []
