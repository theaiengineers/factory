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
import hashlib
import hmac
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
# Only a record from someone who may write to the repository counts. The comment's `author_association` does not
# say so: MEMBER is anyone in the organisation, including a member who can only read this repository, and a member
# whose membership is private is shown to a workflow token as a mere CONTRIBUTOR. So `trust_writers` looks up the
# permission of every account that posts a marker and rewrites the association to what that lookup found. Only an
# OWNER is taken at its word. `latest_record` then counts the associations below.
TRUSTED_ASSOCIATIONS = ("OWNER", "COLLABORATOR")
UNTRUSTED = "NONE"
# A record from the automatic reviewer the factory file names (review station, `automatic.account`). `trust_writers`
# gives it this association. It counts like a writer's, except that its pass counts only for the levels the factory
# file lists under `automatic.passes`; its findings always count.
#
# The account alone proves nothing: `github-actions[bot]` is the login of every workflow in a repository, and a
# workflow that echoes text from a pull request would post a forged marker from it. So an automatic record counts
# only when its second line carries a proof: an HMAC, keyed with a secret only the repository holds
# (FACTORY_REVIEWER_KEY), over the repository, the pull request number, the commit and the verdict. A proof cannot
# be made without the key and cannot be moved to another pull request, commit or verdict.
AUTOMATIC = "AUTOMATIC"
PROOF = re.compile(r"^<!--\s*factory-reviewer-proof\s+v1\s+([0-9a-f]{64})\s*-->\s*$")
KEY_MIN = 32                     # characters; a shorter key is treated as no key
RECHECKS = 3                     # times the commit is judged again when the answer changes while the gate runs
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


def usable_key(key: str | None) -> bool:
    return isinstance(key, str) and len(key) >= KEY_MIN


def proof(key: str, repo: str, number: int, sha: str, verdict: str) -> str:
    message = f"v1\n{repo.lower()}\n{int(number)}\n{sha.lower()}\n{verdict}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def proof_line(key: str, repo: str, number: int, sha: str, verdict: str) -> str:
    return f"<!-- factory-reviewer-proof v1 {proof(key, repo, number, sha, verdict)} -->"


def is_proven(key: str | None, repo: str, number: int, body: str | None) -> bool:
    """True when the comment is a well-formed record whose second line is the proof for exactly this repository,
    pull request, commit and verdict. Without a usable key nothing is proven."""
    marker = parse_marker(body)
    if not marker or not usable_key(key) or not repo:
        return False
    lines = (body or "").lstrip().split("\n")
    found = PROOF.match(lines[1].strip()) if len(lines) > 1 else None
    return bool(found) and hmac.compare_digest(found.group(1), proof(key, repo, number, marker[0], marker[1]))


def is_malformed_marker(body: str | None) -> bool:
    first = (body or "").lstrip().split("\n", 1)[0].strip()
    return bool(MARKER_ATTEMPT.match(first)) and not MARKER.match(first)


def latest_record(comments: list[dict], head_sha: str, head_committed_at: str | None = None,
                  automatic_passes: bool = False) -> dict:
    """What the pull request's comments record about `head_sha`.

    Returns {"verdict": "pass" | "findings" | None, "stale": sha of the newest record for another commit or None,
    "malformed": n, "early": n, "held": n}. `held` counts passes from the automatic reviewer that were not counted
    because `automatic_passes` is false: they neither pass the commit nor clear earlier findings.

    Two more rules for automatic records, because a signed record is public once posted and anything that can
    comment from the reviewer's account can post the same text again. An automatic verdict for a commit counts
    once: a later record carrying the same verdict is a repeat and is ignored. And an automatic pass never clears
    findings a person recorded for the same commit; only a person's pass does. The newest record for the head commit wins, so findings posted after a pass block
    again and a pass posted after findings clears them. A record whose comment was created before the head commit was
    made (`head_committed_at`, ISO 8601 UTC) cannot be a review of it and is not counted. Only records whose
    association is in TRUSTED_ASSOCIATIONS count; pass the comments through `trust_writers` first, which sets it from
    the account's real permission.
    """
    head = head_sha.lower()
    verdict, stale, malformed, early, held = None, None, 0, 0, 0
    unanswered, counted = False, set()        # unanswered: a person's findings that no person has since passed
    for c in sorted(comments, key=lambda c: (c.get("created_at") or "", c.get("id") or 0)):
        automatic = c.get("author_association") == AUTOMATIC
        if not automatic and c.get("author_association") not in TRUSTED_ASSOCIATIONS:
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
        elif automatic and marker[1] in counted:
            continue
        elif automatic and marker[1] == PASS and not automatic_passes:
            counted.add(PASS)
            held += 1
        elif automatic and marker[1] == PASS and unanswered:
            counted.add(PASS)
        else:
            if automatic:
                counted.add(marker[1])
            else:
                unanswered = marker[1] == FINDINGS
            verdict = marker[1]
    return {"verdict": verdict, "stale": stale, "malformed": malformed, "early": early, "held": held}


def decide(tier: dict, head_sha: str, comments: list[dict], head_committed_at: str | None = None, standard: str | None = None,
           automatic: dict | None = None) -> tuple[str, str]:
    """(state, description) of the commit status. `automatic` is the review station's automatic reviewer, if any."""
    short = head_sha[:7]
    level = tier["review"]
    if level == risk.NONE:
        return "success", "No code changed: no independent review needed"
    areas = ", ".join(tier["areas"]) if level == risk.HIGH else CODE_CHANGE
    record = latest_record(comments, head_sha, head_committed_at, bool(automatic) and level in automatic["passes"])
    if record["verdict"] == PASS:
        return "success", _fit(f"Review record for {short} says pass (a process control, not proof)", areas)
    if record["verdict"] == FINDINGS:
        return "failure", _fit(f"Review of {short} has open findings: fix or answer them, then review again", areas)
    if record["held"]:
        return "failure", _fit(f"Automatic review passed {short}; this change also needs a person's review", areas)
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
    def __init__(self, repo: str, token: str, reviewer_key: str = ""):
        self.repo, self.token, self.reviewer_key = repo, token, reviewer_key

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


def trust_writers(gh, comments: list[dict], automatic: dict | None = None, proven=None) -> list[dict]:
    """Marks a record from the factory file's automatic reviewer (`automatic`, or None) as AUTOMATIC when three
    things hold: the comment's login is that account, GitHub says the account is a bot, and `proven(comment)` is
    true (the record carries the reviewer's proof; see AUTOMATIC above). With no `proven` nothing is marked. A
    comment from that account that fails any of these is judged like any other account's, and so does not count.

    Sets the association of every comment that carries (or attempts) a review marker from what the account may
    actually do: COLLABORATOR when it can write to the repository, otherwise untrusted. The permission is asked for
    once per account. An OWNER is not looked up.

    An account GitHub does not know (404) cannot write, so its comment is untrusted. Any other failed lookup RAISES,
    and the gate then says `error`: dropping a record the gate could not check would let an earlier pass stand over
    later findings."""
    cache: dict[str, bool] = {}
    out = []
    for c in comments:
        marked = parse_marker(c.get("body")) or is_malformed_marker(c.get("body"))
        user = c.get("user") or {}
        if (marked and automatic and proven and user.get("login") == automatic["account"] and user.get("type") == "Bot"
                and proven(c)):
            c = {**c, "author_association": AUTOMATIC}
        elif c.get("author_association") != "OWNER" and marked:
            login = (user.get("login") or "").strip()
            if login and login not in cache:
                try:
                    answer = gh.get(f"collaborators/{urllib.parse.quote(login, safe='')}/permission")
                    cache[login] = (answer or {}).get("permission") in WRITE_PERMISSIONS
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        raise
                    cache[login] = False
            c = {**c, "author_association": "COLLABORATOR" if cache.get(login) else UNTRUSTED}
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
    standard, automatic = cfg["review"].get("standard"), cfg["review"].get("automatic")
    files = gh.pages(f"pulls/{number}/files", MAX_FILES)
    tier = risk.classify(files, cfg)
    if len(files) >= MAX_FILES:
        tier["review"] = risk.HIGH
        tier["reasons"].append({"area": "unreadable diff", "file": "(pull request)", "rule": f"{MAX_FILES} or more changed files",
                                "why": "GitHub does not list them all, so the change is treated as high risk"})
        tier["areas"] = sorted(set(tier["areas"]) | {"unreadable diff"})
    comments, committed_at = [], None
    if tier["review"] != risk.NONE:
        key, repo = getattr(gh, "reviewer_key", ""), getattr(gh, "repo", "")
        proven = (lambda c: is_proven(key, repo, number, c.get("body"))) if automatic else None
        comments = trust_writers(gh, gh.pages(f"issues/{number}/comments", 10_000), automatic, proven)
        committed_at = gh.get(f"commits/{head_sha}")["commit"]["committer"]["date"]
    state, description = decide(tier, head_sha, comments, committed_at, standard, automatic)
    return {**base, "tier": tier, "state": state, "description": description, "standard": standard}


def evaluate(gh: GitHub, number: int, sha: str | None = None) -> dict:
    """The status for one commit: the worst decision among the open pull requests whose head commit it is.

    The commit is `sha`, or the current head of pull request `number` when `sha` is not given. Every open pull
    request GitHub lists on the commit is judged. The pull request asked about is added when it is open and on the
    commit but missing from the listing, which is always so for a pull request from a fork.

    Returns {"pr", "open", "head_sha", "state", "description", "results": [one per pull request judged]}. `state` is
    None when no open pull request is on the commit: there is nothing to judge and nothing should be written.
    Reading the pull request itself can raise. Any failure after that becomes the state `error`, so the commit
    never keeps an earlier success."""
    pr = gh.get(f"pulls/{number}")
    head_sha = sha or pr["head"]["sha"]
    result = {"pr": number, "open": pr.get("state") == "open", "head_sha": head_sha, "results": [], "state": None, "description": ""}
    try:
        subjects = [p for p in gh.pages(f"commits/{head_sha}/pulls", 1000) if p.get("state") == "open" and p["head"]["sha"] == head_sha]
        if result["open"] and pr["head"]["sha"] == head_sha and number not in [p["number"] for p in subjects]:
            subjects.insert(0, pr)
        if not subjects:
            return result
        cfg = load_config(gh, pr["base"]["repo"]["default_branch"])
        result["results"] = [evaluate_one(gh, p, cfg) for p in subjects]
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


def settle(gh, number: int, sha: str, args) -> int:
    """Judges commit `sha`, writes its status, then judges it again, until what was written is what is found.

    Runs for different pull requests are not serialised, and several pull requests can share a head commit. A run
    that judged the commit before a second pull request was opened on it, or before findings were recorded, could
    otherwise write its `success` after another run's `failure`. So every write is followed by another look:
    whichever run writes last also looks last, and corrects the status if the answer is no longer the one it wrote.

    The commit is fixed for the whole of this function. Pull requests may leave it (a new push, a merge, a close)
    while it runs; each look judges whichever open pull requests are on it at that moment. When none is left the
    commit is no pull request's head, nothing more is written, and what is there is left.

    Once this function has written, it never leaves a status it could not confirm: if it cannot look again, or the
    answer keeps changing, it writes `error`. Returns the job's exit code."""
    wrote = False
    try:
        for attempt in range(1, RECHECKS + 1):
            result = evaluate(gh, number, sha)
            if result["state"] is None:
                print(f"No open pull request is on {sha[:7]}: no status set.")
                return 0
            code = report(gh, result, args)
            if args.dry_run:
                return code
            wrote = True
            if result["state"] == "error":
                return code
            check = evaluate(gh, number, sha)
            if check["state"] is None or all(check[k] == result[k] for k in ("state", "description")):
                return code
            print(f"The answer for {sha[:7]} changed while it was judged: judging again ({attempt}/{RECHECKS}).")
        why = "the answer kept changing while it ran"
    except Exception as e:  # noqa: BLE001
        if not wrote:
            raise                    # no status of this run's is on the commit: the job fails
        why = f"it could not judge again ({type(e).__name__})"
        print(f"Could not judge {sha[:7]} again: {e!r}")
    gh.set_status(sha, "error", _fit(f"Gate could not decide: {why}; re-run the review-gate job"), None, args.context)
    print(f"Set {args.context}=error on {sha}.")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Set the independent-review commit status on a pull request's head commit.")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--context", default=CONTEXT, help="name of the status to set (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="print the decision; set nothing")
    args = ap.parse_args(argv)
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"], os.environ.get("FACTORY_REVIEWER_KEY", ""))
    # Settle the pull request's head commit. If the pull request has a new head commit by the time that is done,
    # settle that one as well. The commit it left was settled for whichever pull requests stayed on it.
    code, settled = 0, []
    for _ in range(RECHECKS):
        try:
            sha = gh.get(f"pulls/{args.pr}")["head"]["sha"]
        except Exception:  # noqa: BLE001
            if not settled:
                raise                # nothing was judged: the job fails
            print("Could not read the pull request again to see whether it has a new head commit.")
            return 1
        if sha in settled:
            break
        settled.append(sha)
        code = max(code, settle(gh, args.pr, sha, args))
        if args.dry_run:
            break
    return code


def report(gh, result: dict, args) -> int:
    """Prints the decision, writes the job summary and sets the status. Returns the job's exit code."""
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
