import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from factory import reviewer, reviewer_health as health

BOT = "github-actions[bot]"
TODAY = datetime.date(2027, 11, 1)
RULES = """
spec: factory/0.1
intake:
  - station: qa
    uses: independent-review
    automatic:
      account: github-actions[bot]
%s
"""
ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-12345678", "PATH": "/bin"}


def http_error(code):
    return urllib.error.HTTPError("https://api.github.test/x", code, "no", {}, io.BytesIO(b""))


def issue(number, kind, login=BOT, account="Bot", pull=False, body=None):
    out = {"number": number, "user": {"login": login, "type": account},
           "body": body if body is not None else health.MARK % kind + "\nwords"}
    return {**out, "pull_request": {}} if pull else out


class FakeGitHub:
    repo = "Owner/Repo"

    def __init__(self, rules=RULES % "", issues=(), refuse=None):
        self.rules, self.issues, self.refuse = rules, list(issues), refuse
        self.sent, self.asked = [], []

    def pages(self, path, limit):
        self.asked.append(path)
        return self.issues

    def _request(self, method, path, body=None, accept=None):
        if method == "GET":
            self.asked.append(path)
            if self.rules is None:
                raise http_error(404)
            return self.rules.encode()
        if self.refuse:
            raise http_error(self.refuse)
        self.sent.append((method, path, body))
        return json.dumps({"number": 41}).encode()


def answers(*replies):
    """A stand-in for the model call that gives each reply in turn; an exception is raised."""
    left, calls = list(replies), []

    def call(prompt, environ, model, **kw):
        calls.append((prompt, model, kw))
        reply = left.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    return call, calls


class Check(unittest.TestCase):
    def go(self, gh=None, environ=ENV, replies=("OK",)):
        call, calls = answers(*replies)
        pauses = []
        out = health.check(gh or FakeGitHub(), environ, "m", call=call, pause=pauses.append)
        return out, calls, pauses

    def test_working(self):
        gh = FakeGitHub(RULES % "      credential_renewed: 2026-11-27")
        out, calls, pauses = self.go(gh)
        self.assertEqual((out["state"], out["renewed"], pauses), ("working", "2026-11-27", []))
        prompt, model, kw = calls[0]
        self.assertEqual((prompt, model), (health.PING, "m"))
        self.assertEqual((kw["system"], kw["ask"], kw["seconds"]), (health.PING_SYSTEM, health.PING_ASK, health.PING_SECONDS))
        self.assertEqual(gh.asked, ["contents/factory.yml"])        # the default branch: no other is named

    def test_nothing_is_asked_when_no_automatic_reviewer_is_named(self):
        for rules in ("spec: factory/0.1\n", "spec: factory/0.1\nintake:\n  - station: qa\n    uses: independent-review\n", None, "not: [valid"):
            out, calls, _ = self.go(FakeGitHub(rules))
            self.assertEqual((out["state"], calls), ("off", []), rules)

    def test_no_credential_is_missing(self):
        out, calls, _ = self.go(environ={"PATH": "/bin"})
        self.assertEqual((out["state"], calls), ("missing", []))

    def test_a_refused_credential_is_not_tried_again(self):
        out, calls, pauses = self.go(replies=(reviewer.CredentialRefused("refused (status 401)"),))
        self.assertEqual((out["state"], len(calls), pauses), ("refused", 1, []))

    def test_another_failure_is_tried_once_more(self):
        out, calls, pauses = self.go(replies=(reviewer.ReviewerError("boom"), "OK"))
        self.assertEqual((out["state"], len(calls), pauses), ("working", 2, [health.RETRY_SECONDS]))
        out, calls, _ = self.go(replies=(reviewer.ReviewerError("boom"), reviewer.ReviewerError("still")))
        self.assertEqual((out["state"], out["why"], len(calls)), ("failed", "still", 2))
        out, _, _ = self.go(replies=(reviewer.ReviewerError("boom"), reviewer.CredentialRefused("no")))
        self.assertEqual(out["state"], "refused")

    def test_no_words_is_not_an_answer(self):
        out, calls, _ = self.go(replies=("  ", None))
        self.assertEqual((out["state"], len(calls)), ("failed", 2))

    def test_a_fault_reading_the_rules_is_not_hidden(self):
        gh = FakeGitHub()
        gh._request = mock.Mock(side_effect=http_error(500))
        with self.assertRaises(urllib.error.HTTPError):
            self.go(gh)


class Result(unittest.TestCase):
    def test_only_a_known_state_and_a_real_day_are_kept(self):
        self.assertEqual(health.check_result({"state": "working", "renewed": "2026-11-27", "why": "<b>x</b>"}),
                         {"state": "working", "renewed": "2026-11-27"})
        for bad in (None, "", [], {"state": "fine"}, {"renewed": "2026-11-27"}):
            self.assertEqual(health.check_result(bad), {"state": "failed", "renewed": None}, bad)
        for day in ("soon", 20261127, "2026-02-30", ["2026-11-27"]):
            self.assertIsNone(health.check_result({"state": "working", "renewed": day})["renewed"], day)

    def test_the_countdown(self):
        def found(state, renewed, today=TODAY):
            return health.problems({"state": state, "renewed": renewed}, today)
        start = datetime.date(2026, 11, 27)
        last_quiet = start + datetime.timedelta(days=health.RENEW_AFTER_DAYS - 1)
        self.assertEqual(found("working", "2026-11-27", last_quiet), [])
        self.assertEqual(found("working", "2026-11-27", last_quiet + datetime.timedelta(days=1)), ["renewal-due"])
        self.assertEqual(found("working", "2027-12-25"), ["renewal-due"])           # not a past date
        self.assertEqual(found("working", None), [])
        self.assertEqual(found("refused", "2026-11-27"), ["refused", "renewal-due"])
        self.assertEqual(found("missing", None), ["missing"])
        self.assertEqual(found("off", "2020-01-01"), [])
        self.assertLess(health.RENEW_AFTER_DAYS, 365)


class Report(unittest.TestCase):
    def go(self, state, renewed=None, issues=(), **kw):
        gh = FakeGitHub(issues=issues, **kw)
        code, said = health.report(gh, {"state": state, "renewed": renewed}, TODAY)
        return code, said, gh

    def test_working_does_nothing(self):
        code, said, gh = self.go("working")
        self.assertEqual((code, gh.sent), (0, []))
        self.assertIn("works", said[0])

    def test_off_does_not_even_look(self):
        code, _, gh = self.go("off", issues=[issue(3, "refused")])
        self.assertEqual((code, gh.sent, gh.asked), (0, [], []))

    def test_each_problem_opens_one_issue_in_fixed_words(self):
        for kind in ("refused", "missing", "failed"):
            code, said, gh = self.go(kind)
            self.assertEqual(code, 1, kind)
            (method, path, body), = gh.sent
            self.assertEqual((method, path, body["title"]), ("POST", "issues", health.PROBLEMS[kind][0]))
            self.assertEqual(body["body"].splitlines()[0], health.MARK % kind)
            self.assertIn(health.GUIDE, body["body"])
            self.assertIn("#41", said[0])

    def test_a_renewal_that_is_due_is_an_issue_but_not_a_failure(self):
        code, _, gh = self.go("working", "2026-11-27")
        self.assertEqual(code, 0)
        self.assertEqual([b["title"] for _, _, b in gh.sent], [health.PROBLEMS["renewal-due"][0]])

    def test_refused_and_due_are_two_issues(self):
        code, _, gh = self.go("refused", "2026-11-27")
        self.assertEqual((code, [b["body"].splitlines()[0] for _, _, b in gh.sent]),
                         (1, [health.MARK % "refused", health.MARK % "renewal-due"]))

    def test_no_second_issue_while_one_is_open(self):
        code, said, gh = self.go("refused", issues=[issue(9, "refused")])
        self.assertEqual((code, gh.sent), (1, []))
        self.assertIn("#9", said[0])
        self.assertEqual(gh.asked, ["issues?state=open"])

    def test_an_issue_of_another_kind_does_not_stand_in(self):
        _, _, gh = self.go("refused", issues=[issue(9, "failed")])
        self.assertEqual([b["title"] for _, _, b in gh.sent], [health.PROBLEMS["refused"][0]])

    def test_only_the_workflow_accounts_own_issue_counts(self):
        lookalikes = [issue(1, "refused", login="mallory", account="User"), issue(2, "refused", login="other[bot]"),
                      issue(3, "refused", account="User"), issue(4, "refused", pull=True),
                      issue(5, "refused", body="intro\n" + health.MARK % "refused"), issue(6, "refused")]
        lookalikes[-1]["body"] = None
        code, _, gh = self.go("refused", issues=lookalikes)
        self.assertEqual(len(gh.sent), 1)
        # ...and none of them is closed when the credential works.
        _, _, gh = self.go("working", issues=lookalikes)
        self.assertEqual(gh.sent, [])

    def test_its_own_issues_close_when_the_model_answers(self):
        code, said, gh = self.go("working", issues=[issue(9, "refused"), issue(10, "failed"), issue(11, "missing")])
        self.assertEqual(code, 0)
        self.assertEqual([(m, p) for m, p, _ in gh.sent],
                         [("POST", "issues/9/comments"), ("PATCH", "issues/9"), ("POST", "issues/11/comments"),
                          ("PATCH", "issues/11"), ("POST", "issues/10/comments"), ("PATCH", "issues/10")])
        self.assertEqual(gh.sent[1][2], {"state": "closed"})
        self.assertEqual(len(said), 4)                             # "works", and one line for each issue closed

    def test_a_credential_issue_stays_open_until_the_model_has_answered(self):
        # "failed" does not show that a refused credential now works.
        _, _, gh = self.go("failed", issues=[issue(9, "refused"), issue(10, "failed")])
        self.assertEqual(gh.sent, [])
        _, _, gh = self.go("missing", issues=[issue(9, "refused")])
        self.assertEqual([p for _, p, _ in gh.sent], ["issues"])

    def test_the_renewal_issue_closes_when_the_day_is_brought_up_to_date(self):
        _, _, gh = self.go("working", "2027-10-30", issues=[issue(12, "renewal-due")])
        self.assertEqual([(m, p) for m, p, _ in gh.sent], [("POST", "issues/12/comments"), ("PATCH", "issues/12")])
        _, _, gh = self.go("refused", "2027-10-30", issues=[issue(12, "renewal-due"), issue(9, "refused")])
        self.assertEqual([(m, p) for m, p, _ in gh.sent], [("POST", "issues/12/comments"), ("PATCH", "issues/12")])
        _, _, gh = self.go("working", "2026-11-27", issues=[issue(12, "renewal-due")])
        self.assertEqual(gh.sent, [])

    def test_a_result_that_did_not_arrive_is_a_failure(self):
        gh = FakeGitHub()
        code, _ = health.report(gh, None, TODAY)
        self.assertEqual((code, gh.sent[0][2]["title"]), (1, health.PROBLEMS["failed"][0]))

    def test_nothing_from_the_result_reaches_an_issue(self):
        gh = FakeGitHub()
        health.report(gh, {"state": "refused", "renewed": "2026-11-27", "why": "PLANTED", "title": "PLANTED"}, TODAY)
        self.assertNotIn("PLANTED", json.dumps(gh.sent))


class Main(unittest.TestCase):
    def run_main(self, argv, gh):
        out = io.StringIO()
        env = {"GITHUB_REPOSITORY": "Owner/Repo", "GITHUB_TOKEN": "gh-token", **ENV}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(health.review_gate, "GitHub", return_value=gh), \
                contextlib.redirect_stdout(out):
            code = health.main(argv)
        return code, out.getvalue()

    def test_check_writes_only_the_state_and_the_day(self):
        found = {"state": "refused", "renewed": None, "why": "no (status 401)"}
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(health, "check", lambda gh, environ, model: found):
                code, said = self.run_main(["check", "--out", os.path.join(d, "r.json")], FakeGitHub())
            with open(os.path.join(d, "r.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"state": "refused", "renewed": None})
        self.assertEqual(code, 0)
        self.assertIn("refused", said)

    def test_report_fails_the_run_and_says_so_when_the_reviewer_cannot_work(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"state": "refused", "renewed": None}, f)
            gh = FakeGitHub()
            code, said = self.run_main(["report", "--result", path], gh)
            self.assertEqual((code, len(gh.sent)), (1, 1))
            self.assertIn("::error::", said)
            code, said = self.run_main(["report", "--result", path], FakeGitHub(refuse=410))
            self.assertEqual(code, 1)
            self.assertIn("could not be opened", said)
            self.assertIn("'refused'", said)

    def test_an_empty_result_file_is_reported_as_a_failure(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.json")
            open(path, "w").close()
            gh = FakeGitHub()
            code, _ = self.run_main(["report", "--result", path], gh)
            self.assertEqual((code, gh.sent[0][2]["title"]), (1, health.PROBLEMS["failed"][0]))


if __name__ == "__main__":
    unittest.main()
