import pathlib
import unittest

from factory import config, risk

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = config.parse("""
spec: factory/0.1
intake:
  - station: testing
    uses: checks
    require: [unit]
  - station: qa
    uses: independent-review
    standard: docs/REVIEW_STANDARD.md
    skip_when_only: [docs/**, "**/*.md", "**/test_*"]
risk:
  - area: payments
    why: moves money
    paths: [api/billing/**]
  - area: deletion
    why: cannot be undone
    paths: [api/retention/**, scripts/delete_*]
""")


def files(*names):
    return [{"filename": n, "status": "modified"} for n in names]


class Glob(unittest.TestCase):
    def test_star_stays_inside_one_segment_and_double_star_crosses(self):
        cases = [("api/*.py", "api/a.py", True), ("api/*.py", "api/x/a.py", False), ("api/**", "api/x/y/a.py", True),
                 ("api/**", "api", False), ("**/*.md", "README.md", True), ("**/*.md", "docs/a/b.md", True),
                 ("**/test_*", "test_a.py", True), ("**/test_*", "api/billing/test_charge.py", True),
                 ("scripts/delete_*", "scripts/delete_site.py", True), ("scripts/delete_*", "scripts/x/delete_site.py", False),
                 ("a?c", "abc", True), ("a?c", "a/c", False), ("a.c", "abc", False)]
        for pattern, path, want in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(bool(risk.compile_glob(pattern).match(path)), want)


class Classify(unittest.TestCase):
    def test_only_skipped_paths_need_no_review(self):
        tier = risk.classify(files("docs/a.md", "README.md", "api/test_x.py"), CFG)
        self.assertEqual((tier["review"], tier["code"], tier["files"]), (risk.NONE, [], 3))

    def test_code_outside_every_risk_area(self):
        tier = risk.classify(files("api/orders.py", "docs/a.md"), CFG)
        self.assertEqual((tier["review"], tier["code"], tier["areas"]), (risk.CODE, ["api/orders.py"], []))

    def test_a_risk_area_makes_it_high_and_says_why(self):
        tier = risk.classify(files("api/billing/charge.py", "scripts/delete_site.py"), CFG)
        self.assertEqual((tier["review"], tier["areas"]), (risk.HIGH, ["deletion", "payments"]))
        self.assertEqual(tier["reasons"][0], {"area": "payments", "file": "api/billing/charge.py", "rule": "api/billing/**", "why": "moves money"})

    def test_a_skipped_path_inside_a_risk_area_does_not_raise_the_level(self):
        self.assertEqual(risk.classify(files("api/billing/test_charge.py"), CFG)["review"], risk.NONE)

    def test_a_rename_is_judged_at_both_ends(self):
        moved = [{"filename": "api/orders/charge.py", "previous_filename": "api/billing/charge.py", "status": "renamed"}]
        tier = risk.classify(moved, CFG)
        self.assertEqual((tier["review"], tier["areas"]), (risk.HIGH, ["payments"]))

    def test_the_review_standard_named_in_the_factory_file_is_always_high_though_docs_are_skipped(self):
        tier = risk.classify(files("docs/REVIEW_STANDARD.md", "docs/other.md"), CFG)
        self.assertEqual((tier["review"], tier["areas"], tier["code"]), (risk.HIGH, [risk.RULES_AREA], ["docs/REVIEW_STANDARD.md"]))
        self.assertEqual(tier["reasons"][0]["rule"], "the review standard")
        no_standard = config.parse("spec: factory/0.1\nintake:\n  - {station: qa, uses: independent-review, skip_when_only: ['docs/**']}\n")
        self.assertEqual(risk.classify(files("docs/REVIEW_STANDARD.md"), no_standard)["review"], risk.NONE)

    def test_the_factory_file_is_always_high_even_when_a_skip_pattern_covers_it(self):
        cfg = config.parse("spec: factory/0.1\nintake:\n  - {station: qa, uses: independent-review, skip_when_only: ['**']}\n")
        self.assertEqual(risk.classify(files("anything.py"), cfg)["review"], risk.NONE)
        tier = risk.classify(files("factory.yml"), cfg)
        self.assertEqual((tier["review"], tier["areas"]), (risk.HIGH, [risk.RULES_AREA]))


class Config(unittest.TestCase):
    def test_the_example_file_parses(self):
        cfg = config.parse((ROOT / "examples" / "factory.yml").read_text())
        self.assertEqual(cfg["review"]["standard"], "docs/REVIEW_STANDARD.md")
        self.assertEqual([a["area"] for a in cfg["risk"]], ["payments", "sign-in", "deletion"])

    def test_the_repositorys_own_file_parses(self):
        self.assertIsNotNone(config.parse((ROOT / "factory.yml").read_text())["review"])

    def test_no_review_station_is_allowed(self):
        self.assertIsNone(config.parse("spec: factory/0.1\n")["review"])

    def test_what_is_refused(self):
        for text, said in [
            ("- a\n- b\n", "must be a mapping"),
            ("spec: other/1\n", "spec: factory/0.1"),
            ("spec: factory/0.1\nintake: {}\n", "intake must be a list"),
            ("spec: factory/0.1\nintake: [{station: qa}]\n", "`station` and `uses`"),
            ("spec: factory/0.1\nintake:\n  - {station: a, uses: independent-review}\n  - {station: b, uses: independent-review}\n", "more than one"),
            ("spec: factory/0.1\nintake: [{station: qa, uses: independent-review, skip_when_only: docs}]\n", "list of paths"),
            ("spec: factory/0.1\nrisk: [{area: x, paths: [a]}]\n", "needs `why`"),
            ("spec: factory/0.1\nrisk: [{area: x, why: y}]\n", "has no paths"),
            ("spec: [unclosed\n", "not valid YAML"),
        ]:
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError) as caught:
                    config.parse(text)
                self.assertIn(said, str(caught.exception))


class NeverSkip(unittest.TestCase):
    RULES = """
spec: factory/0.1
intake:
  - station: qa
    uses: independent-review
    standard: docs/REVIEW_STANDARD.md
    skip_when_only: ["**/*.md", docs/**]
    never_skip: ["**/AGENTS.md", CLAUDE.md, docs/runbooks/**]
risk:
  - area: operations
    why: tells people what to do in an incident
    paths: [docs/runbooks/**]
"""

    def level(self, *names, rules=None, renamed_from=None):
        files = [{"filename": n} for n in names]
        if renamed_from:
            files[0]["previous_filename"] = renamed_from
        return risk.classify(files, config.parse(rules or self.RULES))

    def test_a_named_file_is_reviewed_though_it_matches_the_skip_list(self):
        for name in ("AGENTS.md", "api/AGENTS.md", "CLAUDE.md"):
            tier = self.level(name)
            self.assertEqual((tier["review"], tier["code"], tier["areas"]), ("code", [name], []), name)

    def test_other_prose_is_still_skipped(self):
        for name in ("README.md", "docs/guide.md", "api/CLAUDE.md", "AGENTS.md.bak.md"):
            self.assertEqual(self.level(name)["review"], "none", name)
        self.assertEqual(self.level("README.md", "AGENTS.md")["code"], ["AGENTS.md"])

    def test_inside_a_risk_area_it_is_high(self):
        tier = self.level("docs/runbooks/outage.md")
        self.assertEqual((tier["review"], tier["areas"]), ("high", ["operations"]))

    def test_a_rename_is_judged_at_both_ends(self):
        self.assertEqual(self.level("docs/notes.md", renamed_from="AGENTS.md")["code"], ["AGENTS.md"])

    def test_a_line_break_in_a_name_does_not_take_it_out_of_a_pattern(self):
        tier = self.level("docs/runbooks/a\nb.md")
        self.assertEqual((tier["review"], tier["areas"]), ("high", ["operations"]))
        self.assertEqual(self.level("docs/runbooks/sub\ndir/x.md")["review"], "high")
        for pattern, name in (("api/billing/**", "api/billing/evil\n.py"), ("**/AGENTS.md", "a\nb/AGENTS.md"), ("**", "a\nb"), ("a**", "a\r\nb/c")):
            self.assertEqual(risk.first_match(name, [pattern]), pattern, (pattern, name))
        self.assertIsNone(risk.first_match("a\n/b", ["*"]))                  # `*` still stays inside one segment

    def test_without_the_key_nothing_changes(self):
        without = self.RULES.replace('    never_skip: ["**/AGENTS.md", CLAUDE.md, docs/runbooks/**]\n', "")
        self.assertEqual(config.parse(without)["review"]["never_skip"], [])
        for name in ("AGENTS.md", "docs/runbooks/outage.md"):
            self.assertEqual(self.level(name, rules=without)["review"], "none", name)

    def test_it_never_lowers_a_level(self):
        # The factory file and the standard stay high; code stays code.
        self.assertEqual(self.level("factory.yml")["review"], "high")
        self.assertEqual(self.level("docs/REVIEW_STANDARD.md")["review"], "high")
        self.assertEqual(self.level("api/x.py")["review"], "code")

    def test_it_must_be_a_list_of_paths(self):
        for bad in ("AGENTS.md", "[1]", "['']", "{a: b}"):
            with self.assertRaises(config.ConfigError, msg=bad) as caught:
                config.parse("spec: factory/0.1\nintake:\n  - {station: qa, uses: independent-review, never_skip: %s}\n" % bad)
            self.assertIn("qa.never_skip", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
