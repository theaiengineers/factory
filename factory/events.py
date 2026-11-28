"""The record of every step: a building's events (spec/event-format.md), written out from what GitHub already holds.

    python3 -m factory.events export --limit 30 > events.jsonl     # the pull requests changed most recently
    python3 -m factory.events export --pr 12                        # one pull request
    python3 -m factory.events check < events.jsonl                  # is every line a well-formed event?

Nothing is stored anywhere new. A pull request, its commits and the statuses the review gate set are already a
dated, append-only history; `export` reads it and writes one event for each step. That means the record can be made
again at any time, covers the time before it was first asked for, and needs no secret beyond a token that can read
the repository.

It covers the change itself (opened, new commits, merged, closed) and the review station. It does not yet cover the
checks station: what GitHub keeps about check runs changes as they wait, start, are re-run and are copied, and a
true record of "the tests failed once" needs more than a first version can promise. See docs/EVENTS.md.

Every event's `id` is worked out from GitHub's own id for the thing it is made from (a status, a pull request),
which never changes. What an event says happened (its type, time, change, station, outcome and reason) is fixed with
it, and nothing that can be edited afterwards, such as a title, is carried. So exporting twice gives the same events
with the same ids, and a reader that keeps one event for each id can be fed overlapping exports (the spec's "repeats
are harmless").

Nothing from the last minute is written: a second that is still going on has not shown everything that happens in
it. The next export has it.

Two things are read as they are at the time of the export, and so can differ between two exports of one event: who
(`actor`), since an account can be renamed, and the link (`evidence`), since a review record can be deleted, after
which the gate is named in its place, and a repository can be renamed. And a later export can lack events an
earlier one had; docs/EVENTS.md lists the ways known (a force-push, an earlier close, commits shared with another
pull request, a deleted review record, a pull request leaving the list that is read).

The review station's name is part of the ids. Rename it in the factory file and the same history comes out as new
events with new ids, beside the ones an earlier export gave.

What it cannot see, because GitHub does not keep it or the gate never said it:
  - when a commit was pushed. `change.updated` carries the commit's own date, which the author's machine set;
  - commits that a force-push removed from a pull request, and whatever was decided about them;
  - a review of a commit that was no longer the head when the gate next ran: the gate set no status for it;
  - which commit was a pull request's head at a given time: so the gate's statuses are read only on the commit
    that is its head now and on commits a review record posted on it names. An earlier head nobody reviewed
    leaves no review events;
  - who an account is. A person's login may be driven by an agent.
What the review station decided is read from the statuses the gate set. A status is not signed: anyone who can write to
the repository can set one under the gate's name, and it will be read as the gate's.
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

from . import config, review_gate

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
MAX_STATUSES = 1000              # statuses read for one commit, newest first, under every name
MAX_COMMENTS = 10_000            # comments read for one pull request
SETTLE_SECONDS = 60              # nothing newer than this is written: see export

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


SHARED = re.compile(r"#(\d+) into [^:]*: ")


def shared_with(description: str | None) -> tuple[int | None, str]:
    """(pull request number or None, the rest). When several open pull requests have the same head commit the gate
    sets one status for the commit, for the one that is worst off, and names it at the front (review_gate.evaluate)."""
    m = SHARED.match(description or "")
    return (int(m.group(1)), (description or "")[m.end():]) if m else (None, description or "")


def decision(state: str, description: str | None) -> tuple[str, str, str]:
    """(kind, reason code, sentence) for one status the gate set, with any pull request named at its front taken off."""
    description = shared_with(description)[1]
    if state == "error" and (description or "").startswith("Gate could not decide"):
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
    """Makes the events of one building, each with the fields every event carries.

    An event's id comes from `source`: GitHub's own id for the thing the event is made from (a status, a pull
    request's number), with a word for which event of that thing it is. Those ids never change and are never
    reused, so two statuses in one second are two events, and the same history gives the same id in every export.
    The pull request's number is part of it too: a status on a head commit that two open pull requests share is
    read for each of them."""

    def __init__(self, factory: str, building: str, recorded: str):
        self.base = {"spec": SPEC, "factory": factory, "building": building, "line": None}
        self.recorded, self.events = recorded, []

    def add(self, type_: str, time: str | None, number: int, commit: str | None, source: tuple, **more) -> None:
        if not time:
            return
        change = {"number": number, **({"commit": commit} if commit else {})}
        event = {**self.base, "id": event_id(self.base["building"], number, *source),
                 "time": time, "recorded": self.recorded, "change": change, "type": type_}
        event.update({k: v for k, v in more.items() if v is not None})
        self.events.append(event)


def lifetime(pr: dict) -> tuple[str, str | None]:
    """(opened, ended): `ended` is when it merged or was last closed, or None while it is open."""
    return pr.get("created_at") or "", pr.get("merged_at") or (pr.get("closed_at") if pr.get("state") == "closed" else None)


def review_events(out: Writer, gh, number: int, sha: str, station: str, context: str, records: list[dict], automatic,
                  opened: str | None = None, ended: str | None = None) -> None:
    """The review station's events for one commit, from the statuses the gate set on it, in the order GitHub gave
    them ids. The gate sets a status each time it runs, so a run of statuses that say the same thing is one event.

    A status belongs to a commit, not to a pull request, and a commit can be on more than one pull request. Only
    statuses set while this pull request was open (from `opened` to `ended`, when it has ended) are read; which
    commits are read at all is decided by the caller (see pull_request_events)."""
    statuses = [s for s in gh.pages(f"commits/{sha}/statuses", MAX_STATUSES)
                if s.get("context") == context and s.get("created_at") and s.get("id") is not None
                and (not opened or s["created_at"] >= opened) and (not ended or s["created_at"] <= ended)]
    last, waiting_for_person = None, False
    for s in sorted(statuses, key=lambda s: (s["created_at"], s["id"])):
        about = shared_with(s.get("description"))[0]
        if about is not None and about != number and s.get("state") != "success":
            # The commit is the head of several open pull requests, and this status speaks for another of them.
            # It says nothing about this one, which may be in a better state.
            continue
        kind, code, text = decision(s.get("state") or "", s.get("description"))
        # Words that are not the gate's are only "the same thing" when the state is the same too: a failure and then
        # a success in unknown words are two events, and the second is the one that lets the pull request merge.
        said = (kind, s.get("state") if kind == UNKNOWN else None)
        if said == last:
            continue
        last, when, link = said, s["created_at"], s.get("target_url") if isinstance(s.get("target_url"), str) else None
        gate, src = {"kind": "system", "id": "review gate"}, ("status", s["id"], station)
        if waiting_for_person and kind != PERSON:
            out.add("action.cleared", when, number, sha, (*src, "cleared"), station=station, actor=gate, evidence=link)
            waiting_for_person = False
        if kind == WAITING:
            out.add("station.entered", when, number, sha, (*src, "entered"), station=station, actor=gate, evidence=link)
        elif kind == PERSON:
            waiting_for_person = True
            out.add("action.needed", when, number, sha, (*src, "needed"), station=station, actor=gate, evidence=link,
                    reason={"code": code, "text": text}, assignees=[])
        elif kind in (PASSED, SENT_BACK):
            # The record the gate counted is not named in a status. The newest record for this commit with that
            # verdict, posted before the status, is taken to be it. This is read as it is now: see the module's notes.
            verdict = review_gate.PASS if kind == PASSED else review_gate.FINDINGS
            match = [c for c in records if c["marker"] == (sha.lower(), verdict) and (c.get("created_at") or "") <= when]
            comment = max(match, key=lambda c: (c.get("created_at") or "", c.get("id") or 0), default=None)
            out.add("station.finished", when, number, sha, (*src, "finished"), station=station, outcome=kind,
                    actor=actor_of((comment or {}).get("user"), automatic) or gate,
                    evidence=(comment or {}).get("html_url") or link,
                    reason={"code": code, "text": text} if kind == SENT_BACK else None)
        elif kind == SKIPPED:
            out.add("station.finished", when, number, sha, (*src, "finished"), station=station, outcome="skipped", actor=gate,
                    evidence=link, reason={"code": code, "text": text})
        elif kind == ERROR:
            out.add("station.finished", when, number, sha, (*src, "finished"), station=station, outcome="error", actor=gate,
                    evidence=link, reason={"code": code, "text": text})
        else:
            # Not the gate's words: recorded as what the status claims, and marked, because it decides merging.
            passed = s.get("state") == "success"
            out.add("station.finished" if passed else "station.entered", when, number, sha, (*src, "finished" if passed else "entered"),
                    station=station, outcome="pass" if passed else None, actor=actor_of(s.get("creator")) or gate,
                    evidence=link, reason={"code": code, "text": text}, data={"factory": {"unrecognised_status": True}})


def pull_request_events(out: Writer, gh, pr: dict, cfg: dict, context: str) -> None:
    number, opened = pr["number"], pr.get("created_at")
    review = cfg.get("review") or {}
    automatic, review_station = review.get("automatic"), str(review["station"]) if review.get("station") else None
    commits = gh.pages(f"pulls/{number}/commits", MAX_COMMITS)
    dated = [(c["sha"], ((c.get("commit") or {}).get("committer") or {}).get("date")) for c in commits if c.get("sha")]
    link = pr.get("html_url") if isinstance(pr.get("html_url"), str) else None
    # Nothing that can be edited later (the title, the base branch) and nothing GitHub does not keep (which commit
    # was the head at that moment) goes on this event.
    out.add("change.opened", opened, number, None, ("pull request", number, "opened"), actor=actor_of(pr.get("user")), evidence=link)
    for sha, date in dated:
        if date and opened and date > opened:
            out.add("change.updated", date, number, sha, ("pull request", number, "commit", sha),
                    data={"factory": {"time_is": "the commit's own date, not when it was pushed"}})
    records = []
    if review_station:
        for c in gh.pages(f"issues/{number}/comments", MAX_COMMENTS):
            marker = review_gate.parse_marker(c.get("body"))
            if marker:
                records.append({**c, "marker": marker})
    head = (pr.get("head") or {}).get("sha")
    if review_station:
        # The gate sets a status on a commit because it is the head of an open pull request, and GitHub does not keep
        # which commit was whose head when. A commit on this pull request's list may only ever have been the head of
        # another one (one branch built on another). So the statuses of a commit are read only when the commit is
        # known to have been this pull request's head: it is the head now, or a review record posted on this pull
        # request names it. A comment belongs to one pull request, so that link is certain; nobody reviews, on this
        # pull request, a commit that was never its head.
        listed = [sha for sha, _ in dated] + ([head] if head else [])
        reviewed = {c["marker"][0] for c in records}
        ended = lifetime(pr)[1]
        for sha in dict.fromkeys(listed):
            if sha == head or sha.lower() in reviewed:
                review_events(out, gh, number, sha, review_station, context, records, automatic, opened, ended)
    if pr.get("merged_at"):
        out.add("change.merged", pr["merged_at"], number, head, ("pull request", number, "merged"), actor=actor_of(pr.get("merged_by")),
                evidence=link, data={"factory": {"merge_commit": pr.get("merge_commit_sha")}})
    elif pr.get("state") == "closed":
        out.add("change.closed", pr.get("closed_at"), number, head, ("pull request", number, "closed", pr.get("closed_at")), evidence=link)


def export(gh, cfg: dict, numbers: list[int] | None = None, limit: int = 30, since: str | None = None,
           context: str = review_gate.CONTEXT, recorded: str | None = None, settle: int = SETTLE_SECONDS) -> list[dict]:
    """The events of the named pull requests, or of the `limit` changed most recently (not before `since`), in order
    of time. Only pull requests into the factory file's `branch` are read, when it names one and none is named here.

    Nothing from the last `settle` seconds is written: a second that is still going on has not shown everything
    that happens in it, and GitHub's lists can run a little behind. The next export has it."""
    now = datetime.datetime.strptime(recorded, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc) if recorded \
        else datetime.datetime.now(datetime.timezone.utc)
    recorded = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    settled_until = (now - datetime.timedelta(seconds=max(1, settle))).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = cfg["raw"]
    building = raw["building"] if isinstance(raw.get("building"), str) and raw["building"] else gh.repo
    factory = raw["factory"] if isinstance(raw.get("factory"), str) and raw["factory"] else gh.repo.split("/")[0]
    branch = raw["branch"] if isinstance(raw.get("branch"), str) and raw["branch"] else None
    out = Writer(factory, building, recorded)
    if numbers:
        prs = [gh.get(f"pulls/{n}") for n in dict.fromkeys(numbers)]
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
    for pr in sorted(prs, key=lambda p: p["number"]):
        pull_request_events(out, gh, pr, cfg, context)
    # In order of time. Events of one second stay in the order they were worked out in, which for one station on
    # one commit is the order GitHub recorded them: sorting them by kind could put a pass after the wait that
    # followed it. (Python's sort keeps equal keys in their first order.)
    unique: dict = {}
    for e in out.events:
        if e["time"][:19] + "Z" <= settled_until:
            unique.setdefault(e["id"], e)
    return sorted(unique.values(), key=lambda e: e["time"])


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
        if not isinstance(event.get(name), str) or not event[name].strip():
            found.append(f"`{name}` is required and must be text")
    for name in ("time", "recorded"):
        if event.get(name) is not None or name == "time":
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
    if type_ == "action.needed" and not (isinstance(event.get("assignees"), list) and all(isinstance(a, str) for a in event["assignees"])):
        found.append("`assignees` is required on action.needed (it may be empty)")
    if type_ == "station.finished":
        if event.get("outcome") not in OUTCOMES:
            found.append("`outcome` on station.finished must be one the spec lists")
    elif event.get("outcome") is not None:
        found.append("`outcome` belongs only on station.finished")
    needs_reason = type_ in ("shipment.held", "shipment.rolled_back", "action.needed") or event.get("outcome") in ("sent_back", "held")
    reason = event.get("reason")
    if needs_reason and (not isinstance(reason, dict) or not isinstance(reason.get("code"), str) or not reason["code"]):
        found.append("`reason.code` is required on a hold, a send-back, a rollback and an action needed")
    elif reason is not None and not isinstance(reason, dict):
        found.append("`reason` must be an object")
    for name in ("evidence", "line"):
        if event.get(name) is not None and not isinstance(event[name], str):
            found.append(f"`{name}` must be text")
    actor = event.get("actor")
    if actor is not None and (not isinstance(actor, dict) or actor.get("kind") not in ACTOR_KINDS):
        found.append("`actor.kind` must be agent, person or system")
    if event.get("data") is not None and not isinstance(event["data"], dict):
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
        except (ValueError, RecursionError):           # ValueError: not JSON, or a number too long to read
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
    if args.since and not is_time(args.since):
        print("--since must be a UTC time such as 2026-01-15T00:00:00Z", file=sys.stderr)
        return 2
    if not os.environ.get("GITHUB_REPOSITORY") or not os.environ.get("GITHUB_TOKEN"):
        print("Set GITHUB_REPOSITORY (OWNER/REPO) and GITHUB_TOKEN (a token that can read it).", file=sys.stderr)
        return 2
    gh = review_gate.GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    try:
        cfg = config.parse(gh._request("GET", f"contents/{config.FILE}", accept="application/vnd.github.raw+json").decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        print(f"No {config.FILE} on the default branch, or the repository cannot be read with this token." if e.code == 404
              else f"GitHub refused the request (HTTP {e.code}). Check the token.", file=sys.stderr)
        return 2
    except config.ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        found = export(gh, cfg, args.pr, max(1, args.limit), args.since, args.context)
    except urllib.error.HTTPError as e:
        print("GitHub has no such pull request, or the token cannot read it." if e.code == 404
              else f"GitHub refused a request (HTTP {e.code}); nothing was written.", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        print("GitHub could not be reached, or its answer could not be read; nothing was written.", file=sys.stderr)
        return 2
    for event in found:
        sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
