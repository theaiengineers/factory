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
    """The API replaced by canned answers. `prs` maps a pull request number to (base, files, comments).

    `on_write[n]` is called with the fake just after the n-th status is written, which is how a test makes something
    happen between the gate's write and its next look: `open`, `close`, `move`, `say`, or setting `fail_on`."""
    repo = "o/r"
    committed_at = "2026-01-15T10:00:00Z"
    factory = FACTORY

    def __init__(self, prs, state="open", fail_on=None):
        self.prs = {n: (base, files, list(comments)) for n, (base, files, comments) in prs.items()}
        self.fail_on, self.statuses, self.on_write = fail_on, [], {}
        self.closed = set(prs) if state == "closed" else set()
        self.heads, self.lag, self.bad_files = {}, {}, set()
        self.permissions, self.unknown, self.lookup_error, self.fork = {"reviewer": "write"}, (), None, False
        self.permission_reads, self.paths, self.file_reads, self.comment_reads, self.sibling_reads = [], [], [], 0, 0

    # what a test makes happen
    def open(self, number, base, files, comments=(), head=HEAD):
        self.prs[number] = (base, files, list(comments))
        self.heads[number] = head

    def close(self, number):
        self.closed.add(number)

    def move(self, number, sha):
        self.heads[number] = sha

    def say(self, number, comment):
        self.prs[number][2].append(comment)

    # the API
    def _pr(self, number, head=None):
        return {"number": number, "state": "closed" if number in self.closed else "open",
                "head": {"sha": head or self.heads.get(number, HEAD)},
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
        self._maybe_fail("pr")
        return self._pr(int(path.rsplit("/", 1)[1]))

    def pages(self, path, limit):
        if path.startswith("commits/"):
            self._maybe_fail("siblings")
            self.sibling_reads += 1
            if self.fork:
                return []
            sha = path.split("/")[1]
            # `lag` is a listing that still shows a pull request on a commit it has already left
            listed = [self._pr(n, self.lag.get(n)) for n in self.prs]
            return [p for p in listed if p["head"]["sha"] == sha]
        number = int(path.split("/")[1])
        if path.endswith("/files"):
            self._maybe_fail("files")
            if number in self.bad_files:
                raise OSError(f"HTTP 502 on the files of #{number}")
            return [dict(f) for f in self.prs[number][1]]
        self._maybe_fail("comments")
        self.comment_reads += 1
        return list(self.prs[number][2])

    def raw_file(self, path, ref):
        self.file_reads.append((path, ref))
        if self.factory is None:
            raise urllib.error.HTTPError("u", 404, "Not Found", None, None)
        return self.factory

    def set_status(self, sha, state, description, target_url, context="independent-review"):
        self.statuses.append((sha, state, description, context))
        then = self.on_write.get(len(self.statuses))
        if then:
            then(self)


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

    def test_only_open_pull_requests_on_the_commit_are_judged(self):
        gh = FakeGitHub({7: ("side", BILLING, []), 8: ("main", DOCS, [])})
        gh.close(7)
        result = review_gate.evaluate(gh, 7)
        self.assertEqual(([r["pr"] for r in result["results"]], result["state"]), ([8], "success"))
        alone = one(BILLING, state="closed")                                      # nobody open on the commit
        result = review_gate.evaluate(alone, 7)
        self.assertEqual((result["results"], result["state"], result["open"]), ([], None, False))

    def test_a_named_commit_is_judged_for_whoever_is_on_it_not_for_the_pull_request_asked_about(self):
        gh = FakeGitHub({7: ("side", DOCS, []), 8: ("main", BILLING, [])})
        gh.move(7, NEW)
        old = review_gate.evaluate(gh, 7, HEAD)
        self.assertEqual((old["head_sha"], [r["pr"] for r in old["results"]], old["state"]), (HEAD, [8], "failure"))
        new = review_gate.evaluate(gh, 7)
        self.assertEqual((new["head_sha"], [r["pr"] for r in new["results"]], new["state"]), (NEW, [7], "success"))
        gh.move(8, NEW)
        self.assertIsNone(review_gate.evaluate(gh, 7, HEAD)["state"])

    def test_a_pull_request_from_a_fork_is_judged_though_the_listing_omits_it(self):
        gh = one(BILLING)
        gh.fork = True
        result = review_gate.evaluate(gh, 7)
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

    def written(self, gh):
        return [("old" if s[0] == HEAD else "new" if s[0] == NEW else s[0][:3], s[1]) for s in gh.statuses]

    # the ordinary cases
    def test_sets_the_status_under_the_default_name(self):
        gh = one(BILLING, [posted(marker())])
        code, _ = self.run_main(gh)
        self.assertEqual((code, [(s[1], s[3]) for s in gh.statuses]), (0, [("success", "independent-review")]))

    def test_a_shadow_run_sets_a_differently_named_status(self):
        gh = one(BILLING)
        code, out = self.run_main(gh, "--context", "independent-review-shadow")
        self.assertEqual((code, [(s[1], s[3]) for s in gh.statuses]), (0, [("failure", "independent-review-shadow")]))
        self.assertIn("Set independent-review-shadow=failure", out)

    def test_a_settled_answer_is_judged_twice_and_written_once(self):
        gh = one(BILLING, [posted(marker())])
        self.run_main(gh)
        self.assertEqual((gh.sibling_reads, len(gh.statuses)), (2, 1))

    def test_a_dry_run_sets_nothing(self):
        gh = one(BILLING)
        code, out = self.run_main(gh, "--dry-run")
        self.assertEqual((code, gh.statuses), (0, []))
        self.assertIn("Dry run", out)

    def test_a_closed_pull_request_alone_on_its_commit_sets_nothing(self):
        gh = one(BILLING, state="closed")
        code, out = self.run_main(gh)
        self.assertEqual((code, gh.statuses), (0, []))
        self.assertIn("No open pull request is on aaaaaaa", out)

    def test_a_pull_request_from_a_fork_gets_its_status_once(self):
        gh = one(BILLING, [posted(marker())])
        gh.fork = True
        self.assertEqual((self.run_main(gh)[0], self.written(gh)), (0, [("old", "success")]))

    def test_an_error_sets_the_error_status_and_fails_the_job(self):
        gh = one(BILLING, fail_on="files")
        code, _ = self.run_main(gh)
        self.assertEqual((code, self.written(gh)), (1, [("old", "error")]))

    def test_failing_to_read_the_pull_request_before_anything_was_written_fails_the_job_and_sets_nothing(self):
        gh = one(BILLING, [posted(marker())], fail_on="pr")
        with self.assertRaises(OSError):
            self.run_main(gh)
        self.assertEqual(gh.statuses, [])

    # something changes between the write and the next look
    def test_a_pull_request_opened_on_the_same_commit_after_the_write_is_judged_before_the_run_ends(self):
        # #7 changes only prose. #8, into main on the same commit with a billing change, appears just after #7's
        # run has written `success`. Without the look after the write, the commit would keep it.
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.on_write = {1: lambda g: g.open(8, "main", BILLING)}
        code, out = self.run_main(gh)
        self.assertEqual((code, self.written(gh)), (0, [("old", "success"), ("old", "failure")]))
        self.assertIn("judging again", out)
        self.assertIn("#8 into main", gh.statuses[-1][2])

    def test_findings_recorded_on_a_sibling_after_the_write_are_seen(self):
        gh = FakeGitHub({7: ("side", BILLING, [posted(marker())]), 8: ("main", BILLING, [posted(marker())])})
        gh.on_write = {1: lambda g: g.say(8, posted(marker(verdict="findings"), 5))}
        self.assertEqual((self.run_main(gh)[0], self.written(gh)), (0, [("old", "success"), ("old", "failure")]))

    def test_a_change_of_wording_alone_is_written_again(self):
        # Same state, different reason: no record at first, then findings. The status must say the current reason.
        gh = one(BILLING)
        gh.on_write = {1: lambda g: g.say(7, posted(marker(verdict="findings")))}
        self.run_main(gh)
        self.assertEqual(self.written(gh), [("old", "failure"), ("old", "failure")])
        self.assertIn("No independent review recorded", gh.statuses[0][2])
        self.assertIn("open findings", gh.statuses[1][2])

    def test_a_stale_pass_is_corrected_though_the_pull_request_was_merged_straight_after_the_write(self):
        # The stale `success` is what lets #7 auto-merge, so by the next look #7 is gone. #8 is still on the commit.
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.on_write = {1: lambda g: (g.open(8, "main", BILLING), g.close(7))}
        self.assertEqual((self.run_main(gh)[0], self.written(gh)), (0, [("old", "success"), ("old", "failure")]))

    def test_the_correction_is_itself_looked_at_again(self):
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.on_write = {1: lambda g: (g.open(8, "main", BILLING), g.close(7)), 2: lambda g: g.say(8, posted(marker()))}
        self.assertEqual((self.run_main(gh)[0], self.written(gh)), (0, [("old", "success"), ("old", "failure"), ("old", "success")]))

    def test_everyone_leaving_the_commit_after_the_write_ends_quietly(self):
        gh = one(BILLING, [posted(marker())])
        gh.on_write = {1: lambda g: g.close(7)}
        self.assertEqual((self.run_main(gh)[0], self.written(gh)), (0, [("old", "success")]))

    # the pull request gets a new head commit while the gate runs
    def test_a_stale_pass_on_the_old_commit_is_corrected_when_the_pull_request_gets_a_new_head(self):
        # #7 and #8 are different branches at the same commit. #7's run writes a stale `success` there, then #7 is
        # pushed to. #8 is still on the old commit and still needs a review. Then #7's new commit is judged too.
        gh = FakeGitHub({7: ("side", DOCS, [])})
        gh.on_write = {1: lambda g: (g.open(8, "main", BILLING), g.move(7, NEW))}
        code, _ = self.run_main(gh)
        self.assertEqual((code, self.written(gh)), (0, [("old", "success"), ("old", "failure"), ("new", "success")]))

    def test_the_old_commit_is_judged_for_every_pull_request_left_on_it_not_only_the_first_listed(self):
        # #7 and #8 (prose) and #9 (billing) share a commit. After #7's stale `success`, #7 and #8 are both pushed
        # to. #9 alone is left, and it needs a review.
        gh = FakeGitHub({7: ("side", DOCS, []), 8: ("side2", DOCS, [])})
        gh.on_write = {1: lambda g: (g.open(9, "main", BILLING), g.move(7, NEW), g.move(8, "d" * 40))}
        code, _ = self.run_main(gh)
        self.assertEqual((code, self.written(gh)), (0, [("old", "success"), ("old", "failure"), ("new", "success")]))
        self.assertIn("No independent review recorded", gh.statuses[1][2])

    def test_a_listing_that_still_shows_the_moved_pull_request_on_the_old_commit_does_not_hide_the_other(self):
        gh = FakeGitHub({7: ("side", DOCS, [])})

        def pushed(g):
            g.open(8, "main", BILLING)
            g.move(7, NEW)
            g.lag = {7: HEAD}                     # the listing has not caught up with the push
        gh.on_write = {1: pushed}
        self.run_main(gh)
        self.assertEqual(self.written(gh)[:2], [("old", "success"), ("old", "failure")])

    def test_a_new_head_with_nobody_left_on_the_old_commit_leaves_the_old_status_and_judges_the_new_one(self):
        gh = one(BILLING, [posted(marker())])
        gh.on_write = {1: lambda g: g.move(7, NEW)}
        code, out = self.run_main(gh)
        self.assertEqual((code, self.written(gh)), (0, [("old", "success"), ("new", "failure")]))
        self.assertIn("Review is for aaaaaaa, head is ccccccc", gh.statuses[-1][2])

    def test_an_old_commit_that_cannot_be_judged_is_marked_an_error_and_the_job_fails(self):
        gh = FakeGitHub({7: ("side", DOCS, [])})

        def pushed(g):
            g.open(8, "main", BILLING)
            g.move(7, NEW)
            g.bad_files = {8}
        gh.on_write = {1: pushed}
        code, _ = self.run_main(gh)
        self.assertEqual((code, self.written(gh)), (1, [("old", "success"), ("old", "error"), ("new", "success")]))

    def test_a_pull_request_that_keeps_getting_new_commits_is_followed_a_bounded_number_of_times(self):
        gh = one(DOCS)
        gh.on_write = {1: lambda g: g.move(7, NEW), 2: lambda g: g.move(7, "d" * 40), 3: lambda g: g.move(7, "e" * 40),
                       4: lambda g: g.move(7, "f" * 40)}
        code, _ = self.run_main(gh)
        self.assertEqual((code, len(gh.statuses)), (0, review_gate.RECHECKS))

    # the gate cannot look again
    def test_a_failed_listing_when_looking_again_ends_as_an_error(self):
        gh = one(BILLING, [posted(marker())])
        gh.on_write = {1: lambda g: setattr(g, "fail_on", "siblings")}
        self.assertEqual((self.run_main(gh)[0], self.written(gh)), (1, [("old", "success"), ("old", "error")]))

    def test_failing_to_read_the_pull_request_when_looking_again_ends_as_an_error(self):
        gh = one(BILLING, [posted(marker())])
        gh.on_write = {1: lambda g: setattr(g, "fail_on", "pr")}
        code, _ = self.run_main(gh)
        self.assertEqual((code, self.written(gh)), (1, [("old", "success"), ("old", "error")]))
        self.assertIn("could not judge again", gh.statuses[-1][2])

    def test_an_answer_that_never_settles_ends_as_an_error_not_a_stale_pass(self):
        gh = one(BILLING)
        answers = iter(["success", "failure"] * 10)

        def flip(_gh, number, sha=None):
            state = next(answers)
            return {"pr": number, "open": True, "head_sha": HEAD, "state": state, "description": state,
                    "results": [{"pr": number, "base": "main", "tier": NONE, "state": state, "description": state, "head_sha": HEAD}]}
        with mock.patch.object(review_gate, "evaluate", flip):
            code, _ = self.run_main(gh)
        self.assertEqual((code, self.written(gh)), (1, [("old", "success")] * 3 + [("old", "error")]))


if __name__ == "__main__":
    unittest.main()
