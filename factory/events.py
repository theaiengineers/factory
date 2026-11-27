"""The record of every step: a building's events (spec/event-format.md), written out from what GitHub already holds.

    python3 -m factory.events export --limit 30 > events.jsonl     # the pull requests changed most recently
    python3 -m factory.events export --pr 12                        # one pull request
    python3 -m factory.events check < events.jsonl                  # is every line a well-formed event?

Nothing is stored anywhere new. A pull request, its commits, the checks that ran on them and the statuses the review
gate set are already a dated, append-only history; `export` reads it and writes one event for each step. That means
the record can be made again at any time, covers the time before it was first asked for, and needs no secret beyond a
token that can read the repository.

Every event's `id` is worked out from the thing it describes (which pull request, commit, station, moment and
outcome), and an event carries nothing that can be edited afterwards, such as a title. So exporting twice gives the
same events with the same ids, and a reader that keeps one event for each id can be fed overlapping exports (the
spec's "repeats are harmless"). A later export can hold fewer events than an earlier one (see force-pushes below),
never different ones under an id already used.

What it cannot see, because GitHub does not keep it or the gate never said it:
  - when a commit was pushed. `change.updated` carries the commit's own date, which the author's machine set;
  - commits that a force-push removed from a pull request, and whatever was decided about them;
  - a review of a commit that was no longer the head when the gate next ran: the gate set no status for it;
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
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z")


def is_time(text) -> bool:
    """A real UTC moment written as the spec asks."""
    if not isinstance(text, str) or not TIME.match(text):
        return False
    try:
        datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True
MAX_COMMITS = 250                # GitHub lists at most this many commits of a pull request
MAX_RUN_PAGES = 10               # check runs read for one commit, a hundred to a page
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
        # Everything that tells two events apart goes into the id: two stations can decide in the same second.
        event = {**self.base, "id": event_id(self.base["building"], number, type_, commit or "", time,
                                             more.get("station") or "", more.get("outcome") or "", *key),
                 "time": time, "recorded": self.recorded, "change": change, "type": type_}
        event.update({k: v for k, v in more.items() if v is not None})
        self.events.append(event)


def check_runs(gh, sha: str) -> list[dict]:
    """Every check run on the commit, earlier attempts included (GitHub's default leaves those out, and then a re-run
    would rewrite what an earlier export said)."""
    runs = []
    for page in range(1, MAX_RUN_PAGES + 1):
        batch = gh.get(f"commits/{sha}/check-runs?filter=all&per_page=100&page={page}").get("check_runs") or []
        runs += batch
        if len(batch) < 100:
            break
    return runs


def testing_events(out: Writer, gh, number: int, sha: str, station: str, names: list[str]) -> None:
    """The checks station's events for one commit, replayed in order of time from its check runs.

    `station.entered` when a required check starts while the station is idle; `station.finished` each time every
    required check has reported and none is still running. A re-run after a failure is therefore a second entry and
    a second finish, and the first stays in the record."""
    if not names:
        return
    runs = [r for r in check_runs(gh, sha) if r.get("name") in names and r.get("started_at")]
    moments = [(r["started_at"], 0, r.get("id") or 0, r) for r in runs]
    moments += [(r["completed_at"], 1, r.get("id") or 0, r) for r in runs if r.get("status") == "completed" and r.get("completed_at")]
    checks, standing, busy = {"kind": "system", "id": "checks"}, {}, False
    for when, finished, _, r in sorted(moments, key=lambda m: m[:3]):
        key = (r.get("name"), (r.get("check_suite") or {}).get("id"))
        if not finished:
            standing[key] = r
            if not busy:
                busy = True
                out.add("station.entered", when, number, sha, station=station, actor=checks)
            continue
        if standing.get(key) is not r:
            continue                 # an attempt that a later one has already replaced
        current = list(standing.values())
        done = all(c.get("status") == "completed" and c.get("completed_at") and c["completed_at"] <= when for c in current)
        if not busy or not done or {c.get("name") for c in current} != set(names):
            continue
        busy = False
        failed = sorted({c["name"] for c in current if c.get("conclusion") not in reviewer.PASSED_CONCLUSIONS})
        out.add("station.finished", when, number, sha, station=station, outcome="failed" if failed else "pass", actor=checks,
                evidence=r.get("html_url"),
                reason={"code": "checks.failed", "text": "A required check did not pass"} if failed else None,
                data={"factory": {"checks": sorted(names), **({"failed": failed} if failed else {})}})


def review_events(out: Writer, gh, number: int, sha: str, station: str, context: str, records: list[dict], automatic) -> None:
    """The review station's events for one commit, from the statuses the gate set on it, oldest first. The gate sets
    a status each time it runs, so a run of statuses that say the same thing is one event."""
    statuses = [s for s in gh.pages(f"commits/{sha}/statuses", 1000) if s.get("context") == context and s.get("created_at")]
    last, waiting_for_person = None, False
    for s in sorted(statuses, key=lambda s: (s["created_at"], s.get("id") or 0)):
        kind, code, text = decision(s.get("state") or "", s.get("description"))
        # Words that are not the gate's are only "the same thing" when the state is the same too: a failure and then
        # a success in unknown words are two events, and the second is the one that lets the pull request merge.
        said = (kind, s.get("state") if kind == UNKNOWN else None)
        if said == last:
            continue
        last, when, link = said, s["created_at"], s.get("target_url")
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
    # Nothing that can be edited later (the title, the base branch) and nothing GitHub does not keep (which commit
    # was the head at that moment) goes on this event, so it is the same in every export.
    out.add("change.opened", opened, number, actor=actor_of(pr.get("user")), evidence=pr.get("html_url"))
    for sha, date in dated:
        if date and opened and date > opened:
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
    # Events of one moment keep the order they were worked out in (the gate's statuses in the order it set them),
    # so a send-back and a pass in the same second come out in the order they happened.
    unique: dict = {}
    for place, e in enumerate(out.events):
        unique.setdefault(e["id"], (place, e))
    ordered = sorted(unique.values(), key=lambda pe: (pe[1]["time"], pe[1]["change"]["number"], ORDER.get(pe[1]["type"], 99), pe[0]))
    return [e for _, e in ordered]


# ── checking an event ──────────────────────────────────────────────────────────────────────────────────────────────
def problems(event) -> list[str]:
    """What is wrong with one event against the spec's required fields and closed lists. Empty when nothing is."""
    if not isinstance(event, dict):
        return ["an event must be a JSON object"]
    found = []
    spec = event.get("spec")
    if not isinstance(spec, str) or not re.fullmatch(r"factory\.event/[0-9]+\.[0-9]+", spec):
        found.append("`spec` must be factory.event/ and a version")
    for name in ("id", "factory", "building"):
        if not isinstance(event.get(name), str) or not event[name]:
            found.append(f"`{name}` is required and must be text")
    for name in ("time", "recorded"):
        if name in event or name == "time":
            if not is_time(event.get(name)):
                found.append(f"`{name}` must be a UTC time such as 2026-01-15T12:16:00Z")
    type_ = event.get("type")
    if type_ not in TYPES:
        return found + ["`type` is not one the spec lists"]
    change, on_a_line = event.get("change"), isinstance(event.get("line"), str) and bool(event.get("line"))
    if change is not None or type_.startswith("change.") or (type_.startswith("station.") and not on_a_line):
        number = change.get("number") if isinstance(change, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            found.append("`change.number` is required for a change event, and for a station event that is not on a line")
        elif "commit" in change and (not isinstance(change["commit"], str) or not change["commit"]):
            found.append("`change.commit` must be text")
    if type_ == "change.sorted" and not (isinstance(event.get("lines"), list) and all(isinstance(i, str) for i in event["lines"])):
        found.append("`lines` is required on change.sorted and must be a list of line ids")
    if type_.startswith("station.") and (not isinstance(event.get("station"), str) or not event["station"]):
        found.append("`station` is required for a station event")
    if type_.startswith("shipment."):
        if type_ != "shipment.held" and not event.get("shipment"):
            found.append("`shipment` is required for a shipment event")
        if not on_a_line:
            found.append("`line` is required for a shipment event")
        if type_ in ("shipment.held", "shipment.started") and not isinstance(event.get("carries"), list):
            found.append("`carries` is required on shipment.held and shipment.started")
    if type_ == "action.needed" and not isinstance(event.get("assignees"), list):
        found.append("`assignees` is required on action.needed (it may be empty)")
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
        try:
            count, said = check_lines(sys.stdin)
        except UnicodeDecodeError:
            print("The input is not UTF-8 text.")
            return 1
        for line in said:
            print(line)
        print(f"{count} event(s), {len(said)} problem(s).")
        return 1 if said else 0
    if args.since and not TIME.match(args.since):
        print("--since must be a UTC time such as 2026-01-15T00:00:00Z", file=sys.stderr)
        return 2
    if not os.environ.get("GITHUB_REPOSITORY") or not os.environ.get("GITHUB_TOKEN"):
        print("Set GITHUB_REPOSITORY (OWNER/REPO) and GITHUB_TOKEN (a token that can read it).", file=sys.stderr)
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
    try:
        found = export(gh, cfg, args.pr, max(1, args.limit), args.since, args.context)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        print("GitHub has no such pull request, or the token cannot read it.", file=sys.stderr)
        return 2
    for event in found:
        sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
