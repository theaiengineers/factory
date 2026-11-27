import contextlib
import io
import os
import unittest
import urllib.error
from unittest import mock

from factory import review_gate, risk

HEAD = "a" * 40
OLD = "b" * 40
HIGH = {"review": "high", "areas": ["payments"], "files": 1, "code": ["api/billing/charge.py"],
        "reasons": [{"area": "payments", "file": "api/billing/charge.py", "rule": "api/billing/**", "why": "moves money"}]}
CODE = {"review": "code", "areas": [], "reasons": [], "code": ["cli/src/index.ts"], "files": 1}
NONE = {"review": "none", "areas": [], "files": 2, "reasons": [], "code": []}
FACTORY = """
spec: factory/0.1
intake:
  - station: qa
    uses: independent-review
    standard: docs/REVIEW_STANDARD.md
    skip_when_only: [docs/**]
risk:
  - area: payments
    why: moves money
    paths: [api/billing/**]
"""


def marker(sha: str = HEAD, verdict: str = "pass") -> str:
    return f"<!-- independent-review sha={sha} verdict={verdict} -->"


def comment(body: str, n: int = 1, association: str = "MEMBER") -> dict:
    return {"id": n, "created_at": f"2026-01-15T10:{n:02d}:00Z", "body": body, "author_association": association}


class Marker(unittest.TestCase):
    def test_parses_sha_and_verdict(self):
        self.assertEqual(review_gate.parse_marker(marker() + "\n## Independent review\nNothing blocking."), (HEAD, "pass"))
        self.assertEqual(review_gate.parse_marker(marker(verdict="findings")), (HEAD, "findings"))

    def test_sha_is_compared_in_lower_case_and_spacing_is_free(self):
        self.assertEqual(review_gate.parse_marker(f"<!--independent-review   sha={'A' * 40}  verdict=pass-->\r\nbody"), (HEAD, "pass"))
        self.assertEqual(review_gate.parse_marker("\n\n" + marker()), (HEAD, "pass"))

    def test_not_a_marker(self):
        for body in (None, "", "Looks good to me", "Some words first\n" + marker(), "> " + marker(), "`" + marker() + "`",
                     marker(sha="abc1234"), marker(sha="g" * 40), marker(verdict="approved"),
                     marker(verdict="pass ") + " trailing words", f"<!-- independent-review verdict=pass sha={HEAD} -->",
                     f"<!-- independent-review sha={HEAD} verdict=pass"):
            with self.subTest(body=body):
                self.assertIsNone(review_gate.parse_marker(body))

    def test_malformed_attempts_are_recognised_so_the_status_can_say_so(self):
        self.assertTrue(review_gate.is_malformed_marker(marker(sha="abc1234")))
        self.assertTrue(review_gate.is_malformed_marker(marker(verdict="approved")))
        self.assertFalse(review_gate.is_malformed_marker(marker()))
        self.assertFalse(review_gate.is_malformed_marker("an ordinary comment"))


class Decision(unittest.TestCase):
    def test_no_code_passes_without_a_record(self):
        self.assertEqual(review_gate.decide(NONE, HEAD, []), ("success", "No code changed: no independent review needed"))

    def test_a_code_change_outside_the_risk_areas_needs_a_record(self):
        state, description = review_gate.decide(CODE, HEAD, [])
        self.assertEqual(state, "failure")
        self.assertIn("No independent review recorded", description)
        self.assertIn("code change, not high risk", description)
        self.assertEqual(review_gate.decide(CODE, HEAD, [comment(marker())])[0], "success")
        self.assertEqual(review_gate.decide(CODE, HEAD, [comment(marker(verdict="findings"))])[0], "failure")
        text = review_gate.summary(CODE, "failure", description, HEAD, "docs/REVIEW_STANDARD.md")
        self.assertIn("every code change is reviewed", text)
        self.assertIn("`cli/src/index.ts`", text)
        self.assertIn("`docs/REVIEW_STANDARD.md`", text)
        self.assertNotIn("The standard", review_gate.summary(CODE, "failure", description, HEAD))

    def test_high_risk_follows_the_record_and_names_the_area(self):
        state, description = review_gate.decide(HIGH, HEAD, [comment("LGTM")])
        self.assertEqual(state, "failure")
        self.assertIn("No independent review recorded for aaaaaaa", description)
        self.assertIn("high risk: payments", description)
        state, description = review_gate.decide(HIGH, HEAD, [comment(marker() + "\nNothing blocking.")])
        self.assertEqual(state, "success")
        self.assertIn("process control, not proof", description)
        self.assertIn("| payments | `api/billing/charge.py` | api/billing/** |", review_gate.summary(HIGH, state, description, HEAD))

    def test_a_record_written_before_the_head_commit_existed_is_not_counted(self):
        early, late = comment(marker(), 1), comment(marker(), 30)
        state, description = review_gate.decide(HIGH, HEAD, [early], "2026-01-15T10:15:00Z")
        self.assertEqual(state, "failure")
        self.assertIn("older than commit aaaaaaa", description)
        self.assertEqual(review_gate.decide(HIGH, HEAD, [early, late], "2026-01-15T10:15:00Z")[0], "success")
        self.assertEqual(review_gate.decide(HIGH, HEAD, [early, comment(marker(verdict="findings"), 30)], "2026-01-15T10:15:00Z")[0], "failure")

    def test_a_new_commit_invalidates_the_record(self):
        state, description = review_gate.decide(HIGH, HEAD, [comment(marker(sha=OLD))])
        self.assertEqual(state, "failure")
        self.assertIn("Review is for bbbbbbb, head is aaaaaaa", description)

    def test_the_newest_record_for_the_head_commit_wins(self):
        findings, passed = marker(verdict="findings"), marker()
        self.assertEqual(review_gate.decide(HIGH, HEAD, [comment(findings, 1), comment(passed, 2)])[0], "success")
        self.assertEqual(review_gate.decide(HIGH, HEAD, [comment(passed, 1), comment(findings, 2)])[0], "failure")
        self.assertEqual(review_gate.decide(HIGH, HEAD, [comment(passed, 2), comment(findings, 1)])[0], "success")
        self.assertEqual(review_gate.decide(HIGH, HEAD, [comment(passed, 1), comment(marker(sha=OLD, verdict="findings"), 2)])[0], "success")

    def test_records_from_accounts_without_write_access_are_ignored(self):
        for association in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", None):
            self.assertEqual(review_gate.decide(HIGH, HEAD, [comment(marker(), association=association)])[0], "failure")
        for association in ("OWNER", "MEMBER", "COLLABORATOR"):
            self.assertEqual(review_gate.decide(HIGH, HEAD, [comment(marker(), association=association)])[0], "success")

    def test_a_malformed_marker_fails_and_points_at_the_standard(self):
        state, description = review_gate.decide(HIGH, HEAD, [comment(marker(sha=HEAD[:7]))], None, "docs/REVIEW_STANDARD.md")
        self.assertEqual(state, "failure")
        self.assertIn("malformed: see docs/REVIEW_STANDARD.md", description)

    def test_descriptions_fit_githubs_limit(self):
        many = dict(HIGH, areas=["sign-in", "api keys", "payments", "database structure", "deletion and retention", "ingest",
                                 "organisation membership", "permissions", "release tooling", "review gate"])
        for comments in ([], [comment(marker())], [comment(marker(verdict="findings"))], [comment(marker(sha=OLD))]):
            _, description = review_gate.decide(many, HEAD, comments)
            self.assertLessEqual(len(description), review_gate.DESCRIPTION_LIMIT)
            self.assertTrue(description)


class FakeGitHub:
    """The API replaced by canned answers. `prs` maps a pull request number to (base, files, comments)."""
    repo = "o/r"
    committed_at = "2026-01-15T10:00:00Z"
    factory = FACTORY

    def __init__(self, prs, state="open", fail_on=None):
        self.prs, self.state, self.fail_on, self.statuses, self.comment_reads, self.file_reads = prs, state, fail_on, [], 0, []

    def _pr(self, number):
        return {"number": number, "state": self.state, "head": {"sha": HEAD},
                "base": {"ref": self.prs[number][0], "sha": OLD, "repo": {"default_branch": "main"}}}

    def _maybe_fail(self, what):
        if self.fail_on == what:
            raise OSError(f"HTTP 502 on {what}")

    def get(self, path):
        if path.startswith("collaborators/"):
            self._maybe_fail("permission")
            login = path.split("/")[1]
            self.permission_reads = getattr(self, "permission_reads", []) + [login]
            return {"permission": getattr(self, "permissions", {}).get(login, "read")}
        if path.startswith("commits/"):
            self._maybe_fail("commit")
            return {"commit": {"committer": {"date": self.committed_at}}}
        return self._pr(int(path.rsplit("/", 1)[1]))

    def pages(self, path, limit):
        if path.startswith("commits/"):
            self._maybe_fail("siblings")
            return [self._pr(n) for n in self.prs]
        number = int(path.split("/")[1])
        if path.endswith("/files"):
            self._maybe_fail("files")
            return [dict(f) for f in self.prs[number][1]]
        self._maybe_fail("comments")
        self.comment_reads += 1
        return self.prs[number][2]

    def raw_file(self, path, ref):
        self.file_reads.append((path, ref))
        if self.factory is None:
            raise urllib.error.HTTPError("u", 404, "Not Found", None, None)
        return self.factory

    def set_status(self, sha, state, description, target_url, context="independent-review"):
        self.statuses.append((sha, state, description, context))


def one(files, comments=(), **kw):
    return FakeGitHub({7: ("main", files, list(comments))}, **kw)


BILLING = [{"filename": "api/billing/charge.py", "status": "modified"}]
DOCS = [{"filename": "docs/x.md", "status": "modified"}]


class Evaluate(unittest.TestCase):
    def test_the_rules_are_read_from_the_default_branch_never_from_the_pull_request(self):
        gh = one(BILLING)
        review_gate.evaluate(gh, 7)
        self.assertEqual(gh.file_reads, [("factory.yml", "main")])

    def test_a_change_that_needs_no_review_reads_no_comments(self):
        gh = one(DOCS, [comment(marker(verdict="findings"))])
        result = review_gate.evaluate(gh, 7)
        self.assertEqual((result["state"], result["head_sha"], gh.comment_reads), ("success", HEAD, 0))

    def test_a_high_risk_pull_request_follows_the_record(self):
        self.assertEqual(review_gate.evaluate(one(BILLING), 7)["state"], "failure")
        self.assertEqual(review_gate.evaluate(one(BILLING, [comment(marker())]), 7)["state"], "success")

    def test_a_pull_request_that_changes_the_factory_file_is_high_risk(self):
        result = review_gate.evaluate(one([{"filename": "factory.yml", "status": "modified"}]), 7)
        self.assertEqual(result["state"], "failure")
        self.assertIn("factory rules", result["description"])

    def test_the_head_commits_time_is_read_and_an_earlier_record_is_refused(self):
        gh = one(BILLING, [comment(marker())])
        gh.committed_at = "2026-01-15T11:00:00Z"
        self.assertIn("older than commit", review_gate.evaluate(gh, 7)["description"])

    def test_a_writer_the_token_sees_as_a_contributor_is_trusted_and_a_reader_is_not(self):
        def record(login):
            return {**comment(marker(), association="CONTRIBUTOR"), "user": {"login": login}}
        gh = one(BILLING, [record("writer"), record("writer")])
        gh.permissions = {"writer": "write"}
        self.assertEqual(review_gate.evaluate(gh, 7)["state"], "success")
        self.assertEqual(gh.permission_reads, ["writer"])
        self.assertEqual(review_gate.evaluate(one(BILLING, [record("reader")]), 7)["state"], "failure")
        gh = one(BILLING, [record("writer")], fail_on="permission")
        gh.permissions = {"writer": "admin"}
        self.assertEqual(review_gate.evaluate(gh, 7)["state"], "failure")

    def test_the_worst_result_among_pull_requests_sharing_the_head_commit_is_the_one_set(self):
        gh = FakeGitHub({7: ("side", DOCS, []), 8: ("main", BILLING, [])})
        result = review_gate.evaluate(gh, 7)
        self.assertEqual(result["state"], "failure")
        self.assertIn("#8 into main", result["description"])

    def test_any_failure_to_read_is_an_error_never_a_pass(self):
        for what in ("siblings", "files", "comments", "commit"):
            with self.subTest(what=what):
                self.assertEqual(review_gate.evaluate(one(BILLING, [comment(marker())], fail_on=what), 7)["state"], "error")

    def test_a_missing_or_unusable_factory_file_is_an_error_that_says_so(self):
        gh = one(BILLING, [comment(marker())])
        gh.factory = None
        result = review_gate.evaluate(gh, 7)
        self.assertEqual((result["state"], result["description"]), ("error", "Gate could not decide: No factory.yml on the default branch"))
        gh.factory = "spec: something-else\n"
        self.assertEqual(review_gate.evaluate(gh, 7)["state"], "error")

    def test_a_factory_file_without_a_review_station_checks_nothing(self):
        gh = one(BILLING)
        gh.factory = "spec: factory/0.1\n"
        result = review_gate.evaluate(gh, 7)
        self.assertEqual(result["state"], "success")
        self.assertIn("no independent-review station", result["description"])

    def test_a_very_large_pull_request_is_treated_as_high_risk(self):
        with mock.patch.object(review_gate, "MAX_FILES", 2):
            result = review_gate.evaluate(one(DOCS + DOCS), 7)
        self.assertEqual(result["state"], "failure")
        self.assertIn("unreadable diff", result["description"])


class Main(unittest.TestCase):
    def run_main(self, gh, *argv):
        out = io.StringIO()
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}
        with mock.patch.object(review_gate, "GitHub", return_value=gh), mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
            code = review_gate.main(["--pr", "7", *argv])
        return code, out.getvalue()

    def test_sets_the_status_under_the_default_name(self):
        gh = one(BILLING, [comment(marker())])
        code, _ = self.run_main(gh)
        self.assertEqual((code, [(s[1], s[3]) for s in gh.statuses]), (0, [("success", "independent-review")]))

    def test_a_shadow_run_sets_a_differently_named_status(self):
        gh = one(BILLING)
        code, out = self.run_main(gh, "--context", "independent-review-shadow")
        self.assertEqual((code, [(s[1], s[3]) for s in gh.statuses]), (0, [("failure", "independent-review-shadow")]))
        self.assertIn("Set independent-review-shadow=failure", out)

    def test_a_dry_run_and_a_closed_pull_request_set_nothing(self):
        gh = one(BILLING)
        self.run_main(gh, "--dry-run")
        closed = one(BILLING, state="closed")
        self.run_main(closed)
        self.assertEqual((gh.statuses, closed.statuses), ([], []))

    def test_an_error_sets_the_error_status_and_fails_the_job(self):
        gh = one(BILLING, fail_on="files")
        code, _ = self.run_main(gh)
        self.assertEqual((code, gh.statuses[0][1]), (1, "error"))


if __name__ == "__main__":
    unittest.main()
