"""How much care a change needs, from the paths it touches and the building's factory file.

    none   only paths the review station skips (prose, tests and the like): no review
    code   something else changed, outside every risk area: a review
    high   a risk area was touched, or the factory file itself: a review, and release gates hold

A path the review station skips never raises the level, even inside a risk area: a test beside sensitive code is
still a test. There are two exceptions, both always high whatever the skip paths say: the factory file, which holds
the rules that judge every change, and the review standard the factory file names, which is what reviewers follow.

The review station can also name paths that are never skipped (`never_skip`). Such a path is judged as if the skip
list did not mention it: it needs a review, and it is high risk if it is inside a risk area. It is for prose that
steers the work, such as the file that tells agents how to work in the repository.
"""
from __future__ import annotations

import functools
import re

from . import config

NONE, CODE, HIGH = "none", "code", "high"
RULES_AREA = "factory rules"
RULES_WHY = "changes the rules that judge every change"
STANDARD_WHY = "is the standard reviewers follow; a change here can weaken every review"


@functools.lru_cache(maxsize=None)
def compile_glob(pattern: str) -> re.Pattern:
    """`*` and `?` stay inside one path segment; `**` crosses segments; `**/` also matches no directory at all."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def first_match(path: str, patterns: list[str]) -> str | None:
    return next((p for p in patterns if compile_glob(p).match(path)), None)


def classify(files: list[dict], cfg: dict) -> dict:
    """`files` are GitHub's pull request file objects (`filename`, and `previous_filename` for a rename).

    Returns {"review": none|code|high, "files": n, "areas": [...], "reasons": [{area, file, rule, why}], "code": [paths]}.
    """
    skip = (cfg.get("review") or {}).get("skip_when_only", [])
    keep = (cfg.get("review") or {}).get("never_skip", [])
    standard = (cfg.get("review") or {}).get("standard")
    reasons, code = [], []
    for f in files:
        # A rename is judged at both ends: moving a file out of a risk area changes that area.
        for name in dict.fromkeys(n for n in (f.get("filename"), f.get("previous_filename")) if n):
            if name == config.FILE:
                code.append(name)
                reasons.append({"area": RULES_AREA, "file": name, "rule": config.FILE, "why": RULES_WHY})
                continue
            if standard and name == standard:
                code.append(name)
                reasons.append({"area": RULES_AREA, "file": name, "rule": "the review standard", "why": STANDARD_WHY})
                continue
            if first_match(name, skip) and not first_match(name, keep):
                continue
            code.append(name)
            for area in cfg.get("risk", []):
                rule = first_match(name, area["paths"])
                if rule:
                    reasons.append({"area": area["area"], "file": name, "rule": rule, "why": area["why"]})
    level = HIGH if reasons else CODE if code else NONE
    return {"review": level, "files": len(files), "areas": sorted({r["area"] for r in reasons}),
            "reasons": reasons, "code": list(dict.fromkeys(code))}
