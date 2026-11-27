import contextlib
import io
import json
import os
import tempfile
import unittest

from factory import config, preview

RULES = """
spec: factory/0.1
intake:
  - station: qa
    uses: independent-review
    standard: docs/REVIEW_STANDARD.md
    skip_when_only: ["**/*.md"]
risk:
  - area: payments
    why: moves money
    paths: [api/billing/**]
  - area: sign-in
    why: lets people in
    paths: [api/auth/**]
"""


def pr(number, *paths, title="A change"):
    return {"number": number, "title": title, "files": [{"path": p} for p in paths]}


class Rows(unittest.TestCase):
    def setUp(self):
        self.cfg = config.parse(RULES)

    def test_each_level(self):
        found = preview.rows([pr(1, "README.md"), pr(2, "api/util.py"), pr(3, "api/billing/charge.py", "README.md")], self.cfg)
        self.assertEqual([r["level"] for r in found], ["none", "code", "high"])
        self.assertEqual(found[2]["areas"], ["payments"])

    def test_the_factory_file_is_high(self):
        self.assertEqual(preview.rows([pr(1, "factory.yml")], self.cfg)[0]["level"], "high")

    def test_accepts_the_api_spelling_of_a_file(self):
        found = preview.rows([{"number": 4, "title": "t", "files": [{"filename": "api/auth/login.py"}]}], self.cfg)
        self.assertEqual(found[0]["areas"], ["sign-in"])

    def test_a_change_with_no_files_listed_is_none_and_says_so(self):
        found = preview.rows([{"number": 5, "title": "t"}], self.cfg)
        self.assertEqual((found[0]["level"], found[0]["files"]), ("none", 0))
        self.assertIn("(no files listed)", preview.render(found, self.cfg))


class Render(unittest.TestCase):
    def setUp(self):
        self.cfg = config.parse(RULES)

    def test_counts_and_quiet_areas(self):
        text = preview.render(preview.rows([pr(1, "README.md"), pr(2, "api/billing/x.py")], self.cfg), self.cfg)
        self.assertIn("2 change(s): 1 need no review, 0 need a review, 1 are high risk.", text)
        self.assertIn("#2  high", text)
        self.assertIn("[payments]", text)
        self.assertIn("no change here touched", text)
        self.assertIn("sign-in.", text)
        self.assertNotIn("payments.", text)

    def test_no_quiet_area_line_when_every_area_was_touched(self):
        text = preview.render(preview.rows([pr(1, "api/billing/x.py", "api/auth/y.py")], self.cfg), self.cfg)
        self.assertNotIn("no change here touched", text)

    def test_says_when_there_is_no_review_station(self):
        cfg = config.parse("spec: factory/0.1\n")
        text = preview.render(preview.rows([pr(1, "README.md")], cfg), cfg)
        self.assertIn("#1  code", text)
        self.assertIn("no independent-review station", text)


class Main(unittest.TestCase):
    def run_main(self, rules, stdin):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "factory.yml")
            if rules is not None:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(rules)
            out, err = io.StringIO(), io.StringIO()
            real = preview.sys.stdin
            preview.sys.stdin = io.StringIO(stdin)
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = preview.main(["--rules", path])
            finally:
                preview.sys.stdin = real
            return code, out.getvalue(), err.getvalue()

    def test_prints_the_levels(self):
        code, out, err = self.run_main(RULES, json.dumps([pr(7, "api/auth/a.py", title="Sign-in fix")]))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("#7  high  Sign-in fix  [sign-in]", out)

    def test_a_missing_rules_file(self):
        code, out, err = self.run_main(None, "[]")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Cannot read", err)

    def test_a_rules_file_that_cannot_be_used(self):
        code, out, err = self.run_main("spec: other\n", "[]")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("factory/0.1", err)

    def test_input_that_is_not_the_list(self):
        for stdin in ("", "{}", "[1]", "not json"):
            code, out, err = self.run_main(RULES, stdin)
            self.assertEqual((code, out), (2, ""), stdin)
            self.assertIn("gh pr list", err)


if __name__ == "__main__":
    unittest.main()
