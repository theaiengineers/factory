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


if __name__ == "__main__":
    unittest.main()
