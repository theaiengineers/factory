# Factory

An open framework for running a repository as a software factory.

AI agents can now write most of a change. What is still missing is the line the change travels down afterwards: the tests, the independent review, the trial run, the decision about whether it may be released without a person, the release itself and the watch that follows. Factory describes that line in one file, enforces it, and records every step so that people can see what happened.

**Status: draft specifications only.** Nothing here runs yet. The two specs below are being extracted from a setup that already runs in production, and the engine will follow them into this repository.

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
| [Event format](spec/event-format.md) | One record for each thing that happens on a line | Draft 0.1 |
| [Factory file](spec/factory-file.md) | The `factory.yml` that describes a building's lines, stations and rules | Draft 0.1 |
| Station interface | How a step joins a line and answers pass, send back or hold | Not yet written |
| Platform adapter | Deploy to staging, promote, check health, roll back | Not yet written |

An example factory file is in [examples/factory.yml](examples/factory.yml).

## What comes next

1. The independent-review gate and the "a person must act" convention, which are already independent of any one project.
2. Risk rules and the release gate reading `factory.yml` instead of tables in code.
3. An event writer at each station.
4. The station interface and the first platform adapter.

## Contributing

The specs are drafts and will change. Open an issue to discuss a change to either one before sending a pull request.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
