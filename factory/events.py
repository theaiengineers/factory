"""The record of every step: a building's events (spec/event-format.md), written out from what GitHub already holds.

    python3 -m factory.events export --limit 30 > events.jsonl     # the pull requests changed most recently
    python3 -m factory.events export --pr 12                        # one pull request
    python3 -m factory.events check < events.jsonl                  # is every line a well-formed event?

Nothing is stored anywhere new. A pull request, its commits, the checks that ran on them and the statuses the review
gate set are already a dated, append-only history; `export` reads it and writes one event for each step. That means
the record can be made again at any time, covers the time before it was first asked for, and needs no secret beyond a
token that can read the repository.

Every event's `id` is worked out from what it describes, so exporting twice gives the same ids and a reader that has
seen an event keeps one copy (the spec's "repeats are harmless").

What it cannot see, because GitHub does not keep it:
  - when a commit was pushed. `change.updated` carries the commit's own date, which the author's machine set;
  - commits that a force-push removed from a pull request, and whatever was decided about them;
  - who an account is. A person's login may be driven by an agent.
What the review station decided is read from the statuses the gate set. A status is not signed: anyone who can write to
the repository can set one under the gate's name, and it will be read as the gate's.

It reads the factory file from the default branch as it is now. Required check names that have changed since are not
known for older pull requests.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.error

from . import config, review_gate, reviewer

SPEC = "factory.event/0.1"
TYPES = ("change.opened", "change.updated", "change.closed", "station.entered", "station.finished", "change.merged",
         "change.sorted", "shipment.held", "shipment.started", "shipment.landed", "shipment.rolled_back",
         "action.needed", "action.cleared")
OUTCOMES = ("pass", "sent_back", "failed", "held", "skipped", "error")
ACTOR_KINDS = ("agent", "person", "system")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
MAX_COMMITS = 250                # GitHub lists at most this many commits of a pull request
TITLE_LIMIT = 200
ORDER = {t: i for i, t in enumerate(("change.opened", "change.updated", "station.entered", "action.needed",
                                     "action.cleared", "station.finished", "change.merged", "change.closed"))}

# What the review gate's status says, by the words review_gate.decide gives it. (kind, reason code, plain sentence)
SKIPPED, PASSED, SENT_BACK, PERSON, WAITING, ERROR, UNKNOWN = "skipped", "pass", "sent_back", "person", "waiting", "error", "unknown"
DECISIONS = (
    ("No code changed", SKIPPED, "review.not_needed", "No reviewed file changed, so no review was needed"),
    ("Review record for ", PASSED, "review.pass", "A review of this commit recorded a pass"),
    ("Review of ", SENT_BACK, "review.findings", "A review of this commit recorded blocking findings"),
    ("Automatic review passed ", PERSON, "review.person_needed", "The automatic review passed; this change also needs a person's review"),
    ("The review record is older than commit ", WAITING, "review.waiting", "Waiting for a review of this commit"),
    ("Review is for ", WAITING, "review.waiting", "Waiting for a review of this commit"),
    ("Review marker is malformed", WAITING, "review.waiting", "Waiting for a review of this commit"),
    ("No independent review recorded for ", WAITING, "review.waiting", "Waiting for a review of this commit"),
)


def decision(state: str, description: str | None) -> tuple[str, str, str]:
    """(kind, reason code, sentence) for one status the gate set."""
    if state == "error":
        return ERROR, "review.error", "The review gate could not decide"
    for start, kind, code, text in DECISIONS:
        if (description or "").startswith(start) and state == ("success" if kind in (SKIPPED, PASSED) else "failure"):
            return kind, code, text
    return UNKNOWN, "review.status_unrecognised", "A status was set under the gate's name in words the gate does not use"


def event_id(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]


def actor_of(user: dict | None, automatic: dict | None = None) -> dict | None:
    """Who did it, as far as an account says. The automatic reviewer's account is an agent; another bot is `system`."""
    login = (user or {}).get("login")
    if not login:
        return None
    if automatic and login == automatic.get("account") and (user or {}).get("type") == "Bot":
        return {"kind": "agent", "id": login}
    return {"kind": "system" if (user or {}).get("type") == "Bot" else "person", "id": login}


class Writer:
    """Makes the events of one building, each with the fields every event carries."""

    def __init__(self, factory: str, building: str, recorded: str):
        self.base = {"spec": SPEC, "factory": factory, "building": building, "line": None}
        self.recorded, self.events = recorded, []

    def add(self, type_: str, time: str | None, number: int, commit: str | None = None, key=(), **more) -> None:
        if not time:
            return
        change = {"number": number, **({"commit": commit} if commit else {})}
        event = {**self.base, "id": event_id(self.base["building"], number, type_, commit or "", time, *key),
                 "time": time, "recorded": self.recorded, "change": change, "type": type_}
        event.update({k: v for k, v in more.items() if v is not None})
        self.events.append(event)


def testing_events(out: Writer, gh, number: int, sha: str, station: str, names: list[str]) -> None:
    """One `station.entered` and, when every required check has finished, one `station.finished` for the commit."""
    if not names:
        return
    runs = [r for r in (gh.get(f"commits/{sha}/check-runs?per_page=100").get("check_runs") or []) if r.get("name") in names]
    if not runs:
        return
    newest: dict = {}
    for r in sorted(runs, key=lambda r: (r.get("started_at") or "", r.get("id") or 0)):
        newest[(r.get("name"), (r.get("check_suite") or {}).get("id"))] = r
    standing = list(newest.values())
    started = min((r.get("started_at") for r in standing if r.get("started_at")), default=None)
    out.add("station.entered", started, number, sha, station=station, actor={"kind": "system", "id": "checks"})
    reported = {r.get("name") for r in standing}
    if reported != set(names) or any(r.get("status") != "completed" or not r.get("completed_at") for r in standing):
        return
    failed = sorted({r["name"] for r in standing if r.get("conclusion") not in reviewer.PASSED_CONCLUSIONS})
    finished = max(r["completed_at"] for r in standing)
    out.add("station.finished", finished, number, sha, station=station, outcome="failed" if failed else "pass",
            actor={"kind": "system", "id": "checks"}, evidence=next((r.get("html_url") for r in standing if r.get("html_url")), None),
            reason={"code": "checks.failed", "text": "A required check failed"} if failed else None,
            data={"factory": {"checks": sorted(names), **({"failed": failed} if failed else {})}})


def review_events(out: Writer, gh, number: int, sha: str, station: str, context: str, records: list[dict], automatic) -> None:
    """The review station's events for one commit, from the statuses the gate set on it, oldest first. The gate sets
    a status each time it runs, so a run of statuses that say the same thing is one event."""
    statuses = [s for s in gh.pages(f"commits/{sha}/statuses", 1000) if s.get("context") == context and s.get("created_at")]
    last, waiting_for_person = None, False
    for s in sorted(statuses, key=lambda s: (s["created_at"], s.get("id") or 0)):
        kind, code, text = decision(s.get("state") or "", s.get("description"))
        if kind == last:
            continue
        last, when, link = kind, s["created_at"], s.get("target_url")
        gate = {"kind": "system", "id": "review gate"}
        if waiting_for_person and kind != PERSON:
            out.add("action.cleared", when, number, sha, station=station, key=("person",), actor=gate, evidence=link)
            waiting_for_person = False
        if kind == WAITING:
            out.add("station.entered", when, number, sha, station=station, actor=gate, evidence=link)
        elif kind == PERSON:
            waiting_for_person = True
            out.add("action.needed", when, number, sha, station=station, key=("person",), actor=gate, evidence=link,
                    reason={"code": code, "text": text}, assignees=[])
        elif kind in (PASSED, SENT_BACK):
            # The record the gate counted is not named in a status. The newest record for this commit with that
            # verdict, posted before the status, is taken to be it.
            verdict = review_gate.PASS if kind == PASSED else review_gate.FINDINGS
            match = [c for c in records if c["marker"] == (sha.lower(), verdict) and (c.get("created_at") or "") <= when]
            comment = max(match, key=lambda c: c.get("created_at") or "", default=None)
            out.add("station.finished", when, number, sha, station=station, outcome=kind,
                    actor=actor_of((comment or {}).get("user"), automatic) or gate,
                    evidence=(comment or {}).get("html_url") or link,
                    reason={"code": code, "text": text} if kind == SENT_BACK else None)
        elif kind == SKIPPED:
            out.add("station.finished", when, number, sha, station=station, outcome="skipped", actor=gate, evidence=link,
                    reason={"code": code, "text": text})
        elif kind == ERROR:
            out.add("station.finished", when, number, sha, station=station, outcome="error", actor=gate, evidence=link,
                    reason={"code": code, "text": text})
        else:
            # Not the gate's words: recorded as what the status claims, and marked, because it decides merging.
            out.add("station.finished" if s.get("state") == "success" else "station.entered", when, number, sha, station=station,
                    outcome="pass" if s.get("state") == "success" else None, actor=actor_of(s.get("creator")) or gate,
                    evidence=link, reason={"code": code, "text": text}, data={"factory": {"unrecognised_status": True}})


def pull_request_events(out: Writer, gh, pr: dict, cfg: dict, context: str) -> None:
    number, opened = pr["number"], pr.get("created_at")
    automatic = (cfg["review"] or {}).get("automatic") if cfg.get("review") else None
    review_station = (cfg["review"] or {}).get("station") if cfg.get("review") else None
    checks = [(s.get("station"), [n for n in s.get("require") or [] if isinstance(n, str) and n])
              for s in cfg["raw"].get("intake") or [] if isinstance(s, dict) and s.get("uses") == "checks" and s.get("station")]
    commits = gh.pages(f"pulls/{number}/commits", MAX_COMMITS)
    dated = [(c["sha"], ((c.get("commit") or {}).get("committer") or {}).get("date")) for c in commits if c.get("sha")]
    before = [sha for sha, date in dated if date and opened and date <= opened]
    title = " ".join(str(pr.get("title") or "").split())[:TITLE_LIMIT]
    out.add("change.opened", opened, number, before[-1] if before else None, actor=actor_of(pr.get("user")),
            evidence=pr.get("html_url"), data={"factory": {"title": title, "base": (pr.get("base") or {}).get("ref")}})
    for sha, date in dated:
        if sha not in before:
            out.add("change.updated", date, number, sha, data={"factory": {"time_is": "the commit's own date, not when it was pushed"}})
    records = []
    if review_station:
        for c in gh.pages(f"issues/{number}/comments", 10_000):
            marker = review_gate.parse_marker(c.get("body"))
            if marker:
                records.append({**c, "marker": marker})
    head = (pr.get("head") or {}).get("sha")
    for sha in dict.fromkeys([sha for sha, _ in dated] + ([head] if head else [])):
        for station, names in checks:
            testing_events(out, gh, number, sha, station, names)
        if review_station:
            review_events(out, gh, number, sha, review_station, context, records, automatic)
    if pr.get("merged_at"):
        out.add("change.merged", pr["merged_at"], number, head, actor=actor_of(pr.get("merged_by")), evidence=pr.get("html_url"),
                data={"factory": {"merge_commit": pr.get("merge_commit_sha")}})
    elif pr.get("state") == "closed":
        out.add("change.closed", pr.get("closed_at"), number, head, evidence=pr.get("html_url"))


def export(gh, cfg: dict, numbers: list[int] | None = None, limit: int = 30, since: str | None = None,
           context: str = review_gate.CONTEXT, recorded: str | None = None) -> list[dict]:
    """The events of the named pull requests, or of the `limit` changed most recently (not before `since`), in order
    of time. Only pull requests into the factory file's `branch` are read, when it names one."""
    recorded = recorded or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = cfg["raw"]
    building = raw["building"] if isinstance(raw.get("building"), str) and raw["building"] else gh.repo
    factory = raw["factory"] if isinstance(raw.get("factory"), str) and raw["factory"] else gh.repo.split("/")[0]
    branch = raw["branch"] if isinstance(raw.get("branch"), str) and raw["branch"] else None
    out = Writer(factory, building, recorded)
    if numbers:
        prs = [gh.get(f"pulls/{n}") for n in numbers]
    else:
        prs, page = [], 1
        while len(prs) < limit:
            batch = gh.get(f"pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}")
            fresh = [p for p in batch if not since or (p.get("updated_at") or "") >= since]
            prs += [p for p in fresh if not branch or (p.get("base") or {}).get("ref") == branch]
            if len(batch) < 100 or len(fresh) < len(batch):
                break
            page += 1
        # The list leaves out who merged; one more read each gives it.
        prs = [gh.get(f"pulls/{p['number']}") if p.get("merged_at") else p for p in prs[:limit]]
    for pr in prs:
        pull_request_events(out, gh, pr, cfg, context)
    unique = {e["id"]: e for e in out.events}
    return sorted(unique.values(), key=lambda e: (e["time"], e["change"]["number"], ORDER.get(e["type"], 99), e["id"]))


# ── checking an event ──────────────────────────────────────────────────────────────────────────────────────────────
def problems(event) -> list[str]:
    """What is wrong with one event against the spec's required fields and closed lists. Empty when nothing is."""
    if not isinstance(event, dict):
        return ["an event must be a JSON object"]
    found = []
    spec = event.get("spec")
    if not isinstance(spec, str) or not spec.startswith("factory.event/"):
        found.append("`spec` must be factory.event/ and a version")
    for name in ("id", "factory", "building"):
        if not isinstance(event.get(name), str) or not event[name]:
            found.append(f"`{name}` is required and must be text")
    for name in ("time", "recorded"):
        if name in event or name == "time":
            if not isinstance(event.get(name), str) or not TIME.match(event[name]):
                found.append(f"`{name}` must be a UTC time such as 2026-01-15T12:16:00Z")
    type_ = event.get("type")
    if type_ not in TYPES:
        return found + ["`type` is not one the spec lists"]
    if type_.startswith(("change.", "station.")):
        change = event.get("change")
        if not isinstance(change, dict) or not isinstance(change.get("number"), int) or isinstance(change.get("number"), bool):
            found.append("`change.number` is required for a change or station event")
    if type_.startswith("station.") and (not isinstance(event.get("station"), str) or not event["station"]):
        found.append("`station` is required for a station event")
    if type_.startswith("shipment."):
        if type_ != "shipment.held" and not event.get("shipment"):
            found.append("`shipment` is required for a shipment event")
        if not isinstance(event.get("line"), str) or not event["line"]:
            found.append("`line` is required for a shipment event")
    if type_ == "station.finished":
        if event.get("outcome") not in OUTCOMES:
            found.append("`outcome` on station.finished must be one the spec lists")
    elif "outcome" in event:
        found.append("`outcome` belongs only on station.finished")
    needs_reason = type_ in ("shipment.held", "shipment.rolled_back", "action.needed") or event.get("outcome") in ("sent_back", "held")
    reason = event.get("reason")
    if needs_reason or reason is not None:
        if not isinstance(reason, dict) or not isinstance(reason.get("code"), str) or not reason["code"]:
            found.append("`reason.code` is required on a hold, a send-back, a rollback and an action needed")
    actor = event.get("actor")
    if actor is not None and (not isinstance(actor, dict) or actor.get("kind") not in ACTOR_KINDS):
        found.append("`actor.kind` must be agent, person or system")
    if "data" in event and not isinstance(event["data"], dict):
        found.append("`data` must be an object")
    return found


def check_lines(lines) -> tuple[int, list[str]]:
    """(events read, problems) for a JSON Lines stream. A repeated id with different content is a problem."""
    count, said, seen = 0, [], {}
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        count += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            said.append(f"line {n}: not JSON")
            continue
        said += [f"line {n}: {p}" for p in problems(event)]
        if isinstance(event, dict) and isinstance(event.get("id"), str):
            same = {k: v for k, v in event.items() if k != "recorded"}
            if seen.setdefault(event["id"], same) != same:
                said.append(f"line {n}: the id was used earlier for a different event")
    return count, said


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The record of every step, as events (spec/event-format.md).")
    steps = parser.add_subparsers(dest="step", required=True)
    one = steps.add_parser("export", help="write a repository's events, one JSON object per line")
    one.add_argument("--pr", type=int, action="append", help="a pull request number; may be given more than once")
    one.add_argument("--limit", type=int, default=30, help="how many pull requests, most recently changed first (default 30)")
    one.add_argument("--since", default=None, help="leave out pull requests not changed since this UTC time (2026-01-15T00:00:00Z)")
    one.add_argument("--context", default=review_gate.CONTEXT, help="the name of the gate's status (default independent-review)")
    steps.add_parser("check", help="read events on standard input and say what is wrong with them")
    args = parser.parse_args(argv)
    if args.step == "check":
        count, said = check_lines(sys.stdin)
        for line in said:
            print(line)
        print(f"{count} event(s), {len(said)} problem(s).")
        return 1 if said else 0
    if args.since and not TIME.match(args.since):
        print("--since must be a UTC time such as 2026-01-15T00:00:00Z", file=sys.stderr)
        return 2
    gh = review_gate.GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    try:
        cfg = config.parse(gh._request("GET", f"contents/{config.FILE}", accept="application/vnd.github.raw+json").decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        print(f"No {config.FILE} on the default branch", file=sys.stderr)
        return 2
    except config.ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    for event in export(gh, cfg, args.pr, max(1, args.limit), args.since, args.context):
        sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
