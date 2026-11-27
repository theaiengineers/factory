import contextlib
import io
import json
import os
import random
import unittest
import urllib.error
from unittest import mock

from factory import config, events, review_gate, risk

A, B, C = "a" * 40, "b" * 40, "c" * 40
BOT = "github-actions[bot]"
RECORDED = "2026-02-01T00:00:00Z"
RULES = """
spec: factory/0.1
factory: example
building: example/shop
branch: main
intake:
  - station: testing
    uses: checks
    require: [tests, lint]
  - station: qa
    uses: independent-review
    standard: docs/REVIEW_STANDARD.md
    skip_when_only: ["**/*.md"]
    automatic:
      account: github-actions[bot]
risk:
  - area: payments
    why: moves money
    paths: [api/billing/**]
"""


def commit(sha, date):
    return {"sha": sha, "commit": {"committer": {"date": date}}}


def run(name, started, completed=None, conclusion="success", suite=1, ident=1):
    return {"id": ident, "name": name, "started_at": started, "completed_at": completed, "check_suite": {"id": suite},
            "status": "completed" if completed else "in_progress", "conclusion": conclusion if completed else None,
            "html_url": f"https://github.test/runs/{ident}"}


def status(when, state, description, context="independent-review", ident=1, creator=BOT):
    return {"id": ident, "created_at": when, "state": state, "description": description, "context": context,
            "target_url": "https://github.test/gate-run", "creator": {"login": creator, "type": "Bot" if creator.endswith("[bot]") else "User"}}


def record(sha, verdict, when, login="alice", kind="User", ident=1):
    return {"id": ident, "created_at": when, "user": {"login": login, "type": kind}, "html_url": f"https://github.test/c/{ident}",
            "body": f"<!-- independent-review sha={sha} verdict={verdict} -->\nwords"}


def pull(number=12, state="open", merged_at=None, closed_at=None, head=A, base="main", title="Add a thing", updated="2026-01-15T13:00:00Z"):
    return {"number": number, "state": state, "created_at": "2026-01-15T12:05:07Z", "updated_at": updated, "merged_at": merged_at,
            "closed_at": closed_at, "title": title, "html_url": f"https://github.test/pull/{number}",
            "user": {"login": "alice", "type": "User"}, "merged_by": {"login": "bob", "type": "User"} if merged_at else None,
            "merge_commit_sha": "f" * 40 if merged_at else None, "head": {"sha": head}, "base": {"ref": base}}


class FakeGitHub:
    repo = "example/shop"

    def __init__(self, prs=(), commits=None, runs=None, statuses=None, comments=None):
        self.prs = {p["number"]: p for p in prs}
        self.commits, self.runs, self.statuses, self.comments = commits or {}, runs or {}, statuses or {}, comments or {}
        self.asked = []

    def get(self, path):
        self.asked.append(path)
        if path.startswith("pulls?"):
            page = int(path.rsplit("page=", 1)[1])
            ordered = sorted(self.prs.values(), key=lambda p: p["updated_at"], reverse=True)
            return [{k: v for k, v in p.items() if k != "merged_by"} for p in ordered[(page - 1) * 100:page * 100]]
        if path.startswith("pulls/"):
            return self.prs[int(path.split("/")[1])]
        if "/check-runs" in path:
            return {"check_runs": self.runs.get(path.split("/")[1], [])}
        raise AssertionError(path)

    def pages(self, path, limit):
        self.asked.append(path)
        if path.endswith("/commits"):
            return self.commits.get(int(path.split("/")[1]), [commit(A, "2026-01-15T12:00:00Z")])
        if path.endswith("/statuses"):
            return self.statuses.get(path.split("/")[1], [])
        if path.endswith("/comments"):
            return self.comments.get(int(path.split("/")[1]), [])
        raise AssertionError(path)


def export(gh, rules=RULES, **kw):
    return events.export(gh, config.parse(rules), recorded=RECORDED, **kw)


def told(found):
    return [(e["type"], e.get("station"), e.get("outcome"), e["change"].get("commit", "")[:1]) for e in found]


class Words(unittest.TestCase):
    def test_every_description_the_gate_can_give_is_understood(self):
        # Driven through the gate's own decide(), so a change to its wording fails here.
        code = {"review": risk.CODE, "areas": [], "reasons": [], "code": ["a.py"], "files": 1}
        high = {**code, "review": risk.HIGH, "areas": ["payments"]}
        none = {**code, "review": risk.NONE}

        def by(login, verdict, sha=A, kind="User", association="OWNER", when="2026-01-15T12:30:00Z"):
            return {"user": {"login": login, "type": kind}, "author_association": association, "created_at": when,
                    "body": f"<!-- independent-review sha={sha} verdict={verdict} -->"}
        auto = {**by(BOT, "pass", kind="Bot"), "author_association": review_gate.AUTOMATIC}
        cases = ((none, [], None, events.SKIPPED), (code, [], None, events.WAITING), (code, [by("al", "pass")], None, events.PASSED),
                 (high, [by("al", "findings")], None, events.SENT_BACK), (code, [by("al", "pass", sha=B)], None, events.WAITING),
                 (code, [by("al", "pass", when="2026-01-01T00:00:00Z")], "2026-01-15T12:00:00Z", events.WAITING),
                 (code, [{**by("al", "pass"), "body": "<!-- independent-review sha=zz -->"}], None, events.WAITING),
                 (high, [auto], None, events.PERSON))
        seen = set()
        for tier, comments, committed, kind in cases:
            state, description = review_gate.decide(tier, A, comments, committed, automatic={"account": BOT, "passes": ["code"]})
            self.assertEqual(events.decision(state, description)[0], kind, description)
            seen.add(description.split(A[:7])[0][:20])
        self.assertGreaterEqual(len(seen), 7)
        self.assertEqual(events.decision("error", "Gate could not decide: x")[0], events.ERROR)

    def test_words_that_are_not_the_gates_are_not_trusted(self):
        for state, description in (("success", "LGTM"), ("success", None), ("failure", "Review record for abc says pass"),
                                   ("success", "No independent review recorded for abc"), ("pending", "Review of x has open findings")):
            self.assertEqual(events.decision(state, description)[0], events.UNKNOWN, (state, description))


class OneChange(unittest.TestCase):
    def test_a_change_from_start_to_merge(self):
        gh = FakeGitHub(
            [pull(state="closed", merged_at="2026-01-15T12:41:49Z", closed_at="2026-01-15T12:41:49Z", head=B)],
            commits={12: [commit(A, "2026-01-15T12:00:00Z"), commit(B, "2026-01-15T12:24:53Z")]},
            runs={A: [run("tests", "2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z"), run("lint", "2026-01-15T12:05:12Z", "2026-01-15T12:05:40Z", ident=2)],
                  B: [run("tests", "2026-01-15T12:25:00Z", "2026-01-15T12:26:00Z", ident=3), run("lint", "2026-01-15T12:25:01Z", "2026-01-15T12:25:30Z", ident=4)]},
            statuses={A: [status("2026-01-15T12:05:20Z", "failure", f"No independent review recorded for {A[:7]} (code change, not high risk)"),
                          status("2026-01-15T12:10:00Z", "failure", f"No independent review recorded for {A[:7]} (code change, not high risk)", ident=2),
                          status("2026-01-15T12:16:00Z", "failure", f"Review of {A[:7]} has open findings: fix or answer them, then review again", ident=3)],
                      B: [status("2026-01-15T12:25:05Z", "failure", f"Review is for {A[:7]}, head is {B[:7]}: review again", ident=4),
                          status("2026-01-15T12:41:13Z", "success", f"Review record for {B[:7]} says pass (a process control, not proof)", ident=5)]},
            comments={12: [record(A, "findings", "2026-01-15T12:15:50Z", ident=7), record(B, "pass", "2026-01-15T12:41:00Z", login=BOT, kind="Bot", ident=8),
                           {"id": 9, "created_at": "2026-01-15T12:20:00Z", "user": {"login": "alice"}, "body": "thanks"}]})
        found = export(gh, numbers=[12])
        self.assertEqual(told(found), [
            ("change.opened", None, None, ""), ("station.entered", "testing", None, "a"), ("station.entered", "qa", None, "a"),
            ("station.finished", "testing", "pass", "a"), ("station.finished", "qa", "sent_back", "a"),
            ("change.updated", None, None, "b"), ("station.entered", "testing", None, "b"), ("station.entered", "qa", None, "b"),
            ("station.finished", "testing", "pass", "b"), ("station.finished", "qa", "pass", "b"), ("change.merged", None, None, "b")])
        self.assertEqual(events.check_lines(json.dumps(e) for e in found), (11, []))
        sent_back, passed, merged = found[4], found[9], found[10]
        self.assertEqual((sent_back["actor"], sent_back["evidence"], sent_back["reason"]["code"]),
                         ({"kind": "person", "id": "alice"}, "https://github.test/c/7", "review.findings"))
        self.assertEqual((passed["actor"], passed["evidence"]), ({"kind": "agent", "id": BOT}, "https://github.test/c/8"))
        self.assertEqual((merged["actor"]["id"], merged["data"]["factory"]["merge_commit"]), ("bob", "f" * 40))
        first = found[0]
        self.assertEqual((first["spec"], first["factory"], first["building"], first["line"], first["recorded"], first["time"]),
                         ("factory.event/0.1", "example", "example/shop", None, RECORDED, "2026-01-15T12:05:07Z"))
        self.assertNotIn("data", first)

    def test_exporting_twice_gives_the_same_ids(self):
        def gh():
            return FakeGitHub([pull()], statuses={A: [status("2026-01-15T12:06:00Z", "success", "No code changed: no independent review needed")]})
        one, two = export(gh(), numbers=[12]), events.export(gh(), config.parse(RULES), [12], recorded="2027-01-01T00:00:00Z")
        self.assertEqual([e["id"] for e in one], [e["id"] for e in two])
        self.assertEqual(len({e["id"] for e in one}), len(one))
        self.assertEqual(events.check_lines(json.dumps(e) for e in one + two), (len(one) * 2, []))

    def test_a_change_closed_without_merging(self):
        found = export(FakeGitHub([pull(state="closed", closed_at="2026-01-16T09:00:00Z")]), numbers=[12])
        self.assertEqual(told(found)[-1], ("change.closed", None, None, "a"))
        self.assertNotIn("change.merged", [e["type"] for e in found])

    def test_prose_only_is_skipped_at_review(self):
        gh = FakeGitHub([pull()], statuses={A: [status("2026-01-15T12:06:00Z", "success", "No code changed: no independent review needed")]})
        skipped = [e for e in export(gh, numbers=[12]) if e.get("station") == "qa"]
        self.assertEqual([(e["type"], e["outcome"], e["reason"]["code"]) for e in skipped], [("station.finished", "skipped", "review.not_needed")])

    def test_a_person_needed_after_an_automatic_pass_is_an_action_and_is_cleared(self):
        gh = FakeGitHub([pull()], statuses={A: [
            status("2026-01-15T12:06:00Z", "failure", f"Automatic review passed {A[:7]}; this change also needs a person's review (high risk: payments)"),
            status("2026-01-15T12:07:00Z", "failure", f"Automatic review passed {A[:7]}; this change also needs a person's review (high risk: payments)", ident=2),
            status("2026-01-15T12:30:00Z", "success", f"Review record for {A[:7]} says pass (a process control, not proof) (high risk: payments)", ident=3)]},
            comments={12: [record(A, "pass", "2026-01-15T12:05:50Z", login=BOT, kind="Bot", ident=1), record(A, "pass", "2026-01-15T12:29:00Z", ident=2)]})
        qa = [e for e in export(gh, numbers=[12]) if e.get("station") == "qa"]
        self.assertEqual([e["type"] for e in qa], ["action.needed", "action.cleared", "station.finished"])
        self.assertEqual((qa[0]["reason"]["code"], qa[0]["assignees"], qa[2]["actor"]["id"]), ("review.person_needed", [], "alice"))
        self.assertEqual(events.check_lines(json.dumps(e) for e in qa), (3, []))

    def test_a_status_in_words_the_gate_does_not_use_is_recorded_and_marked(self):
        gh = FakeGitHub([pull()], statuses={A: [status("2026-01-15T12:06:00Z", "success", "Approved by hand", creator="mallory")]})
        forced, = [e for e in export(gh, numbers=[12]) if e.get("station") == "qa"]
        self.assertEqual((forced["type"], forced["outcome"], forced["actor"], forced["reason"]["code"], forced["data"]),
                         ("station.finished", "pass", {"kind": "person", "id": "mallory"}, "review.status_unrecognised",
                          {"factory": {"unrecognised_status": True}}))

    def test_two_stations_deciding_in_the_same_second_are_both_kept(self):
        at = "2026-01-15T12:06:00Z"
        gh = FakeGitHub([pull()], runs={A: [run("tests", at, at), run("lint", at, at, ident=2)]},
                        statuses={A: [status(at, "failure", f"No independent review recorded for {A[:7]}"),
                                      status(at, "failure", f"Review of {A[:7]} has open findings: fix", ident=2),
                                      status(at, "success", f"Review record for {A[:7]} says pass", ident=3)]})
        found = [e for e in export(gh, numbers=[12]) if e["time"] == at]
        self.assertEqual(sorted((e["type"], e["station"], e.get("outcome")) for e in found),
                         sorted([("station.entered", "testing", None), ("station.finished", "testing", "pass"), ("station.entered", "qa", None),
                                 ("station.finished", "qa", "sent_back"), ("station.finished", "qa", "pass")]))
        self.assertEqual(len({e["id"] for e in found}), 5)
        self.assertEqual([e.get("outcome") for e in found if e["station"] == "qa" and e["type"] == "station.finished"], ["sent_back", "pass"])

    def test_one_second_keeps_the_order_the_gate_set_its_statuses_in(self):
        # A pass, then (the record deleted) a wait, in one second: the record must not end on the pass.
        at = "2026-01-15T12:06:00Z"
        gh = FakeGitHub([pull()], statuses={A: [status(at, "failure", f"No independent review recorded for {A[:7]}", ident=3),
                                                status(at, "success", f"Review record for {A[:7]} says pass", ident=2),
                                                status(at, "failure", f"Review is for {B[:7]}, head is {A[:7]}: review again", ident=1)]})
        qa = [e for e in export(gh, numbers=[12]) if e.get("station") == "qa"]
        self.assertEqual([(e["type"], e.get("outcome")) for e in qa],
                         [("station.entered", None), ("station.finished", "pass"), ("station.entered", None)])

    def test_one_second_loses_nothing(self):
        at = "2026-01-15T12:06:00Z"
        person = f"Automatic review passed {A[:7]}; this change also needs a person's review"
        gh = FakeGitHub([pull()], statuses={A: [
            status(at, "failure", f"No independent review recorded for {A[:7]}", ident=1), status(at, "failure", "Blocked by hand", ident=2),
            status(at, "success", f"Review record for {A[:7]} says pass", ident=3), status(at, "success", "Approved by hand", ident=4),
            status(at, "failure", person, ident=5), status(at, "failure", f"No independent review recorded for {A[:7]}", ident=6),
            status(at, "failure", person, ident=7), status(at, "success", f"Review record for {A[:7]} says pass", ident=8)]})
        qa = [e for e in export(gh, numbers=[12]) if e.get("station") == "qa"]
        self.assertEqual([(e["type"], e.get("outcome"), (e.get("data") or {}).get("factory", {}).get("unrecognised_status", False)) for e in qa], [
            ("station.entered", None, False), ("station.entered", None, True), ("station.finished", "pass", False),
            ("station.finished", "pass", True), ("action.needed", None, False), ("action.cleared", None, False),
            ("station.entered", None, False), ("action.needed", None, False), ("action.cleared", None, False), ("station.finished", "pass", False)])
        self.assertEqual(len({e["id"] for e in qa}), 10)
        self.assertEqual(events.check_lines(json.dumps(e) for e in qa), (10, []))

    def test_two_pull_requests_sharing_a_commit_each_have_its_events(self):
        both = {A: [status("2026-01-15T12:06:00Z", "success", "No code changed: no independent review needed")]}
        found = export(FakeGitHub([pull(12), pull(13)], statuses=both, runs={A: [run("tests", "2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z"),
                                                                              run("lint", "2026-01-15T12:05:12Z", "2026-01-15T12:05:40Z", ident=2)]}))
        per = {n: [(e["type"], e.get("station")) for e in found if e["change"]["number"] == n] for n in (12, 13)}
        self.assertEqual(per[12], per[13])
        self.assertEqual(len(per[12]), 4)
        self.assertEqual(len({e["id"] for e in found}), len(found))

    def test_who_and_the_link_are_read_as_they_are_now_and_the_rest_is_fixed(self):
        said = {A: [status("2026-01-15T12:30:00Z", "failure", f"Review of {A[:7]} has open findings: fix")]}
        then = export(FakeGitHub([pull()], statuses=said, comments={12: [record(A, "findings", "2026-01-15T12:29:00Z")]}), numbers=[12])
        now = export(FakeGitHub([pull()], statuses=said), numbers=[12])                  # the record has since been deleted
        one, two = then[-1], now[-1]
        self.assertEqual((one["actor"]["id"], two["actor"]["id"]), ("alice", "review gate"))
        fixed = lambda e: {k: v for k, v in e.items() if k not in ("actor", "evidence")}      # noqa: E731
        self.assertEqual(fixed(one), fixed(two))

    def test_unknown_words_that_fail_and_then_succeed_are_two_events(self):
        gh = FakeGitHub([pull()], statuses={A: [status("2026-01-15T12:06:00Z", "failure", "Hold on"), status("2026-01-15T12:06:30Z", "pending", "Still", ident=2),
                                                status("2026-01-15T12:07:00Z", "success", "Go", ident=3), status("2026-01-15T12:08:00Z", "success", "Go again", ident=4)]})
        qa = [e for e in export(gh, numbers=[12]) if e.get("station") == "qa"]
        self.assertEqual([(e["type"], e.get("outcome")) for e in qa],
                         [("station.entered", None), ("station.entered", None), ("station.finished", "pass")])
        self.assertTrue(all(e["data"]["factory"]["unrecognised_status"] for e in qa))

    def test_an_error_and_another_context(self):
        gh = FakeGitHub([pull()], statuses={A: [status("2026-01-15T12:06:00Z", "error", "Gate could not decide: boom"),
                                                status("2026-01-15T12:07:00Z", "success", "Review record for x says pass", context="other", ident=2)]})
        qa = [e for e in export(gh, numbers=[12]) if e.get("station") == "qa"]
        self.assertEqual([(e["type"], e["outcome"]) for e in qa], [("station.finished", "error")])
        self.assertNotIn("boom", json.dumps(qa))                                 # the reason is in fixed words
        other = [e for e in export(gh, numbers=[12], context="other") if e.get("station") == "qa"]
        self.assertEqual([(e["type"], e["outcome"]) for e in other], [("station.finished", "pass")])

    def test_the_reviewer_named_is_from_a_record_made_before_the_decision(self):
        gh = FakeGitHub([pull()], statuses={A: [status("2026-01-15T12:30:00Z", "success", f"Review record for {A[:7]} says pass")]},
                        comments={12: [record(A, "pass", "2026-01-15T12:45:00Z", login="late", ident=1), record(B, "pass", "2026-01-15T12:20:00Z", login="other", ident=2),
                                       record(A, "findings", "2026-01-15T12:21:00Z", login="finder", ident=3)]})
        passed, = [e for e in export(gh, numbers=[12]) if e.get("outcome") == "pass"]
        self.assertEqual((passed["actor"], passed["evidence"]), ({"kind": "system", "id": "review gate"}, "https://github.test/gate-run"))


class Testing(unittest.TestCase):
    def station(self, runs, rules=RULES):
        found = export(FakeGitHub([pull()], runs={A: runs}), rules=rules, numbers=[12])
        return [e for e in found if e.get("station") == "testing"]

    def test_a_failed_check_fails_the_station_and_is_named(self):
        entered, finished = self.station([run("tests", "2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z", conclusion="failure"),
                                          run("lint", "2026-01-15T12:05:12Z", "2026-01-15T12:05:40Z", ident=2)])
        self.assertEqual((entered["time"], finished["time"], finished["outcome"], finished["reason"]["code"], finished["data"]["factory"]["failed"]),
                         ("2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z", "failed", "checks.failed", ["tests"]))

    def test_not_finished_while_a_check_runs_or_has_not_reported(self):
        self.assertEqual([e["type"] for e in self.station([run("tests", "2026-01-15T12:05:10Z"), run("lint", "2026-01-15T12:05:12Z", "2026-01-15T12:05:40Z", ident=2)])],
                         ["station.entered"])
        self.assertEqual([e["type"] for e in self.station([run("tests", "2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z")])], ["station.entered"])
        self.assertEqual(self.station([run("unrelated", "2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z")]), [])

    FIRST = [run("tests", "2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z", conclusion="failure"),
             run("lint", "2026-01-15T12:05:12Z", "2026-01-15T12:05:40Z", ident=2)]

    def test_a_rerun_is_a_second_entry_and_finish_and_the_first_stays(self):
        before = self.station(self.FIRST)
        after = self.station(self.FIRST + [run("tests", "2026-01-15T12:10:00Z", "2026-01-15T12:11:00Z", ident=5)])
        self.assertEqual([(e["type"], e.get("outcome"), e["time"]) for e in after],
                         [("station.entered", None, "2026-01-15T12:05:10Z"), ("station.finished", "failed", "2026-01-15T12:06:00Z"),
                          ("station.entered", None, "2026-01-15T12:10:00Z"), ("station.finished", "pass", "2026-01-15T12:11:00Z")])
        self.assertEqual(after[:2], before)                                 # the earlier export's events, unchanged
        running = self.station(self.FIRST + [run("tests", "2026-01-15T12:10:00Z", ident=5)])
        self.assertEqual([e["type"] for e in running], ["station.entered", "station.finished", "station.entered"])

    def test_a_failure_and_the_rerun_that_starts_in_the_same_second(self):
        found = self.station([run("tests", "2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z", conclusion="failure"),
                              run("lint", "2026-01-15T12:05:12Z", "2026-01-15T12:05:40Z", ident=2),
                              run("tests", "2026-01-15T12:06:00Z", "2026-01-15T12:07:00Z", ident=5)])
        self.assertEqual([(e["type"], e.get("outcome")) for e in found],
                         [("station.entered", None), ("station.finished", "failed"), ("station.entered", None), ("station.finished", "pass")])

    def test_the_newest_run_of_a_name_stands_whichever_suite_it_is_in(self):
        # Closed and reopened: the same name runs again in a new suite. The old failure does not stand for ever.
        found = self.station(self.FIRST + [run("tests", "2026-01-15T12:10:00Z", "2026-01-15T12:11:00Z", suite=2, ident=5),
                                           run("lint", "2026-01-15T12:10:01Z", "2026-01-15T12:10:30Z", suite=2, ident=6)])
        self.assertEqual([(e["type"], e.get("outcome")) for e in found],
                         [("station.entered", None), ("station.finished", "failed"), ("station.entered", None), ("station.finished", "pass")])
        self.assertEqual(found[-1]["evidence"], "https://github.test/runs/5")

    def test_a_run_that_starts_and_finishes_in_one_second_and_one_dated_backwards(self):
        at = "2026-01-15T12:05:10Z"
        self.assertEqual([(e["type"], e.get("outcome")) for e in self.station([run("tests", at, at), run("lint", at, at, ident=2)])],
                         [("station.entered", None), ("station.finished", "pass")])
        back = self.station([run("tests", at, "2026-01-15T12:00:00Z"), run("lint", at, at, ident=2)])
        self.assertEqual([(e["type"], e["time"]) for e in back], [("station.entered", at), ("station.finished", at)])

    def test_an_earlier_export_is_always_inside_a_later_one(self):
        # Generated histories of check runs, each exported at every second along the way.
        rng = random.Random(7)
        second = lambda n: f"2026-01-15T12:{n // 60:02d}:{n % 60:02d}Z"      # noqa: E731
        for _ in range(300):
            runs = []
            for ident in range(1, rng.randint(2, 8)):
                start = rng.randint(0, 12)
                end = start + rng.randint(0, 4) if rng.random() < 0.85 else None
                runs.append(run(rng.choice(["tests", "lint"]), second(start), second(end) if end is not None else None,
                                conclusion=rng.choice(["success", "failure", "cancelled"]), suite=rng.randint(1, 2), ident=ident))
            earlier = {}
            for moment in range(0, 18):
                seen = [({**r, "completed_at": None, "status": "in_progress", "conclusion": None}
                         if r["completed_at"] and r["completed_at"] > second(moment) else r)
                        for r in runs if r["started_at"] <= second(moment)]
                found = self.station(seen)
                now = {e["id"]: e for e in found}
                self.assertEqual(len(now), len(found))
                for ident, event in earlier.items():
                    self.assertEqual(now.get(ident), event, (runs, moment))
                kinds = [e["type"] for e in found]
                self.assertEqual(kinds, (["station.entered", "station.finished"] * len(kinds))[:len(kinds)])
                earlier = now

    def test_earlier_attempts_are_asked_for(self):
        gh = FakeGitHub([pull()], runs={A: self.FIRST})
        export(gh, numbers=[12])
        self.assertTrue([p for p in gh.asked if "/check-runs?filter=all" in p])

    def test_a_cancelled_check_is_not_a_pass(self):
        _, finished = self.station([run("tests", "2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z", conclusion="cancelled"),
                                    run("lint", "2026-01-15T12:05:12Z", "2026-01-15T12:05:40Z", ident=2)])
        self.assertEqual((finished["outcome"], finished["reason"]["text"]), ("failed", "A required check did not pass"))

    def test_no_required_checks_no_testing_events(self):
        rules = RULES.replace("    require: [tests, lint]\n", "")
        self.assertEqual(self.station([run("tests", "2026-01-15T12:05:10Z", "2026-01-15T12:06:00Z")], rules=rules), [])


class Listing(unittest.TestCase):
    def test_the_most_recently_changed_into_the_factorys_branch(self):
        prs = [pull(1, updated="2026-01-10T00:00:00Z"), pull(2, updated="2026-01-12T00:00:00Z", base="release"),
               pull(3, updated="2026-01-14T00:00:00Z"), pull(4, updated="2026-01-16T00:00:00Z", state="closed", merged_at="2026-01-16T00:00:00Z")]
        numbers = lambda found: sorted({e["change"]["number"] for e in found})     # noqa: E731
        self.assertEqual(numbers(export(FakeGitHub(prs))), [1, 3, 4])
        self.assertEqual(numbers(export(FakeGitHub(prs), limit=2)), [3, 4])
        self.assertEqual(numbers(export(FakeGitHub(prs), since="2026-01-13T00:00:00Z")), [3, 4])
        without_branch = RULES.replace("branch: main\n", "")
        self.assertEqual(numbers(export(FakeGitHub(prs), rules=without_branch)), [1, 2, 3, 4])
        merged, = [e for e in export(FakeGitHub(prs)) if e["type"] == "change.merged"]
        self.assertEqual(merged["actor"]["id"], "bob")                              # read again: the list leaves it out

    def test_no_review_station_no_review_events_and_no_comments_read(self):
        gh = FakeGitHub([pull()], statuses={A: [status("2026-01-15T12:06:00Z", "success", "No code changed")]})
        found = export(gh, rules="spec: factory/0.1\n", numbers=[12])
        self.assertEqual([e["type"] for e in found], ["change.opened"])
        self.assertEqual((found[0]["factory"], found[0]["building"]), ("example", "example/shop"))
        self.assertFalse([p for p in gh.asked if "comments" in p or "statuses" in p])

    def test_nothing_that_can_be_edited_later_is_in_the_record(self):
        # A new title, a new base branch and a force-push all leave the opening event exactly as it was.
        before = export(FakeGitHub([pull(title="First title")]), numbers=[12])
        after = export(FakeGitHub([pull(title="Second title", base="main", head=B)], commits={12: [commit(B, "2026-01-15T11:00:00Z")]}), numbers=[12])
        self.assertEqual([e for e in before if e["type"] == "change.opened"], [e for e in after if e["type"] == "change.opened"])
        self.assertNotIn("title", json.dumps(before))
        self.assertEqual(events.check_lines(json.dumps(e) for e in before + after)[1], [])


GOOD = {"spec": "factory.event/0.1", "id": "x1", "time": "2026-01-15T12:16:00Z", "factory": "example", "building": "example/shop",
        "change": {"number": 12}, "station": "qa", "type": "station.finished", "outcome": "pass"}


class Check(unittest.TestCase):
    def test_a_good_event(self):
        self.assertEqual(events.problems(GOOD), [])
        self.assertEqual(events.problems({**GOOD, "something_new": 1, "data": {"acme.scan": {}}, "time": "2026-01-15T12:16:00.123Z"}), [])
        shipment = {**{k: v for k, v in GOOD.items() if k not in ("change", "station", "outcome")}, "type": "shipment.landed", "line": "api", "shipment": "a-97"}
        self.assertEqual(events.problems(shipment), [])
        # The spec's own example: a station on a line, about a shipment and no one change.
        on_a_line = {**{k: v for k, v in GOOD.items() if k != "change"}, "line": "api", "station": "evaluate"}
        self.assertEqual(events.problems(on_a_line), [])
        self.assertEqual(events.problems({**GOOD, "reason": {"text": "a note"}, "evidence": None, "line": None}), [])
        self.assertEqual(events.problems({**GOOD, "type": "action.needed", "outcome": None, "reason": {"code": "x"}, "assignees": []} | {"outcome": "pass"})[:1],
                         ["`outcome` belongs only on station.finished"])

    def test_what_is_wrong_is_said(self):
        cases = (({**GOOD, "type": "station.exploded"}, "`type`"), ({**GOOD, "outcome": "fine"}, "`outcome`"),
                 ({**GOOD, "time": "2026-01-15 12:16"}, "`time`"), ({**GOOD, "time": "2026-01-15T12:16:00+01:00"}, "`time`"),
                 ({**GOOD, "spec": "other/1"}, "`spec`"), ({**GOOD, "id": ""}, "`id`"), ({**GOOD, "change": {"number": "12"}}, "`change.number`"),
                 ({**GOOD, "change": {"number": True}}, "`change.number`"), ({**GOOD, "station": None}, "`station`"),
                 ({**GOOD, "outcome": "sent_back"}, "`reason.code`"), ({**GOOD, "type": "station.entered"}, "`outcome` belongs"),
                 ({**GOOD, "actor": {"kind": "robot"}}, "`actor.kind`"), ({**GOOD, "data": []}, "`data`"),
                 ({**GOOD, "recorded": "yesterday"}, "`recorded`"), ({**GOOD, "type": "action.needed"}, "`reason.code`"),
                 ({**GOOD, "type": "shipment.landed"}, "`shipment`"), ([GOOD], "JSON object"), ("x", "JSON object"),
                 ({**GOOD, "time": "2026-99-99T99:99:99Z"}, "`time`"), ({**GOOD, "time": "２０２６-01-15T12:16:00Z"}, "`time`"),
                 ({**GOOD, "spec": "factory.event/"}, "`spec`"), ({**GOOD, "change": {"number": -1}}, "`change.number`"),
                 ({**GOOD, "change": {"number": 1, "commit": 7}}, "`change.commit`"),
                 ({**GOOD, "type": "action.needed", "reason": {"code": "x"}}, "`assignees`"),
                 ({k: v for k, v in GOOD.items() if k != "change"}, "`change.number`"),
                 ({**GOOD, "type": "change.sorted"}, "`lines`"), ({**GOOD, "id": "   "}, "`id`"), ({**GOOD, "evidence": 7}, "`evidence`"),
                 ({**GOOD, "line": 7}, "`line`"), ({**GOOD, "reason": "because"}, "`reason`"),
                 ({**GOOD, "type": "action.needed", "reason": {"code": "x"}, "assignees": [7]}, "`assignees`"),
                 ({**GOOD, "type": "shipment.held", "line": "api", "reason": {"code": "x"}}, "`carries`"))
        for event, word in cases:
            self.assertTrue(any(word in p for p in events.problems(event)), (event, events.problems(event)))

    def test_lines(self):
        text = [json.dumps(GOOD), "", "not json", json.dumps({**GOOD, "outcome": "failed"}), json.dumps({**GOOD, "recorded": "2026-01-16T00:00:00Z"})]
        count, said = events.check_lines(text)
        self.assertEqual(count, 4)
        self.assertEqual(len(said), 2)
        self.assertIn("line 3: not JSON", said[0])
        self.assertIn("line 4: the id was used earlier for a different event", said[1])
        self.assertEqual(events.check_lines(["[" * 100_000])[1], ["line 1: not JSON"])


class Main(unittest.TestCase):
    def run_main(self, argv, gh=None, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        env = {"GITHUB_REPOSITORY": "example/shop", "GITHUB_TOKEN": "t"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(events.review_gate, "GitHub", return_value=gh), \
                mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = events.main(argv)
        return code, out.getvalue(), err.getvalue()

    def gh(self, rules=RULES):
        gh = FakeGitHub([pull()])

        def request(method, path, body=None, accept=None):
            if rules is None:
                raise urllib.error.HTTPError("u", 404, "no", {}, io.BytesIO(b""))
            return rules.encode()
        gh._request = request
        return gh

    def test_export_writes_one_json_object_a_line(self):
        code, out, _ = self.run_main(["export", "--pr", "12"], self.gh())
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)["type"] for line in out.splitlines()], ["change.opened"])
        self.assertEqual(self.run_main(["check"], stdin=out)[0], 0)

    def test_a_factory_file_that_is_missing_or_unusable_is_said_plainly(self):
        for rules, word in ((None, "No factory.yml"), ("spec: other\n", "spec")):
            code, out, err = self.run_main(["export"], self.gh(rules))
            self.assertEqual((code, out), (2, ""))
            self.assertIn(word, err)

    def test_plain_failures_are_said_plainly(self):
        gh = self.gh()
        gh.get = mock.Mock(side_effect=urllib.error.HTTPError("u", 404, "no", {}, io.BytesIO(b"")))
        code, out, err = self.run_main(["export", "--pr", "999"], gh)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no such pull request", err)
        gh.get = mock.Mock(side_effect=urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"")))
        code, out, err = self.run_main(["export"], gh)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("HTTP 401", err)
        self.assertEqual(self.run_main(["export", "--since", "2026-99-99T00:00:00Z"], self.gh())[0], 2)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(events.main(["export"]), 2)
        self.assertIn("GITHUB_TOKEN", err.getvalue())

    def test_a_since_that_is_not_a_time_is_refused(self):
        code, _, err = self.run_main(["export", "--since", "last week"], self.gh())
        self.assertEqual(code, 2)
        self.assertIn("--since", err)

    def test_check_fails_when_something_is_wrong(self):
        code, out, _ = self.run_main(["check"], stdin=json.dumps({**GOOD, "outcome": "fine"}) + "\n")
        self.assertEqual(code, 1)
        self.assertIn("1 event(s), 1 problem(s).", out)


if __name__ == "__main__":
    unittest.main()
