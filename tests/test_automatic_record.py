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

    def test_an_allowed_automatic_pass_clears_findings_like_any_newer_record(self):
        self.assertEqual(review_gate.decide(CODE, HEAD, [person("findings", n=1), auto(n=2)], automatic=CODE_ONLY)[0], "success")

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


def posted(body, login, kind, association="NONE", n=1):
    return {**note(body, n, association), "user": {"login": login, "type": kind}}


class Trust(unittest.TestCase):
    def test_the_named_bot_is_marked_without_a_lookup(self):
        gh = Lookups()
        out = review_gate.trust_writers(gh, [posted(marker(), BOT, "Bot")], CODE_ONLY)
        self.assertEqual((out[0]["author_association"], gh.asked), (review_gate.AUTOMATIC, []))

    def test_the_bot_is_not_trusted_when_the_factory_file_names_none(self):
        gh = Lookups()
        out = review_gate.trust_writers(gh, [posted(marker(), BOT, "Bot")], None)
        self.assertEqual(out[0]["author_association"], review_gate.UNTRUSTED)

    def test_another_bot_is_not_the_automatic_reviewer(self):
        out = review_gate.trust_writers(Lookups(), [posted(marker(), "other-app[bot]", "Bot")], CODE_ONLY)
        self.assertEqual(out[0]["author_association"], review_gate.UNTRUSTED)

    def test_the_login_alone_is_not_enough(self):
        # GitHub must also say the account is a bot.
        for kind in ("User", None, "Organization"):
            out = review_gate.trust_writers(Lookups(), [posted(marker(), BOT, kind)], CODE_ONLY)
            self.assertEqual(out[0]["author_association"], review_gate.UNTRUSTED, kind)

    def test_people_are_still_looked_up(self):
        gh = Lookups(writers={"alice"})
        out = review_gate.trust_writers(gh, [posted(marker(), "alice", "User", n=1), posted(marker(), "mallory", "User", n=2)], CODE_ONLY)
        self.assertEqual([c["author_association"] for c in out], ["COLLABORATOR", review_gate.UNTRUSTED])

    def test_a_bot_comment_without_a_marker_is_left_alone(self):
        out = review_gate.trust_writers(Lookups(), [posted("Tests passed.", BOT, "Bot")], CODE_ONLY)
        self.assertEqual(out[0]["author_association"], "NONE")


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
        for bad in ("{account: alice}", "{account: '[bot]'}", "{account: 7}", "{}", "github-actions[bot]", "[a]"):
            with self.assertRaises(config.ConfigError, msg=bad):
                self.parse(bad)

    def test_passes_must_be_known_levels(self):
        for bad in ("[]", "[none]", "[code, all]", "code"):
            with self.assertRaises(config.ConfigError, msg=bad):
                self.parse("{account: 'github-actions[bot]', passes: %s}" % bad)


if __name__ == "__main__":
    unittest.main()
