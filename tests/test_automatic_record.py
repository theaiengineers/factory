"""How the gate counts a record from the automatic reviewer the factory file names."""
import unittest
import urllib.error

from factory import config, review_gate

HEAD = "a" * 40
BOT = "github-actions[bot]"
CODE = {"review": "code", "files": 1, "areas": [], "reasons": [], "code": ["api/util.py"]}
HIGH = {"review": "high", "files": 1, "areas": ["payments"], "code": ["api/billing/x.py"],
        "reasons": [{"area": "payments", "file": "api/billing/x.py", "rule": "api/billing/**", "why": "moves money"}]}
CODE_ONLY = {"account": BOT, "passes": ["code"]}
BOTH = {"account": BOT, "passes": ["code", "high"]}


def marker(verdict="pass", sha=HEAD):
    return f"<!-- independent-review sha={sha} verdict={verdict} -->\nbody"


def note(body, n, association):
    return {"id": n, "created_at": f"2026-01-15T10:{n:02d}:00Z", "body": body, "author_association": association}


def auto(verdict="pass", n=1):
    return note(marker(verdict), n, review_gate.AUTOMATIC)


def person(verdict="pass", n=1):
    return note(marker(verdict), n, "COLLABORATOR")


class Decide(unittest.TestCase):
    def test_an_automatic_pass_counts_for_a_level_it_is_allowed(self):
        self.assertEqual(review_gate.decide(CODE, HEAD, [auto()], automatic=CODE_ONLY)[0], "success")
        self.assertEqual(review_gate.decide(HIGH, HEAD, [auto()], automatic=BOTH)[0], "success")

    def test_an_automatic_pass_does_not_count_for_high_risk_unless_allowed(self):
        state, description = review_gate.decide(HIGH, HEAD, [auto()], automatic=CODE_ONLY)
        self.assertEqual(state, "failure")
        self.assertIn("also needs a person's review", description)

    def test_a_person_can_pass_what_the_automatic_reviewer_may_not(self):
        self.assertEqual(review_gate.decide(HIGH, HEAD, [auto(n=1), person(n=2)], automatic=CODE_ONLY)[0], "success")
        self.assertEqual(review_gate.decide(HIGH, HEAD, [person(n=1), auto(n=2)], automatic=CODE_ONLY)[0], "success")

    def test_automatic_findings_always_count(self):
        for tier, automatic in ((CODE, CODE_ONLY), (HIGH, CODE_ONLY), (HIGH, BOTH)):
            self.assertEqual(review_gate.decide(tier, HEAD, [auto("findings")], automatic=automatic)[0], "failure")
        state, description = review_gate.decide(HIGH, HEAD, [person(n=1), auto("findings", n=2)], automatic=CODE_ONLY)
        self.assertEqual(state, "failure")
        self.assertIn("open findings", description)

    def test_a_held_automatic_pass_does_not_clear_findings(self):
        state, description = review_gate.decide(HIGH, HEAD, [person("findings", n=1), auto(n=2)], automatic=CODE_ONLY)
        self.assertEqual(state, "failure")
        self.assertIn("open findings", description)
        state, _ = review_gate.decide(HIGH, HEAD, [auto("findings", n=1), auto(n=2)], automatic=CODE_ONLY)
        self.assertEqual(state, "failure")

    def test_an_automatic_pass_never_clears_a_persons_findings(self):
        for automatic in (CODE_ONLY, BOTH):
            state, description = review_gate.decide(CODE, HEAD, [person("findings", n=1), auto(n=2)], automatic=automatic)
            self.assertEqual(state, "failure")
            self.assertIn("open findings", description)
        # a person's pass does, and an automatic pass after that changes nothing
        self.assertEqual(review_gate.decide(CODE, HEAD, [person("findings", n=1), auto(n=2), person(n=3)], automatic=CODE_ONLY)[0], "success")

    def test_automatic_findings_in_between_do_not_let_an_automatic_pass_through(self):
        # A person's findings stay unanswered until a person passes, whatever the automatic reviewer records after.
        for automatic in (CODE_ONLY, BOTH):
            for tier in (CODE, HIGH):
                state, _ = review_gate.decide(tier, HEAD, [person("findings", n=1), auto("findings", n=2), auto(n=3)], automatic=automatic)
                self.assertEqual(state, "failure")
        cleared = [person("findings", n=1), auto("findings", n=2), person(n=3)]
        self.assertEqual(review_gate.decide(CODE, HEAD, cleared, automatic=CODE_ONLY)[0], "success")
        again = [person("findings", n=1), person(n=2), auto("findings", n=3), auto(n=4)]
        self.assertEqual(review_gate.decide(CODE, HEAD, again, automatic=CODE_ONLY)[0], "success")

    def test_every_ordering_keeps_the_two_rules(self):
        # Against a plain restatement of the rules, for every sequence of up to five records.
        import itertools
        kinds = [("p", "pass"), ("p", "findings"), ("a", "pass"), ("a", "findings")]
        for length in range(1, 6):
            for sequence in itertools.product(kinds, repeat=length):
                for allowed in (True, False):
                    verdict, unanswered, seen = None, False, set()
                    for who, said in sequence:
                        if who == "p":
                            verdict, unanswered = said, said == "findings"
                        elif said in seen:
                            continue
                        else:
                            seen.add(said)
                            if said == "pass" and (not allowed or unanswered):
                                continue
                            verdict = said
                    records = [(person if who == "p" else auto)(said, n=i + 1) for i, (who, said) in enumerate(sequence)]
                    got = review_gate.latest_record(records, HEAD, automatic_passes=allowed)["verdict"]
                    self.assertEqual(got, verdict, (sequence, allowed))

    def test_an_automatic_pass_clears_automatic_findings(self):
        self.assertEqual(review_gate.decide(CODE, HEAD, [auto("findings", n=1), auto(n=2)], automatic=CODE_ONLY)[0], "success")

    def test_a_signed_record_posted_again_is_a_repeat_and_is_ignored(self):
        # The signed text is public, so anything that can comment from the reviewer's account can post it again.
        replayed_pass = [auto(n=1), person("findings", n=2), auto(n=3)]
        self.assertEqual(review_gate.decide(CODE, HEAD, replayed_pass, automatic=CODE_ONLY)[0], "failure")
        replayed_findings = [auto("findings", n=1), person(n=2), auto("findings", n=3)]
        self.assertEqual(review_gate.decide(CODE, HEAD, replayed_findings, automatic=CODE_ONLY)[0], "success")
        held_twice = review_gate.latest_record([auto(n=1), auto(n=2)], HEAD, automatic_passes=False)
        self.assertEqual((held_twice["verdict"], held_twice["held"]), (None, 1))

    def test_a_repeat_for_another_commit_is_only_stale(self):
        other = note(marker(sha="b" * 40), 3, review_gate.AUTOMATIC)
        self.assertEqual(review_gate.decide(CODE, HEAD, [auto(n=1), other], automatic=CODE_ONLY)[0], "success")

    def test_without_an_automatic_reviewer_such_a_record_never_passes(self):
        # decide is given the association only when trust_writers set it; with no reviewer named, nothing is allowed.
        self.assertEqual(review_gate.decide(CODE, HEAD, [auto()])[0], "failure")
        self.assertEqual(review_gate.decide(CODE, HEAD, [auto()], automatic=None)[0], "failure")

    def test_an_automatic_record_for_another_commit_is_stale(self):
        stale = note(marker(sha="b" * 40), 1, review_gate.AUTOMATIC)
        state, description = review_gate.decide(CODE, HEAD, [stale], automatic=CODE_ONLY)
        self.assertEqual(state, "failure")
        self.assertIn("review again", description)


class Lookups:
    """Answers permission lookups; records who was asked about."""
    def __init__(self, writers=()):
        self.writers, self.asked = set(writers), []

    def get(self, path):
        login = path.split("/")[1]
        self.asked.append(login)
        if login in self.writers:
            return {"permission": "write"}
        raise urllib.error.HTTPError(path, 404, "Not Found", None, None)


YES = lambda c: True  # noqa: E731


def posted(body, login, kind, association="NONE", n=1):
    return {**note(body, n, association), "user": {"login": login, "type": kind}}


class Trust(unittest.TestCase):
    def test_the_named_bot_is_marked_without_a_lookup(self):
        gh = Lookups()
        out = review_gate.trust_writers(gh, [posted(marker(), BOT, "Bot")], CODE_ONLY, YES)
        self.assertEqual((out[0]["author_association"], gh.asked), (review_gate.AUTOMATIC, []))

    def test_the_named_bot_without_a_proof_does_not_count(self):
        for proven in (None, lambda c: False):
            out = review_gate.trust_writers(Lookups(), [posted(marker(), BOT, "Bot")], CODE_ONLY, proven)
            self.assertEqual(out[0]["author_association"], review_gate.UNTRUSTED)

    def test_the_bot_is_not_trusted_when_the_factory_file_names_none(self):
        gh = Lookups()
        out = review_gate.trust_writers(gh, [posted(marker(), BOT, "Bot")], None, YES)
        self.assertEqual(out[0]["author_association"], review_gate.UNTRUSTED)

    def test_another_bot_is_not_the_automatic_reviewer(self):
        out = review_gate.trust_writers(Lookups(), [posted(marker(), "other-app[bot]", "Bot")], CODE_ONLY, YES)
        self.assertEqual(out[0]["author_association"], review_gate.UNTRUSTED)

    def test_the_login_alone_is_not_enough(self):
        # GitHub must also say the account is a bot.
        for kind in ("User", None, "Organization"):
            out = review_gate.trust_writers(Lookups(), [posted(marker(), BOT, kind)], CODE_ONLY, YES)
            self.assertEqual(out[0]["author_association"], review_gate.UNTRUSTED, kind)

    def test_people_are_still_looked_up(self):
        gh = Lookups(writers={"alice"})
        out = review_gate.trust_writers(gh, [posted(marker(), "alice", "User", n=1), posted(marker(), "mallory", "User", n=2)], CODE_ONLY, YES)
        self.assertEqual([c["author_association"] for c in out], ["COLLABORATOR", review_gate.UNTRUSTED])

    def test_a_bot_comment_without_a_marker_is_left_alone(self):
        out = review_gate.trust_writers(Lookups(), [posted("Tests passed.", BOT, "Bot")], CODE_ONLY, YES)
        self.assertEqual(out[0]["author_association"], "NONE")


KEY = "k" * 40
REPO = "Owner/Repo"


def signed(verdict="pass", sha=HEAD, number=7, key=KEY, repo=REPO):
    return f"<!-- independent-review sha={sha} verdict={verdict} -->\n{review_gate.proof_line(key, repo, number, sha, verdict)}\nbody"


class Proof(unittest.TestCase):
    def test_a_signed_record_is_proven(self):
        self.assertTrue(review_gate.is_proven(KEY, REPO, 7, signed()))
        self.assertTrue(review_gate.is_proven(KEY, "owner/repo", 7, "\n\n" + signed("findings")))

    def test_a_proof_cannot_be_moved(self):
        good = signed()
        line = good.split("\n")[1]
        self.assertFalse(review_gate.is_proven(KEY, REPO, 8, good))                                   # another pull request
        self.assertFalse(review_gate.is_proven(KEY, "other/repo", 7, good))                           # another repository
        self.assertFalse(review_gate.is_proven("x" * 40, REPO, 7, good))                              # another key
        self.assertFalse(review_gate.is_proven(KEY, REPO, 7, f"<!-- independent-review sha={'b' * 40} verdict=pass -->\n{line}"))
        self.assertFalse(review_gate.is_proven(KEY, REPO, 7, signed("findings").split("\n")[0] + "\n" + line))

    def test_what_is_not_a_proof(self):
        first = signed().split("\n")[0]
        line = signed().split("\n")[1]
        for body in (None, "", first, first + "\n", first + "\nwords\n" + line, line + "\n" + first, first + "\n" + line.replace("v1", "v2"),
                     first + "\n<!-- factory-reviewer-proof v1 " + "0" * 64 + " -->", first + "\n" + line.upper()):
            self.assertFalse(review_gate.is_proven(KEY, REPO, 7, body), body)

    def test_no_usable_key_proves_nothing(self):
        for key in (None, "", "short", 7):
            self.assertFalse(review_gate.is_proven(key, REPO, 7, signed(key="short" if key in ("short", 7) else KEY)))
        self.assertFalse(review_gate.is_proven(KEY, "", 7, signed(repo="")))


class Judge(unittest.TestCase):
    """evaluate_one end to end: only a signed record from the workflow account counts."""
    RULES = "spec: factory/0.1\nintake:\n  - station: qa\n    uses: independent-review\n    automatic: {account: 'github-actions[bot]'}\n"

    def state(self, body, key=KEY, rules=None, kind="Bot", login=BOT):
        class GH(Lookups):
            repo, reviewer_key = REPO, key

            def get(self, path):
                if path.startswith("commits/"):
                    return {"commit": {"committer": {"date": "2026-01-15T09:00:00Z"}}}
                return super().get(path)

            def pages(self, path, limit):
                if "/files" in path:
                    return [{"filename": "api/util.py"}]
                return [posted(body, login, kind)]
        pr = {"number": 7, "head": {"sha": HEAD}, "base": {"ref": "main"}}
        return review_gate.evaluate_one(GH(), pr, config.parse(rules or self.RULES))["state"]

    def test_a_signed_pass_passes(self):
        self.assertEqual(self.state(signed()), "success")

    def test_an_unsigned_or_wrongly_signed_pass_does_not(self):
        self.assertEqual(self.state(marker()), "failure")
        self.assertEqual(self.state(signed(number=8)), "failure")
        self.assertEqual(self.state(signed(key="x" * 40)), "failure")

    def test_the_gate_needs_the_key(self):
        self.assertEqual(self.state(signed(), key=""), "failure")

    def test_a_signature_does_not_help_another_account(self):
        self.assertEqual(self.state(signed(), kind="User"), "failure")
        self.assertEqual(self.state(signed(), login="other[bot]"), "failure")

    def test_a_signed_record_counts_for_nothing_once_the_reviewer_is_not_named(self):
        self.assertEqual(self.state(signed(), rules="spec: factory/0.1\nintake:\n  - station: qa\n    uses: independent-review\n"), "failure")


RULES = """
spec: factory/0.1
intake:
  - station: qa
    uses: independent-review
    automatic: %s
"""


class Config(unittest.TestCase):
    def parse(self, automatic):
        return config.parse(RULES % automatic)["review"]["automatic"]

    def test_none_by_default(self):
        self.assertIsNone(config.parse("spec: factory/0.1\nintake:\n  - station: qa\n    uses: independent-review\n")["review"]["automatic"])

    def test_passes_default_to_code_only(self):
        self.assertEqual(self.parse("{account: 'github-actions[bot]'}"), CODE_ONLY)

    def test_high_can_be_allowed(self):
        self.assertEqual(self.parse("{account: 'github-actions[bot]', passes: [code, high]}"), BOTH)

    def test_the_account_must_be_a_bot_login(self):
        for bad in ("{account: alice}", "{account: '[bot]'}", "{account: 7}", "{}", "github-actions[bot]", "[a]",
                    "{account: ' [bot]'}", "{account: 'a b[bot]'}", "{account: 'alice[bot] '}", "{account: '-x[bot]'}"):
            with self.assertRaises(config.ConfigError, msg=bad):
                self.parse(bad)

    def test_a_misspelt_key_is_refused_not_ignored(self):
        with self.assertRaises(config.ConfigError) as caught:
            self.parse("{account: 'github-actions[bot]', pases: [code, high]}")
        self.assertIn("pases", str(caught.exception))

    def test_the_renewal_day_is_optional_and_must_be_a_day(self):
        for given in ("2026-11-27", "'2026-11-27'"):
            self.assertEqual(self.parse("{account: 'github-actions[bot]', credential_renewed: %s}" % given),
                             {**CODE_ONLY, "credential_renewed": "2026-11-27"})
        for bad in ("yesterday", "'2026-13-01'", "'2026-02-30'", "20261127", "2026-11-27T10:00:00Z", "[2026-11-27]", "0", "''"):
            with self.assertRaises(config.ConfigError, msg=bad) as caught:
                self.parse("{account: 'github-actions[bot]', credential_renewed: %s}" % bad)
            self.assertIn("YYYY-MM-DD", str(caught.exception))
        # Unquoted, PyYAML itself refuses a day that does not exist; that is a plain refusal too, not a crash.
        for bad in ("2026-13-01", "2026-02-30"):
            with self.assertRaises(config.ConfigError) as caught:
                self.parse("{account: 'github-actions[bot]', credential_renewed: %s}" % bad)
            self.assertIn("not a real day", str(caught.exception))

    def test_passes_must_be_known_levels(self):
        for bad in ("[]", "[none]", "[code, all]", "code", "[high]"):
            with self.assertRaises(config.ConfigError, msg=bad):
                self.parse("{account: 'github-actions[bot]', passes: %s}" % bad)


if __name__ == "__main__":
    unittest.main()
