#!/usr/bin/env python3
"""Sets the independent-review commit status on a pull request's head commit.

    success   no review is needed (factory/risk.py), or the latest review record for the head commit says `pass`
    failure   a review is needed and there is no record for the head commit, or the latest one has open findings
    error     the gate could not decide (an API call failed, or the factory file cannot be used); nothing merges
              on an error, and a re-run clears it

A commit status belongs to the commit, not to one pull request, so every open pull request with the same head commit
is judged and the worst result is the one set: a pull request into a side branch that needs no review cannot turn
the status green for a pull request of the same commit into the main branch that does.

A review record is a pull request comment whose first line is

    <!-- independent-review sha=<40 hex head commit> verdict=pass|findings -->

A new commit changes the head SHA, so an earlier record stops counting; so does a record written before the head
commit existed. Records are never replaced: each review is a new comment, so a send-back keeps its own timestamp.

This is a process control, not proof. When author and reviewer share one login, that login can post the marker
without a review having happened.

SAFETY. Everything is read through the GitHub API as data. The pull request's code is never checked out or run, and
the rules come from the factory file on the DEFAULT BRANCH, so a pull request cannot change the rules that judge it.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python3 -m factory.review_gate --pr 123 [--context NAME] [--dry-run]

`--context` names the status. Use another name (say `independent-review-shadow`) to run beside an existing gate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, risk

API = "https://api.github.com"
CONTEXT = "independent-review"
PASS = "pass"
FINDINGS = "findings"
MAX_FILES = 3000                 # GitHub lists at most this many files of a pull request
DESCRIPTION_LIMIT = 140          # GitHub's limit on a status description
# Only a record from someone who may write to the repository counts. The comment's `author_association` says so for
# most accounts, but a workflow token is shown a member whose organisation membership is private as a mere
# CONTRIBUTOR, so for any other association the commenter's permission is looked up.
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
WRITE_PERMISSIONS = ("admin", "maintain", "write")
ATTEMPTS = 3                     # per API call, for a network fault or a 5xx / rate-limit answer
RETRY_SECONDS = 2
RANK = {"success": 0, "failure": 1, "error": 2}

MARKER = re.compile(r"^<!--\s*independent-review\s+sha=([0-9a-fA-F]{40})\s+verdict=(pass|findings)\s*-->\s*$")
MARKER_ATTEMPT = re.compile(r"^<!--\s*independent-review\b")


def parse_marker(body: str | None) -> tuple[str, str] | None:
    """(sha, verdict) when the comment's first line is a well-formed review marker, else None."""
    first = (body or "").lstrip().split("\n", 1)[0].strip()
    m = MARKER.match(first)
    return (m.group(1).lower(), m.group(2)) if m else None


def is_malformed_marker(body: str | None) -> bool:
    first = (body or "").lstrip().split("\n", 1)[0].strip()
    return bool(MARKER_ATTEMPT.match(first)) and not MARKER.match(first)


def latest_record(comments: list[dict], head_sha: str, head_committed_at: str | None = None) -> dict:
    """What the pull request's comments record about `head_sha`.

    Returns {"verdict": "pass" | "findings" | None, "stale": sha of the newest record for another commit or None,
    "malformed": n, "early": n}. The newest record for the head commit wins, so findings posted after a pass block
    again and a pass posted after findings clears them. A record whose comment was created before the head commit was
    made (`head_committed_at`, ISO 8601 UTC) cannot be a review of it and is not counted. Records from accounts that are not an owner, member or collaborator are ignored (`trust_writers` first vouches for accounts with write access).
    """
    head = head_sha.lower()
    verdict, stale, malformed, early = None, None, 0, 0
    for c in sorted(comments, key=lambda c: (c.get("created_at") or "", c.get("id") or 0)):
        if c.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        if is_malformed_marker(c.get("body")):
            malformed += 1
            continue
        marker = parse_marker(c.get("body"))
        if not marker:
            continue
        if marker[0] != head:
            stale = marker[0]
        elif head_committed_at and (c.get("created_at") or "") < head_committed_at:
            early += 1
        else:
            verdict = marker[1]
    return {"verdict": verdict, "stale": stale, "malformed": malformed, "early": early}


def decide(tier: dict, head_sha: str, comments: list[dict], head_committed_at: str | None = None, standard: str | None = None) -> tuple[str, str]:
    """(state, description) of the commit status."""
    short = head_sha[:7]
    level = tier["review"]
    if level == risk.NONE:
        return "success", "No code changed: no independent review needed"
    areas = ", ".join(tier["areas"]) if level == risk.HIGH else CODE_CHANGE
    record = latest_record(comments, head_sha, head_committed_at)
    if record["verdict"] == PASS:
        return "success", _fit(f"Review record for {short} says pass (a process control, not proof)", areas)
    if record["verdict"] == FINDINGS:
        return "failure", _fit(f"Review of {short} has open findings: fix or answer them, then review again", areas)
    if record["early"]:
        return "failure", _fit(f"The review record is older than commit {short}: review again", areas)
    if record["stale"]:
        return "failure", _fit(f"Review is for {record['stale'][:7]}, head is {short}: review again", areas)
    if record["malformed"]:
        return "failure", _fit("Review marker is malformed" + (f": see {standard}" if standard else ""), areas)
    return "failure", _fit(f"No independent review recorded for {short}", areas)


CODE_CHANGE = "code change, not high risk"


def _fit(text: str, areas: str | None = None) -> str:
    """Keeps a description inside GitHub's limit; the areas are added when there is room."""
    if areas:
        with_areas = f"{text} ({areas})" if areas == CODE_CHANGE else f"{text} (high risk: {areas})"
        if len(with_areas) <= DESCRIPTION_LIMIT:
            return with_areas
    return text if len(text) <= DESCRIPTION_LIMIT else text[:DESCRIPTION_LIMIT - 1] + "…"


def summary(tier: dict, state: str, description: str, head_sha: str, standard: str | None = None) -> str:
    lines = [f"## independent-review: {state}", "", description, "", f"Head commit `{head_sha}`.", ""]
    how = ["", f"The standard, and how to record a review: `{standard}`."] if standard else []
    level = tier["review"]
    if level == risk.HIGH:
        lines += ["| Area | File | Rule that matched |", "| --- | --- | --- |"]
        lines += [f"| {r['area']} | `{r['file']}` | {r['rule'].replace('|', chr(92) + '|')} |" for r in tier["reasons"]]
        lines += how
    elif level == risk.CODE:
        code = tier.get("code", [])
        lines += ["No high-risk rule matched, but the pull request changes code, and every code change is reviewed:", ""]
        lines += [f"- `{name}`" for name in code[:50]] + ([f"- and {len(code) - 50} more"] if len(code) > 50 else [])
        lines += how
    else:
        lines.append(f"No code among the {tier['files']} changed file(s): only paths the review station skips.")
    return "\n".join(lines) + "\n"


# ── GitHub API ─────────────────────────────────────────────────────────────────────────────────────────────────────
class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def _request(self, method: str, path: str, body: dict | None = None, accept: str = "application/vnd.github+json") -> bytes:
        req = urllib.request.Request(
            f"{API}/repos/{self.repo}/{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": accept,
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "review-gate"})
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if attempt == ATTEMPTS or e.code not in (403, 429, 500, 502, 503, 504):
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == ATTEMPTS:
                    raise
            time.sleep(RETRY_SECONDS * attempt)
        raise RuntimeError("unreachable")

    def get(self, path: str) -> dict | list:
        return json.loads(self._request("GET", path))

    def pages(self, path: str, limit: int) -> list:
        out: list = []
        page = 1
        while len(out) < limit:
            batch = self.get(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            out += batch
            if len(batch) < 100:
                break
            page += 1
        return out

    def raw_file(self, path: str, ref: str) -> str:
        """A file's text at a commit, read as data. Raises if it cannot be read: the gate then reports an error."""
        quoted = urllib.parse.quote(path)
        return self._request("GET", f"contents/{quoted}?ref={ref}", accept="application/vnd.github.raw+json").decode("utf-8", "replace")

    def set_status(self, sha: str, state: str, description: str, target_url: str | None, context: str = CONTEXT) -> None:
        body = {"state": state, "description": description, "context": context}
        if target_url:
            body["target_url"] = target_url
        self._request("POST", f"statuses/{sha}", body)


def load_config(gh, default_branch: str) -> dict:
    """The factory file on the default branch. A pull request's own copy is never read."""
    try:
        text = gh.raw_file(config.FILE, default_branch)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise config.ConfigError(f"No {config.FILE} on the default branch") from e
        raise
    return config.parse(text)


def trust_writers(gh, comments: list[dict]) -> list[dict]:
    """Comments that carry (or attempt) a review marker from an account the association does not vouch for: ask the API
    whether that account may write to the repository, once per account, and mark the comment a collaborator's if so.
    A lookup that fails leaves the comment untrusted: the gate then asks for a review, it never passes by accident."""
    cache: dict[str, bool] = {}
    out = []
    for c in comments:
        if c.get("author_association") not in TRUSTED_ASSOCIATIONS and (parse_marker(c.get("body")) or is_malformed_marker(c.get("body"))):
            login = ((c.get("user") or {}).get("login") or "").strip()
            if login and login not in cache:
                try:
                    cache[login] = (gh.get(f"collaborators/{login}/permission") or {}).get("permission") in WRITE_PERMISSIONS
                except Exception:  # noqa: BLE001
                    cache[login] = False
            if cache.get(login):
                c = {**c, "author_association": "COLLABORATOR"}
        out.append(c)
    return out


def evaluate_one(gh: GitHub, pr: dict, cfg: dict) -> dict:
    """The decision for one pull request (the API's pull request object)."""
    number, head_sha = pr["number"], pr["head"]["sha"]
    base = {"pr": number, "base": pr["base"]["ref"], "head_sha": head_sha}
    if cfg["review"] is None:
        tier = {"review": risk.NONE, "files": 0, "areas": [], "reasons": [], "code": []}
        return {**base, "tier": tier, "state": "success",
                "description": f"{config.FILE} has no independent-review station: nothing to check"}
    standard = cfg["review"].get("standard")
    files = gh.pages(f"pulls/{number}/files", MAX_FILES)
    tier = risk.classify(files, cfg)
    if len(files) >= MAX_FILES:
        tier["review"] = risk.HIGH
        tier["reasons"].append({"area": "unreadable diff", "file": "(pull request)", "rule": f"{MAX_FILES} or more changed files",
                                "why": "GitHub does not list them all, so the change is treated as high risk"})
        tier["areas"] = sorted(set(tier["areas"]) | {"unreadable diff"})
    comments, committed_at = [], None
    if tier["review"] != risk.NONE:
        comments = trust_writers(gh, gh.pages(f"issues/{number}/comments", 10_000))
        committed_at = gh.get(f"commits/{head_sha}")["commit"]["committer"]["date"]
    state, description = decide(tier, head_sha, comments, committed_at, standard)
    return {**base, "tier": tier, "state": state, "description": description, "standard": standard}


def evaluate(gh: GitHub, number: int) -> dict:
    """The status for the head commit of pull request `number`: the worst decision among the open pull requests that
    share that head commit. Returns {"open", "head_sha", "state", "description", "results": [one per pull request]}.
    Any failure after the head commit is known becomes the state `error`, so the commit never keeps an earlier success."""
    pr = gh.get(f"pulls/{number}")
    head_sha = pr["head"]["sha"]
    result = {"pr": number, "open": pr.get("state") == "open", "head_sha": head_sha, "results": []}
    try:
        siblings = [p for p in gh.pages(f"commits/{head_sha}/pulls", 1000)
                    if p.get("state") == "open" and p["head"]["sha"] == head_sha and p["number"] != number]
        cfg = load_config(gh, pr["base"]["repo"]["default_branch"])
        result["results"] = [evaluate_one(gh, p, cfg) for p in [pr] + siblings]
    except config.ConfigError as e:
        result.update(state="error", exception=e, description=_fit(f"Gate could not decide: {e}"))
        return result
    except Exception as e:  # noqa: BLE001
        result.update(state="error", exception=e,
                      description=_fit(f"Gate could not decide ({type(e).__name__}): re-run the review-gate job"))
        return result
    worst = max(result["results"], key=lambda r: RANK[r["state"]])
    description = worst["description"]
    if len(result["results"]) > 1 and worst["state"] != "success":
        description = _fit(f"#{worst['pr']} into {worst['base']}: {description}")
    result.update(state=worst["state"], description=description)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Set the independent-review commit status on a pull request's head commit.")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--context", default=CONTEXT, help="name of the status to set (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="print the decision; set nothing")
    args = ap.parse_args(argv)
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    result = evaluate(gh, args.pr)
    if result["state"] == "error":
        text = f"## independent-review: error\n\n{result['description']}\n\n`{result['exception']!r}`\n"
    else:
        several = len(result["results"]) > 1
        text = "\n".join((f"### Pull request #{r['pr']} into `{r['base']}`\n\n" if several else "")
                         + summary(r["tier"], r["state"], r["description"], r["head_sha"], r.get("standard")) for r in result["results"])
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text)
    if args.dry_run:
        print("Dry run: no status set.")
    elif not result["open"]:
        print("The pull request is not open: no status set.")
    else:
        run_url = None
        if os.environ.get("GITHUB_RUN_ID"):
            run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{gh.repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        gh.set_status(result["head_sha"], result["state"], result["description"], run_url, args.context)
        print(f"Set {args.context}={result['state']} on {result['head_sha']}.")
    # The status carries the verdict; the job itself fails only when it could not decide.
    return 1 if result["state"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
