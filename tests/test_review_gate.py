import contextlib
import io
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from factory import review_gate, risk

HEAD = "a" * 40
NEW = "c" * 40
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


def comment(body: str, n: int = 1, association: str = "COLLABORATOR") -> dict:
    """A comment after trust_writers has set its association: COLLABORATOR is an account that can write."""
    return {"id": n, "created_at": f"2026-01-15T10:{n:02d}:00Z", "body": body, "author_association": association}


def posted(body: str, n: int = 1, association: str = "MEMBER", login: str = "reviewer") -> dict:
    """A comment as GitHub returns it, before the gate has looked up what its author may do."""
    return {"id": n, "created_at": f"2026-01-15T10:{n:02d}:00Z", "body": body, "author_association": association, "user": {"login": login}}


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
        # MEMBER is anyone in the organisation: it does not count until trust_writers has checked the account.
        self.assertEqual(review_gate.decide(HIGH, HEAD, [comment(marker(), association="MEMBER")])[0], "failure")
        for association in ("OWNER", "COLLABORATOR"):
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
        self.permissions, self.permission_reads, self.sibling_reads, self.arrives = {"reviewer": "write"}, [], 0, {}
        self.fork, self.unknown, self.later_comments = False, (), {}
        self.heads, self.moves = {}, {}
        self.paths, self.pr_reads, self.closed, self.closes, self.pr_read_fails_at, self.lookup_error = [], 0, set(), {}, None, None

    def _pr(self, number):
        return {"number": number, "state": "closed" if number in self.closed else self.state, "head": {"sha": self.heads.get(number, HEAD)},
                "base": {"ref": self.prs[number][0], "sha": OLD, "repo": {"default_branch": "main"}}}

    def _maybe_fail(self, what):
        if self.fail_on == what:
            raise OSError(f"HTTP 502 on {what}")

    def get(self, path):
        self.paths.append(path)
        if path.startswith("collaborators/"):
            self._maybe_fail("permission")
            if self.lookup_error:
                raise urllib.error.HTTPError("u", self.lookup_error, "refused", None, None)
            login = urllib.parse.unquote(path.split("/")[1])
            self.permission_reads.append(login)
            if login in self.unknown:
                raise urllib.error.HTTPError("u", 404, "Not Found", None, None)
            return {"permission": self.permissions.get(login, "read")}
        if path.startswith("commits/"):
            self._maybe_fail("commit")
            return {"commit": {"committer": {"date": self.committed_at}}}
        number = int(path.rsplit("/", 1)[1])
        self.pr_reads += 1
        if self.pr_reads == self.pr_read_fails_at:
            raise OSError("HTTP 502 on the pull request")
        if self.closes.get(number) == self.pr_reads:                 # closed or merged while the gate runs
            self.closed.add(number)
        if self.moves.get(number, (None,))[0] == self.pr_reads:      # a new commit pushed while the gate runs
            self.heads[number] = self.moves[number][1]
        return self._pr(number)

    def pages(self, path, limit):
        if path.startswith("commits/"):
            self._maybe_fail("siblings")
            self.sibling_reads += 1
            for number, (after, pr) in list(self.arrives.items()):      # a pull request opened while the gate runs
                if self.sibling_reads > after:
                    self.prs[number] = pr
            sha = path.split("/")[1]
            return [] if self.fork else [p for p in (self._pr(n) for n in self.prs) if p["head"]["sha"] == sha]
        number = int(path.split("/")[1])
        if path.endswith("/files"):
            self._maybe_fail("files")
            return [dict(f) for f in self.prs[number][1]]
        self._maybe_fail("comments")
        self.comment_reads += 1
        after, extra = self.later_comments.get(number, (0, []))      # comments posted while the gate runs
        return self.prs[number][2] + (extra if self.comment_reads > after else [])

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
        self.assertEqual(review_gate.evaluate(one(BILLING, [posted(marker())]), 7)["state"], "success")

    def test_a_pull_request_that_changes_the_factory_file_is_high_risk(self):
        result = review_gate.evaluate(one([{"filename": "factory.yml", "status": "modified"}]), 7)
        self.assertEqual(result["state"], "failure")
        self.assertIn("factory rules", result["description"])

    def test_the_head_commits_time_is_read_and_an_earlier_record_is_refused(self):
        gh = one(BILLING, [posted(marker())])
        gh.committed_at = "2026-01-15T11:00:00Z"
        self.assertIn("older than commit", review_gate.evaluate(gh, 7)["description"])

    def test_only_an_account_that_can_write_to_the_repository_is_trusted_whatever_its_association(self):
        for association in ("MEMBER", "CONTRIBUTOR", "COLLABORATOR", "NONE"):
            with self.subTest(association=association):
                gh = one(BILLING, [posted(marker(), 1, association, "writer"), posted(marker(), 2, association, "writer")])
                gh.permissions = {"writer": "write"}
                self.assertEqual(review_gate.evaluate(gh, 7)["state"], "success")
                self.assertEqual(gh.permission_reads, ["writer"])                 # looked up once per account
                # The same association from an account that can only read: an organisation member, say.
                self.assertEqual(review_gate.evaluate(one(BILLING, [posted(marker(), 1, association, "reader")]), 7)["state"], "failure")

    def test_an_owner_is_trusted_without_a_lookup(self):
        gh = one(BILLING, [posted(marker(), 1, "OWNER", "owner")])
        self.assertEqual((review_gate.evaluate(gh, 7)["state"], gh.permission_reads), ("success", []))

    def test_a_failed_lookup_is_an_error_so_findings_the_gate_could_not_check_are_never_dropped(self):
        # A pass, then findings from a second writer whose permission cannot be read. Dropping the findings would
        # leave the pass standing.
        records = [posted(marker(), 1, "OWNER", "owner"), posted(marker(verdict="findings"), 2, "MEMBER", "second")]
        gh = one(BILLING, records, fail_on="permission")
        self.assertEqual(review_gate.evaluate(gh, 7)["state"], "error")
        gh = one(BILLING, records)
        gh.permissions = {"second": "write"}
        self.assertEqual(review_gate.evaluate(gh, 7)["state"], "failure")
        self.assertEqual(review_gate.evaluate(one(BILLING, [posted(marker())], fail_on="permission"), 7)["state"], "error")
        for refused in (403, 429, 500, 503):                                      # GitHub's own answers, not only a dead network
            gh = one(BILLING, records)
            gh.lookup_error = refused
            self.assertEqual(review_gate.evaluate(gh, 7)["state"], "error", refused)

    def test_an_account_github_does_not_know_is_untrusted_and_its_login_is_quoted(self):
        gh = one(BILLING, [posted(marker(), 1, "NONE", "odd[bot]")])
        gh.unknown = ("odd[bot]",)
        self.assertEqual((review_gate.evaluate(gh, 7)["state"], gh.permission_reads), ("failure", ["odd[bot]"]))
        self.assertIn("collaborators/odd%5Bbot%5D/permission", gh.paths)           # the path as it was asked for
        gh = one(BILLING, [{**posted(marker()), "user": None}])                   # a deleted account
        self.assertEqual(review_gate.evaluate(gh, 7)["state"], "failure")

    def test_only_comments_that_carry_a_marker_are_looked_up(self):
        gh = one(BILLING, [posted("just a remark", 1, "NONE", "anyone")])
        review_gate.evaluate(gh, 7)
        self.assertEqual(gh.permission_reads, [])

    def test_findings_from_an_account_that_cannot_write_do_not_block_a_trusted_pass(self):
        gh = one(BILLING, [posted(marker(), 1), posted(marker(verdict="findings"), 2, "MEMBER", "reader")])
        self.assertEqual(review_gate.evaluate(gh, 7)["state"], "success")

    def test_the_worst_result_among_pull_requests_sharing_the_head_commit_is_the_one_set(self):
        gh = FakeGitHub({7: ("side", DOCS, []), 8: ("main", BILLING, [])})
        result = review_gate.evaluate(gh, 7)
        self.assertEqual(result["state"], "failure")
        self.assertIn("#8 into main", result["description"])

    def test_a_closed_pull_request_is_not_judged_when_open_ones_share_its_commit(self):
        gh = FakeGitHub({7: ("side", BILLING, []), 8: ("main", DOCS, [])})
        gh.closed = {7}
        result = review_gate.evaluate(gh, 7)
        self.assertEqual(([r["pr"] for r in result["results"]], result["state"]), ([8], "success"))
        alone = one(BILLING, state="closed")
        result = review_gate.evaluate(alone, 7)
        self.assertEqual(([r["pr"] for r in result["results"]], result["state"]), ([7], "failure"))

    def test_any_failure_to_read_is_an_error_never_a_pass(self):
        for what in ("siblings", "files", "comments", "commit"):
            with self.subTest(what=what):
                self.assertEqual(review_gate.evaluate(one(BILLING, [posted(marker())], fail_on=what), 7)["state"], "error")

    def test_a_missing_or_unusable_factory_file_is_an_error_that_says_so(self):
        gh = one(BILLING, [posted(marker())])
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
        gh = one(BILLING, [posted(marker())])
        code, _ = self.run_main(gh)
        self.assertEqual((code, [(s[1], s[3]) for s in gh.statuses]), (0, [("success", "independent-review")]))

    def test_a_pull_request_that_appears_on_the_same_commit_while_the_gate_runs_is_judged_before_the_run_ends(self):
        # #7 changes only prose. #8, into main with the same head commit and a billing change, is opened after #7's
        # run has listed the pull requests sharing the commit. Without the check after the write, #7's run would
        # leave `success` on a commit that #8 needs reviewed.
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.arrives = {8: (1, ("main", BILLING, []))}
        code, out = self.run_main(gh)
        self.assertEqual([s[1] for s in gh.statuses], ["success", "failure"])
        self.assertEqual(code, 0)
        self.assertIn("judging again", out)

    def test_findings_recorded_on_a_sibling_while_the_gate_runs_are_seen_before_the_run_ends(self):
        gh = FakeGitHub({7: ("side", BILLING, [posted(marker())]), 8: ("main", BILLING, [posted(marker())])})
        gh.later_comments = {8: (2, [posted(marker(verdict="findings"), 5)])}     # after the first judging has read both
        code, _ = self.run_main(gh)
        self.assertEqual((code, [s[1] for s in gh.statuses]), (0, ["success", "failure"]))

    def test_a_stale_pass_is_corrected_even_when_the_pull_request_is_merged_before_the_gate_looks_again(self):
        # #7 (prose, into a side branch) is judged before #8 (billing, into main, same head commit) exists. #7's
        # stale `success` is what lets it auto-merge, so by the time the gate judges again #7 is no longer open. The
        # run wrote that `success`: it must still replace it with what the commit's open pull request needs.
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.arrives = {8: (1, ("main", BILLING, []))}
        gh.closes = {7: 3}                       # the third read of #7: after the write and the first look back
        code, _ = self.run_main(gh)
        self.assertEqual((code, [s[1] for s in gh.statuses]), (0, ["success", "failure"]))
        self.assertIn("No independent review recorded", gh.statuses[-1][2])

    def test_merged_between_the_write_and_the_first_look_back(self):
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.arrives = {8: (1, ("main", BILLING, []))}
        gh.closes = {7: 2}                       # the very next read of #7 after the write
        code, _ = self.run_main(gh)
        self.assertEqual((code, [s[1] for s in gh.statuses]), (0, ["success", "failure"]))

    def test_a_correction_written_for_a_closed_pull_request_is_itself_looked_at_again(self):
        # After the forced `failure`, findings on #8 are answered by a pass: the run that owns the status follows it.
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.arrives = {8: (1, ("main", BILLING, []))}
        gh.closes = {7: 2}
        gh.later_comments = {8: (2, [posted(marker())])}      # the pass lands after the corrected judging has read #8
        code, _ = self.run_main(gh)
        self.assertEqual((code, [s[1] for s in gh.statuses]), (0, ["success", "failure", "success"]))

    def test_a_stale_pass_on_the_old_commit_is_corrected_when_the_pull_request_gets_a_new_head(self):
        # #7 and #8 are different branches at the same commit. #7's run writes a stale `success` there, then #7 is
        # pushed to a new commit before the gate looks again. #8 is still on the old commit and still needs a review.
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.arrives = {8: (1, ("main", BILLING, []))}
        gh.moves = {7: (2, NEW)}
        code, out = self.run_main(gh)
        self.assertEqual((code, [(s[0], s[1]) for s in gh.statuses]), (0, [(HEAD, "success"), (HEAD, "failure"), (NEW, "success")]))
        self.assertIn("for the pull requests still on it", out)

    def test_a_new_head_with_nobody_left_on_the_old_commit_leaves_the_old_status(self):
        gh = one(BILLING, [posted(marker())])
        gh.moves = {7: (2, NEW)}
        code, out = self.run_main(gh)
        self.assertEqual((code, [(s[0], s[1]) for s in gh.statuses]), (0, [(HEAD, "success"), (NEW, "failure")]))
        self.assertIn("no open pull request is on it", out)

    def test_failing_to_judge_the_old_commit_after_a_new_head_marks_it_an_error(self):
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.arrives = {8: (1, ("main", BILLING, []))}
        gh.moves = {7: (2, NEW)}
        real = gh.pages

        def pages(path, limit):
            if path == f"pulls/8/files":
                raise OSError("HTTP 502")
            return real(path, limit)
        gh.pages = pages
        code, _ = self.run_main(gh)
        self.assertEqual([(s[0], s[1]) for s in gh.statuses], [(HEAD, "success"), (HEAD, "error"), (NEW, "success")])

    def test_a_change_of_wording_alone_is_written_again(self):
        # Same state, different reason: no record at first, then findings. The status must say the current reason.
        gh = one(BILLING)
        gh.later_comments = {7: (1, [posted(marker(verdict="findings"))])}
        code, _ = self.run_main(gh)
        self.assertEqual([s[1] for s in gh.statuses], ["failure", "failure"])
        self.assertIn("No independent review recorded", gh.statuses[0][2])
        self.assertIn("open findings", gh.statuses[1][2])

    def test_a_pull_request_closed_before_the_gate_ever_wrote_sets_nothing(self):
        gh = one(BILLING, state="closed")
        code, out = self.run_main(gh)
        self.assertEqual((code, gh.statuses), (0, []))
        self.assertIn("not open", out)

    def test_failing_to_read_the_pull_request_when_looking_again_ends_as_an_error(self):
        gh = one(BILLING, [posted(marker())])
        gh.pr_read_fails_at = 2                  # the first read judged it; the read for the look back fails
        code, out = self.run_main(gh)
        self.assertEqual((code, [s[1] for s in gh.statuses]), (1, ["success", "error"]))
        self.assertIn("could not judge again", gh.statuses[-1][2])

    def test_failing_to_read_the_pull_request_before_anything_was_written_fails_the_job_and_sets_nothing(self):
        gh = one(BILLING, [posted(marker())])
        gh.pr_read_fails_at = 1
        with self.assertRaises(OSError):
            self.run_main(gh)
        self.assertEqual(gh.statuses, [])

    def test_a_pull_request_from_a_fork_which_the_listing_omits_gets_its_status_once(self):
        gh = one(BILLING, [posted(marker())])
        gh.fork = True
        code, _ = self.run_main(gh)
        self.assertEqual((code, [s[1] for s in gh.statuses]), (0, ["success"]))
        gh = one(BILLING)
        gh.fork = True
        self.assertEqual((self.run_main(gh)[0], [s[1] for s in gh.statuses]), (0, ["failure"]))

    def test_an_answer_that_never_settles_ends_as_an_error_not_a_stale_pass(self):
        gh = one(BILLING)
        answers = iter(["success", "failure"] * 10)

        def flip(_gh, number):
            state = next(answers)
            return {"pr": number, "open": True, "head_sha": HEAD, "state": state, "description": state,
                    "results": [{"pr": number, "base": "main", "tier": NONE, "state": state, "description": state, "head_sha": HEAD}]}
        with mock.patch.object(review_gate, "evaluate", flip):
            code, _ = self.run_main(gh)
        self.assertEqual((code, [s[1] for s in gh.statuses]), (1, ["success", "success", "success", "error"]))

    def test_a_failed_listing_when_looking_again_ends_as_an_error(self):
        gh = one(BILLING, [posted(marker())])
        real = gh.pages

        def pages(path, limit):
            if path.startswith("commits/") and gh.sibling_reads >= 1:
                raise OSError("HTTP 502")
            return real(path, limit)
        gh.pages = pages
        code, _ = self.run_main(gh)
        self.assertEqual((code, [s[1] for s in gh.statuses]), (1, ["success", "error"]))

    def test_a_settled_answer_is_judged_twice_and_written_once(self):
        gh = one(BILLING, [posted(marker())])
        self.run_main(gh)
        self.assertEqual((gh.sibling_reads, len(gh.statuses)), (2, 1))

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
