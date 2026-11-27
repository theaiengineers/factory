# The record of every step

A factory keeps one record of what happened to each change: when it was opened, what each station decided, when it merged. Dashboards and reports read that record and nothing else. Its form is the [event format](../spec/event-format.md): one JSON object for each thing that happened, one to a line.

For the stations the factory has today (tests, then the independent review), nothing new has to be stored to keep that record. GitHub already holds a dated history of every pull request, its commits, the checks that ran and the statuses the review gate set. `factory.events` reads that history and writes it out as events.

That has three consequences worth knowing:

- **It can be made again at any time.** Lose the file and nothing is lost.
- **It covers the past.** A repository that adopts the factory today can write out the record of last month's pull requests.
- **It needs no secret of its own.** A token that can read the repository is enough.

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

It reads `factory.yml` from the default branch for the station names, the required checks, the automatic reviewer and the branch. When the factory file names a `branch`, only pull requests into it are read. It makes several requests for each pull request and a few for each commit, so a large export takes a while and counts against the token's hourly limit.

Every event's `id` is worked out from what it describes. Export twice and the ids are the same, so a reader that keeps one event for each id can be fed overlapping exports.

## What each event is made from

| Event | Made from | Its time |
|---|---|---|
| `change.opened` | The pull request | When it was opened |
| `change.updated` | Each commit dated after the pull request was opened | The commit's own date (see the limits) |
| `station.entered`, testing | The check runs named in `require`, on a commit | When the first of them started |
| `station.finished`, testing | The same, once every required check has reported and finished. `pass`, or `failed` with the failed names in `data` | When the last of them finished |
| `station.entered`, review | The gate's first status on a commit that says a review is awaited | When the gate set it |
| `station.finished`, review | A gate status that says pass (`pass`), open findings (`sent_back`), no review needed (`skipped`), or could not decide (`error`) | When the gate set it |
| `action.needed` | A gate status that says the automatic review passed and a person's review is also needed | When the gate set it |
| `action.cleared` | The next gate status on that commit that says something else | When the gate set it |
| `change.merged` | The pull request | When it merged |
| `change.closed` | The pull request, closed without merging | When it closed |

The gate sets a status every time it runs. Several in a row that say the same thing are one event.

On a review's `pass` or `sent_back`, `actor` and `evidence` are the author and address of the newest review record for that commit with that verdict, posted before the gate's status. When there is none, they are the gate and its run.

`actor.kind` is `agent` for the automatic reviewer the factory file names, `system` for any other bot, the checks and the gate, and `person` for any other account.

Reason codes the factory writes: `review.findings`, `review.not_needed`, `review.person_needed`, `review.error`, `review.status_unrecognised`, `checks.failed`. The sentence beside each code is fixed in the code.

## What it cannot tell you

- **When a commit was pushed.** GitHub does not keep it. `change.updated` carries the commit's own date, which was set on the author's machine and can be anything.
- **Commits a force-push removed.** They are no longer listed on the pull request, so neither they nor what was decided about them appear.
- **Who an account is.** `person` means an account that is not a bot. An agent working through a person's login looks the same.
- **That a status is the gate's.** A status is not signed. Anyone who can write to the repository can set one under the gate's name. A status in the gate's words is read as the gate's. A status in other words is still recorded, because it decides whether the pull request can merge, and it is marked: `reason.code` is `review.status_unrecognised` and `data.factory.unrecognised_status` is true.
- **What the factory file said in the past.** The required checks and station names are read as they are now.
- **Anything after the merge.** Sorting onto lines, shipments, holds and watches have no station in the factory yet, so no events.

Text in an event that came from a pull request (its title, in `data.factory.title`, cut to one line of 200 characters) is whatever its author typed. A reader that shows it must treat it as text.

## Checking events

```
python3 -m factory.events check < events.jsonl
```

It reads events on standard input and says what is wrong with each line against the spec's required fields and closed lists, and whether an id was used for two different events. It ends with a count, and fails when there is any problem. It is for anything that writes events, not only this exporter.

## What is not built

- **Somewhere to send them.** The factory file's `events:` key names sinks, and nothing sends to them yet. Run the export where the record is wanted.
- **A workflow that runs the export for you.**
- **A dashboard.**
