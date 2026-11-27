"""Watches the automatic reviewer's model credential, so that it does not stop working unnoticed.

A subscription token lasts a year, a key can be withdrawn, and a subscription can end. When that happens the
reviewer says so on the next pull request (reviewer.py, the `credential-refused` notice), but a quiet repository may
not see a pull request for weeks. This is run on a schedule instead (.github/workflows/reviewer-health.yml):

    python3 -m factory.reviewer_health check  --out result.json       # holds the model's credential, can only read
    python3 -m factory.reviewer_health report --result result.json    # can open issues, never holds the credential

`check` makes one very small model call that carries nothing from the repository. `report` opens an issue when
something needs a person, does not open a second one while the first is open, and closes its own issue when the
problem has gone. Every word of an issue is fixed here: nothing the model or the repository says reaches one.

It can also count down to a renewal. If the factory file gives `automatic.credential_renewed: YYYY-MM-DD`, an issue is
opened once that date is RENEW_AFTER_DAYS old. Nothing can read a secret's age, so the date is only as good as the
person who writes it down.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error

from . import config, review_gate, reviewer

WORKFLOW_ACCOUNT = "github-actions[bot]"     # the account a workflow's own token opens issues from
RENEW_AFTER_DAYS = 330                       # a subscription token lasts 365 days: about five weeks' warning
RETRY_SECONDS = 30
PING_SECONDS = 120
PING_SYSTEM = "Reply with the single word OK."
PING_ASK = "Answer as your instructions say."
PING = "This is a check that the connection works."
RAW = "application/vnd.github.raw+json"
GUIDE = "https://github.com/theaiengineers/factory/blob/main/docs/AUTOMATIC_REVIEWER.md#when-the-credential-stops-working"

OFF, WORKING, MISSING, REFUSED, FAILED, DUE = "off", "working", "missing", "refused", "failed", "renewal-due"
STATES = (OFF, WORKING, MISSING, REFUSED, FAILED)
MARK = "<!-- factory-reviewer-health: %s -->"
FOOT = (f"\n\nHow to replace or renew the credential: {GUIDE}\n\nThis issue was opened by the factory's reviewer health "
        "check. It closes itself when a later check finds the problem gone.\n")
# kind: (title, what to say). In the order they are reported.
PROBLEMS = {
    REFUSED: ("The automatic reviewer's model credential has stopped working",
              "The model's provider refused the credential the automatic reviewer uses. It has expired, been "
              "withdrawn, or the subscription or account behind it can no longer be used.\n\n"
              "Until it is replaced, no change is reviewed automatically: each one needs a person's review. Nothing "
              "merges unreviewed because of this.\n\n"
              "Someone who administers this repository needs to create a new credential and set the secret again."),
    MISSING: ("The automatic reviewer has no model credential",
              "The factory file names an automatic reviewer, but this repository has neither of the secrets it can "
              "use (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`), or the workflow does not pass one on.\n\n"
              "Until one is set, no change is reviewed automatically: each one needs a person's review. If the "
              "automatic reviewer is no longer wanted, remove `automatic` from the factory file and this check stops."),
    FAILED: ("The automatic reviewer could not reach its model",
             "The health check tried twice to get an answer from the model and got none. The credential was not "
             "refused, so this may be a passing fault at the provider, a limit that has been reached, or a fault in "
             "the check itself. The run's log says which.\n\n"
             "If pull requests are still being reviewed, nothing needs doing. If they are not, read the reviewer "
             "workflow's latest run."),
    DUE: ("The automatic reviewer's model credential is due for renewal",
          "The factory file says the credential was last renewed on a date that is now "
          f"{RENEW_AFTER_DAYS} days or more ago (or is not a past date). A Claude subscription token lasts a year, "
          "so it is close to expiring.\n\n"
          "Create a new one, set the secret again, and change `automatic.credential_renewed` in the factory file to "
          "the day you did it. The reviewer keeps working until the old one actually expires."),
}
RESOLVED = "A later health check found this problem gone, so this issue is closed."
STOPPED = "The factory file no longer names an automatic reviewer, so there is nothing to watch and this issue is closed."
DAYS_AHEAD = 1                               # a day written in a time zone ahead of UTC is not "in the future"


def read_rules(gh) -> dict:
    """The factory file on the default branch (the contents API reads that branch when no other is named)."""
    return config.parse(gh._request("GET", f"contents/{config.FILE}", accept=RAW).decode("utf-8", "replace"))


def check(gh, environ, model: str | None = None, call=reviewer.call_claude, pause=time.sleep) -> dict:
    """The check step: {"state", "renewed", "why"}. It never raises for an ordinary failure."""
    try:
        cfg = read_rules(gh)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        return {"state": OFF, "renewed": None, "why": f"there is no {config.FILE} on the default branch"}
    except config.ConfigError as e:
        # Not "off": a file that cannot be read must not quieten this check. The gate fails on the same file.
        return {"state": FAILED, "renewed": None, "why": f"the factory file cannot be used ({e})"}
    automatic = (cfg["review"] or {}).get("automatic")
    if not automatic:
        return {"state": OFF, "renewed": None, "why": "the factory file names no automatic reviewer"}
    out = {"renewed": automatic.get("credential_renewed")}
    try:
        reviewer.credential(environ)
    except reviewer.ReviewerError as e:
        return {**out, "state": MISSING, "why": str(e)}
    why = ""
    for attempt in (1, 2):
        try:
            reply = call(PING, environ, model, system=PING_SYSTEM, ask=PING_ASK, seconds=PING_SECONDS)
        except reviewer.CredentialRefused as e:
            return {**out, "state": REFUSED, "why": str(e)}
        except reviewer.ReviewerError as e:
            why = str(e)
        else:
            if isinstance(reply, str) and reply.strip():
                return {**out, "state": WORKING, "why": "the model answered"}
            why = "The model gave no answer"
        if attempt == 1:
            pause(RETRY_SECONDS)
    return {**out, "state": FAILED, "why": why}


def check_result(result) -> dict:
    """The JSON handed from the check step, checked again. Anything that is not such a result means the check step
    did not finish, which is reported as a failure to reach the model. `known` is false then: nothing was learnt, so
    nothing may be closed on the strength of it."""
    if not isinstance(result, dict) or result.get("state") not in STATES:
        return {"state": FAILED, "renewed": None, "known": False}
    renewed = result.get("renewed")
    try:
        renewed = datetime.date.fromisoformat(renewed).isoformat() if isinstance(renewed, str) else None
    except ValueError:
        renewed = None
    return {"state": result["state"], "renewed": renewed, "known": True}


def problems(result: dict, today: datetime.date) -> list[str]:
    """What needs a person, as keys of PROBLEMS."""
    if result["state"] == OFF:
        return []
    found = [result["state"]] if result["state"] in PROBLEMS else []
    if result["renewed"]:
        age = (today - datetime.date.fromisoformat(result["renewed"])).days
        if age >= RENEW_AFTER_DAYS or age < -DAYS_AHEAD:
            found.append(DUE)
    return found


def own_issues(gh) -> dict[str, list[int]]:
    """Open issues this check opened, by kind. Only an issue from the workflow account counts, so no person and no
    other app can stop a warning by opening an issue that looks like one. (Another workflow in the same repository
    posts from the same account; the gate records the same limit for comments.)"""
    found: dict[str, list[int]] = {}
    for issue in gh.pages("issues?state=open", 5000):
        user = issue.get("user") or {}
        if "pull_request" in issue or user.get("login") != WORKFLOW_ACCOUNT or user.get("type") != "Bot":
            continue
        first = ((issue.get("body") or "").splitlines() or [""])[0].strip()
        for kind in PROBLEMS:
            if first == MARK % kind:
                found.setdefault(kind, []).append(issue["number"])
    return found


def report(gh, result, today: datetime.date | None = None) -> tuple[int, list[str]]:
    """The report step: opens and closes issues. Returns (exit code, what it did). The code is 1 while the reviewer
    cannot work, so the scheduled run also shows red; a renewal that is only due is not a failure."""
    result = check_result(result)
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    existing = own_issues(gh)
    if result["state"] == OFF:
        said = ["No automatic reviewer to watch."]
        for number in sorted(n for numbers in existing.values() for n in numbers):
            said.append(close(gh, number, STOPPED))
        return 0, said
    found = problems(result, today)
    said = [] if found else ["The automatic reviewer's model credential works."]
    for kind, (title, words) in PROBLEMS.items():
        numbers = existing.get(kind, [])
        if kind in found:
            if numbers:
                said.append(f"{title}: issue #{numbers[0]} is already open.")
            else:
                made = json.loads(gh._request("POST", "issues", {"title": title, "body": MARK % kind + "\n" + words + FOOT}))
                said.append(f"{title}: opened issue #{made.get('number')}.")
        elif result["known"] and (kind == DUE or result["state"] == WORKING):
            # A problem with the credential is known to be gone only when the model has answered, and a renewal is
            # known not to be due only when the check step said what day the factory file gives.
            for number in numbers:
                said.append(close(gh, number, RESOLVED))
    return (1 if any(kind != DUE for kind in found) else 0), said


def close(gh, number: int, words: str) -> str:
    gh._request("POST", f"issues/{number}/comments", {"body": words})
    gh._request("PATCH", f"issues/{number}", {"state": "closed"})
    return f"Closed issue #{number}."


def one_line(text) -> str:
    """Text for the run's log: one line, and nothing GitHub would read as a workflow command."""
    return " ".join(str(text).split()).replace("::", ": :")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Checks that the automatic reviewer can still reach its model.")
    steps = parser.add_subparsers(dest="step", required=True)
    one = steps.add_parser("check", help="make one small model call and write the result")
    one.add_argument("--out", required=True)
    one.add_argument("--model", default=None)
    two = steps.add_parser("report", help="open or close an issue")
    two.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    gh = review_gate.GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    if args.step == "check":
        result = check(gh, os.environ, args.model or None)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"state": result["state"], "renewed": result["renewed"]}, f)
        print(f"Reviewer health: {result['state']} ({one_line(result['why'])})")
        return 0
    try:
        with open(args.result, encoding="utf-8") as f:
            result = json.load(f)
    except (OSError, json.JSONDecodeError):
        result = None
    try:
        code, said = report(gh, result)
    except urllib.error.HTTPError as e:
        checked = check_result(result)
        fine = checked["state"] in (OFF, WORKING) and not problems(checked, datetime.datetime.now(datetime.timezone.utc).date())
        print(f"::{'warning' if fine else 'error'}::Reviewer health is '{checked['state']}', and issues could not be "
              f"read, opened or closed (HTTP {e.code}). Issues may be switched off for this repository, or the job "
              "lacks `issues: write`.")
        return 0 if fine else 1
    for line in said:
        print(line)
    if code:
        print("::error::The automatic reviewer cannot reach its model. See the issue this check opened.")
    return code


if __name__ == "__main__":
    sys.exit(main())
