# Factory

An open framework for running a repository as a software factory.

AI agents can now write most of a change. What is still missing is the line the change travels down afterwards: the tests, the independent review, the trial run, the decision about whether it may be released without a person, the release itself and the watch that follows. Factory describes that line in one file, enforces it, and records every step so that people can see what happened.

**Status: early.** The specs are drafts, and one station runs: the independent-review gate. The rest of the engine is being extracted from a setup that already runs in production.

## What runs today

The independent-review gate. It reads the risk rules from your `factory.yml`, works out whether a pull request needs a review, and sets a commit status that passes only when a reviewer has recorded a pass for the exact head commit. See [docs/REVIEW_STANDARD.md](docs/REVIEW_STANDARD.md) for the record a reviewer leaves, and the comment at the top of [.github/workflows/review-gate.yml](.github/workflows/review-gate.yml) for the few lines that add it to a repository.

This repository runs on itself: its own [factory.yml](factory.yml) and gate judge its pull requests.

The automatic reviewer. An optional station that does the review: when a change's tests pass, a model reads it against your standard and records pass or findings. It uses your own Claude subscription or API key, has no tools, and by default leaves high-risk changes to a person. A scheduled health check opens an issue when its credential stops working. See [docs/AUTOMATIC_REVIEWER.md](docs/AUTOMATIC_REVIEWER.md).

The record of every step. One command writes out what happened to each change (opened, reviewed, sent back, a person needed, merged) as events, from what GitHub already holds. Test results are not in it yet. See [docs/EVENTS.md](docs/EVENTS.md).

## Setting it up

Most people will set this up with an agent. Two guides are written for that, and a person can follow them too:

1. [Readiness check](docs/READINESS.md): what a repository needs before a factory can run on it, most of which is the project's own (its tests above all). It reads, changes nothing, and ends in a report saying which stage the repository can reach today.
2. [Setup guide](docs/SETUP.md): stage 1, step by step, with the three decisions that belong to the owner marked. Starter files are in [starter/](starter/), and `python3 -m factory.preview` shows what a set of rules would have said about changes that have already merged.

To run the tests: `python3 -m pip install --require-hashes --only-binary :all: -r requirements.txt`, then `python3 -m unittest discover -s tests`.

## The idea in one picture

```
            shared belt                       production lines
┌────────────┬─────────┬──────┐         ┌─ api ──── staging ─ gate ─ shipping ─ evaluate ─▶ live
│ production │ testing │  QA  │─ merge ─┼─ website ─ publish ───────────────────────────────▶ live
└─────▲──────┴─────────┴──┬───┘  sort   └─ cli ───── publish ───────────────────────────────▶ live
      └──── rework ───────┘
```

## Vocabulary

| Term | Meaning |
|---|---|
| Factory | An organisation or product: all of its repositories together |
| Building | One repository |
| Production line | One release path to one destination, such as an API to production or a package to a registry. A building can hold several |
| Station | A step on a line: testing, QA, staging, release gate, shipping, evaluate |
| Change | A pull request moving along the shared belt |
| Sorter | The moment of merge, when a change is split onto each line it feeds |
| Shipment | A release on one line. It can carry several changes |
| Gate | The station that decides whether a shipment may leave without a person |
| Hold | A shipment waiting for a person, with a stated reason |

## The four contracts

Everything else is built against these, so they are specified separately from any code.

| Contract | What it defines | Status |
|---|---|---|
| [Event format](spec/event-format.md) | One record for each thing that happens on a line | Draft 0.1. Written out for a change and its review: [docs/EVENTS.md](docs/EVENTS.md) |
| [Factory file](spec/factory-file.md) | The `factory.yml` that describes a building's lines, stations and rules | Draft 0.1 |
| Station interface | How a step joins a line and answers pass, send back or hold | Not yet written |
| Platform adapter | Deploy to staging, promote, check health, roll back | Not yet written |

An example factory file is in [examples/factory.yml](examples/factory.yml).

## What comes next

1. The "a person must act" convention for anything that stops a line.
2. The release gate reading `factory.yml`.
3. Events for the checks station and for the stations after a merge, and somewhere to send them. (A change and its review have them: [docs/EVENTS.md](docs/EVENTS.md).)
4. The station interface and the first platform adapter.

## Contributing

The specs are drafts and will change. Open an issue to discuss a change to either one before sending a pull request.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
