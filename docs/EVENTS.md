# The record of every step

A factory keeps one record of what happened to each change: when it was opened, what each station decided, when it merged. Dashboards and reports read that record and nothing else. Its form is the [event format](../spec/event-format.md): one JSON object for each thing that happened, one to a line.

For a change's own life and for the independent review, nothing new has to be stored to keep that record. GitHub already holds a dated history of every pull request, its commits and the statuses the review gate set. `factory.events` reads that history and writes it out as events.

That has three consequences worth knowing:

- **It can be made again at any time.** Lose the file and it can be written out again, apart from the few things GitHub itself stops keeping (see the limits below).
- **It covers the past.** A repository that adopts the factory today can write out the record of last month's pull requests.
- **It needs no secret of its own.** A token that can read the repository is enough.

**It does not cover the tests yet.** There are no events for the checks station. What GitHub keeps about a check run changes while it waits for a machine, when it is run again, and when a partial re-run copies it, and a first version cannot promise that "the tests failed once" is recorded once and truly. A reader that needs test results asks GitHub for the commit's checks. See [what is not built](#what-is-not-built).

## Writing the record out

From a clone of the factory at the version you use, with its one dependency installed (as in step 2 of the [setup guide](SETUP.md)):

```
export GITHUB_REPOSITORY=OWNER/REPO
export GITHUB_TOKEN=$(gh auth token)
python3 -m factory.events export --limit 30 > events.jsonl
```

| Option | What it does |
|---|---|
| `--limit N` | The N pull requests changed most recently (default 30) |
| `--since TIME` | Leave out pull requests not changed since a UTC time, such as `2026-01-15T00:00:00Z` |
| `--pr N` | One pull request. May be given more than once |
| `--context NAME` | The name of the gate's status, if yours is not `independent-review` |

It reads `factory.yml` from the default branch for the review station's name, the automatic reviewer and the branch. When the factory file names a `branch`, only pull requests into it are read; with `--pr` the named pull requests are read whatever branch they are into. It makes a few requests for each pull request and one for each commit, so a large export takes a while and counts against the token's hourly limit.

## What stays the same between exports, and what does not

Every event's `id` is worked out from GitHub's own id for the thing the event is made from: a status, or a pull request. Those never change and are never used twice. What an event says happened (its type, time, change, station, outcome and reason) is fixed with it, and nothing that can be edited afterwards is carried, so a pull request's title and base branch are not in the record; a reader that wants them asks GitHub by the pull request's number. Export twice and the same events come out with the same ids, so a reader that keeps one event for each id can be fed overlapping exports.

Nothing from the last minute is written, by the clock of the machine running the export. A second that is still going on has not shown everything that happens in it. The next export has what was held back.

Two fields are read as they are at the time of the export and can differ between two exports of the same event: `actor`, because an account can be renamed, and `evidence`, because a review record can be deleted (the gate is then named in its place) and every address changes if the repository is renamed or moved. `check` reports such a pair as an id used for two different events, which is what it is.

A later export can lack events an earlier one had. The ways known: a force-push removes commits from a pull request; GitHub keeps only the last time a pull request was closed, so an earlier close of one that was reopened is gone; a pull request that shares commits with another can stop listing them once the other has merged, and events on a shared commit can stop being written once another pull request is found to have been open on it; and, when exporting the most recent pull requests and not named ones, a pull request can leave the list by falling outside `--limit` or being pointed at another branch.

The review station's name and the factory file's `building` (the repository's name, when the file gives none) are part of the ids. If either is changed, the same history comes out as new events with new ids, beside the ones an earlier export gave.

Events come out in order of time. Events of the same second on one commit keep the order in which GitHub recorded the statuses they are made from.

## What each event is made from

| Event | Made from | Its time |
|---|---|---|
| `change.opened` | The pull request | When it was opened |
| `change.updated` | Each commit on the pull request whose own date is after the pull request was opened | The commit's own date (see the limits) |
| `station.entered`, review | A gate status on a commit that says a review is awaited, when the one before it said something else | When the gate set it |
| `station.finished`, review | A gate status that says pass (`pass`), open findings (`sent_back`), no review needed (`skipped`), or could not decide (`error`) | When the gate set it |
| `action.needed` | A gate status that says the automatic review passed and a person's review is also needed | When the gate set it |
| `action.cleared` | The next gate status on that commit that says something else | When the gate set it |
| `change.merged` | The pull request | When it merged |
| `change.closed` | The pull request, closed without merging | When it closed |

The gate sets a status every time it runs. Several in a row that say the same thing are one event, so two findings records on one commit with nothing else between them are one `sent_back`.

A status in words the gate does not use becomes a `station.finished` with `pass` when its state is success, and a `station.entered` otherwise, and is marked (see the limits).

A status belongs to a commit, and a commit can be on more than one pull request: one closed and opened again as a new pull request, or one branch built on another with both into the same branch. The gate sets a status on a commit because it is the head of an open pull request, but GitHub does not keep which commit was whose head when. So a status is read as being about a pull request only when that is known:

- it was set while the pull request was open, from when it was opened to when it merged or was last closed; and
- the commit is the pull request's head now, or no other pull request that lists the commit was open at that moment.

This leaves gaps, stated here so that nothing false is written:

- **A stack that is open together.** On a commit that is not a pull request's head now, and that another open pull request also listed at the time, the status could have been about either, and is written for neither. The lower pull request of a stack keeps everything said about its present head, and loses what was said about its earlier heads while the upper one was open. The upper one has only what was said about its own commits.
- **Reopened after a successor.** If a pull request is closed, another is opened from the same head commit, and the first is later reopened, the first is read as open for the whole time and picks up what was said about the second while it was closed.
- **The same second.** A status set in the very second a pull request opened, merged or closed counts as inside its life, and so can be read for two pull requests on one commit.
- **Just after the end.** A status the gate set a moment after a pull request closed or merged is not read for it.

On a review's `pass` or `sent_back`, `actor` and `evidence` are the author and address of the newest review record for that commit with that verdict, posted before the gate's status. When there is none, they are the gate and its run.

`actor.kind` is `agent` for the automatic reviewer the factory file names, `system` for any other bot and for the gate, and `person` for any other account.

Reason codes the factory writes: `review.findings`, `review.not_needed`, `review.person_needed`, `review.error`, `review.status_unrecognised`. The sentence beside each code is fixed in the code.

When several open pull requests have the same head commit at the same time, the gate sets one status for the commit, for whichever of them is worst off, and names that pull request at the front. Such a status is read as being about the pull request it names and no other. This leaves three gaps, all on a shared head commit only:

- A status of success names none and is read for each of them, in the words written for one. A pull request that was reviewed and passed can come out as `skipped`, or the other way about. Both are successes.
- When two of them have findings, only the one the gate names gets a `sent_back`.
- With a very long base branch name the gate's description is cut short, and can be read as not the gate's words.

## What it cannot tell you

- **When a commit was pushed.** GitHub does not keep it. `change.updated` carries the commit's own date, which was set on the author's machine and can be anything; one dated in the future is not written until that time has passed. A commit made before the pull request was opened and pushed afterwards gets no `change.updated`, and when several commits are pushed together each gets one though only the last was ever the head.
- **Commits a force-push removed.** They are no longer listed on the pull request, so neither they nor what was decided about them appear.
- **A review of a commit that had already been replaced.** The review station's events come from the statuses the gate set. If a new commit arrived before the gate next ran, the gate never set a status for the review of the old one, and that review, a send-back included, leaves no event. The count of send-backs here can therefore be lower than the count of findings records on the pull requests.
- **That an action or a wait ended, when the commit was replaced.** An `action.needed` or a review `station.entered` on a commit is closed only by a later status on the same commit. When a new commit takes over, nothing closes the old one; read a later `change.updated`, `change.merged` or `change.closed` as ending it.
- **Which record the gate counted.** The reviewer named on a `pass` or `sent_back` is the author of the newest record with that verdict posted before the gate's status. A record from an account the gate does not count, posted in the few seconds between the real record and the gate's status, would be named in its place.
- **Who an account is.** `person` means an account that is not a bot. An agent working through a person's login looks the same.
- **That a status is the gate's.** A status is not signed. Anyone who can write to the repository can set one under the gate's name. A status in the gate's words is read as the gate's. A status in other words, in any state, is still recorded, because it decides whether the pull request can merge, and it is marked: `reason.code` is `review.status_unrecognised` and `data.factory.unrecognised_status` is true. A gate status set at a time when the factory file had no review station is read the same way.
- **What the factory file said in the past.** The review station's name is read as it is now, and applied to the whole history.
- **Very large or very busy pull requests.** GitHub lists at most 250 commits of a pull request. On one commit, the newest 1,000 statuses (under every name, not only the gate's) are read; on one pull request, at most 10,000 comments. Past those, events are missing and nothing says so.
- **An earlier close.** GitHub keeps only the last time a pull request was closed, so one that was closed, reopened and closed again has one `change.closed`.
- **Anything after the merge.** Sorting onto lines, shipments, holds and watches have no station in the factory yet, so no events.

The names in an event (a login, a station's name) come from GitHub and the factory file. A reader that shows them must treat them as text.

## Checking events

```
python3 -m factory.events check < events.jsonl
```

It reads events on standard input and says what is wrong with each line against the spec's required fields and closed lists, and whether an id was used for two different events. It ends with a count, and fails when there is any problem. It is for anything that writes events, not only this exporter. It does not check everything the spec says: not that a `shipment.started` names its actor or what `carries` holds, for instance, nor anything about what an event means. A time with a leap second (`:60`) is refused.

## What is not built

- **Events for the checks station.** A first attempt was reviewed five times and set aside. Each review found another way GitHub's check runs differ from a plain history: a waiting run carries a start time that is later replaced; a run cancelled while waiting keeps it; "Re-run failed jobs" copies the jobs it does not run again, with new ids and old times; and during a re-run a check whose new run has not started still shows its old result. The work is kept on the branch `events/checks-station`.
- **Somewhere to send them.** The factory file's `events:` key names sinks, and nothing sends to them yet. Run the export where the record is wanted.
- **A workflow that runs the export for you.**
- **A dashboard.**
