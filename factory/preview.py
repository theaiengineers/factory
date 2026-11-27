"""Tries a factory file against changes that have already merged, before the gate is switched on.

    gh pr list --state merged --limit 30 --json number,title,files | python3 -m factory.preview --rules factory.yml

It reads the list on standard input and the rules from a local file, and prints the level each change would have
been given. It calls nothing and changes nothing.

What it cannot see: `gh` lists at most the first 100 files of a pull request and gives a renamed file only its new
name, so a very large change or a file moved out of a risk area can be given a lower level here than the gate gives.
A change listed with that many files is marked.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import config, risk

LEVELS = (risk.NONE, risk.CODE, risk.HIGH)
LISTED_AT_MOST = 100


def rows(prs: list[dict], cfg: dict) -> list[dict]:
    """One row for each pull request: {number, title, level, areas, files}."""
    out = []
    for pr in prs:
        listed = pr.get("files") or []
        if not isinstance(listed, list) or not all(isinstance(f, dict) for f in listed):
            raise ValueError("files must be a list of objects")
        names = [f.get("path") or f.get("filename") for f in listed]
        if not all(isinstance(n, str) for n in names if n):
            raise ValueError("a file's path must be text")
        files = [{"filename": n} for n in names if n]
        if cfg.get("review") is None:
            # The gate checks nothing without an independent-review station (review_gate.evaluate_one).
            tier = {"review": risk.NONE, "areas": []}
        else:
            tier = risk.classify(files, cfg)
        out.append({"number": pr.get("number"), "title": str(pr.get("title") or ""), "level": tier["review"],
                    "areas": tier["areas"], "files": len(files)})
    return out


def unused_areas(found: list[dict], cfg: dict) -> list[str]:
    """Risk areas that no change in the sample touched: a quiet area, or a path that is spelt wrong."""
    touched = {a for r in found for a in r["areas"]}
    return [a["area"] for a in cfg.get("risk", []) if a["area"] not in touched]


def render(found: list[dict], cfg: dict) -> str:
    lines = []
    for r in found:
        areas = f"  [{', '.join(r['areas'])}]" if r["areas"] else ""
        empty = "  (no files listed)" if not r["files"] else ""
        if r["files"] >= LISTED_AT_MOST:
            empty = f"  ({r['files']} files listed: there may be more, and the gate reads them all)"
        lines.append(f"#{r['number']}  {r['level']:<4}  {r['title'][:70]}{areas}{empty}")
    counts = {level: sum(1 for r in found if r["level"] == level) for level in LEVELS}
    lines += ["", f"{len(found)} change(s): {counts[risk.NONE]} need no review, {counts[risk.CODE]} need a review, "
                  f"{counts[risk.HIGH]} are high risk."]
    if not cfg.get("review"):
        lines.append("There is no independent-review station in intake, so the gate checks nothing and passes every "
                     "change. Add one (`uses: independent-review`) for these rules to have any effect.")
        return "\n".join(lines) + "\n"
    if not found:
        lines.append("No merged pull request was listed, so the rules were tried against nothing. Check the list "
                     "was made in the right repository.")
    quiet = unused_areas(found, cfg)
    if found and quiet:
        lines.append("Risk areas no change here touched (check the paths are spelt as they are in the repository): "
                     + ", ".join(quiet) + ".")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the level each merged pull request would have been given.")
    parser.add_argument("--rules", default=config.FILE, help="the factory file to try (default: factory.yml)")
    args = parser.parse_args(argv)
    try:
        with open(args.rules, encoding="utf-8") as f:
            cfg = config.parse(f.read())
    except OSError as e:
        print(f"Cannot read {args.rules}: {e.strerror}", file=sys.stderr)
        return 2
    except UnicodeDecodeError:
        print(f"Cannot read {args.rules}: it is not UTF-8 text", file=sys.stderr)
        return 2
    except config.ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        prs = json.load(sys.stdin)
    except json.JSONDecodeError:
        prs = None
    try:
        if not isinstance(prs, list) or not all(isinstance(p, dict) for p in prs):
            raise ValueError("not a list of pull requests")
        found = rows(prs, cfg)
    except ValueError:
        print("Standard input must be the JSON list from `gh pr list --json number,title,files`.", file=sys.stderr)
        return 2
    sys.stdout.write(render(found, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
