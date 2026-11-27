# Factory file

Draft 0.1. This will change.

Each building (repository) has one `factory.yml` at its root. It says which production lines the building has, what feeds each one, which stations a change passes through, and when a release must wait for a person.

A complete example is in [../examples/factory.yml](../examples/factory.yml).

## Top level

```yaml
spec: factory/0.1
factory: example          # the factory this building belongs to
building: example/shop    # this repository
branch: main              # the branch that releases
```

## `intake`: the shared belt

Every change passes these stations, in order, before it can merge.

```yaml
intake:
  - station: testing
    uses: checks
    require: [unit, integration]
  - station: qa
    uses: independent-review
    standard: docs/REVIEW_STANDARD.md
    skip_when_only: [docs/**, "**/*.md"]
```

| Key | Meaning |
|---|---|
| `station` | The name used in events and on dashboards |
| `uses` | What performs the step. Built in: `checks` (named CI checks must pass) and `independent-review` (a reviewer other than the author must record a pass for the head commit) |
| `require` | For `checks`: the check names that must pass |
| `standard` | For `independent-review`: the document reviewers follow |
| `skip_when_only` | The station is skipped when a change touches nothing outside these paths |

## `risk`: what needs extra care

Risk is defined once and used twice: by QA, to decide how careful a review must be, and by release gates, to decide whether a shipment may leave unattended.

```yaml
risk:
  - area: payments
    why: moves money; a mistake charges real customers
    paths: [api/billing/**, scripts/*stripe*]
```

`why` is required. It is shown to the reviewer and to the person a release is held for.

## `lines`: the production lines

One entry for each place the building releases to.

```yaml
lines:
  - id: api
    feeds: [api/**, migrations/**]
    adapter: aws-lambda
    stations:
      - staging: { check: smoke }
      - gate:
          unattended: true
          hold_when:
            - risk: high
            - changed: [migrations/**]
            - changed_release_path: true
            - open_action: release-failed
          wait_while: previous_watch_running
      - shipping
      - evaluate: { watch: 50m, compare: 7d, on_breach: rollback }
```

| Key | Meaning |
|---|---|
| `id` | The line's name in events |
| `feeds` | Paths that put a merged change on this line. A change can feed several lines, or none |
| `adapter` | The platform adapter that deploys, promotes, checks health and rolls back |
| `stations` | In order. A station is a name alone or a name with settings |

### The gate

| Key | Meaning |
|---|---|
| `unattended` | `true` lets a shipment leave without a person unless a `hold_when` rule matches. `false` holds every shipment |
| `hold_when: risk` | Hold when anything since the last shipment touches a `risk` area |
| `hold_when: changed` | Hold when anything since the last shipment touches these paths |
| `hold_when: changed_release_path` | Hold when the factory's own release code or this file changed |
| `hold_when: open_action` | Hold while an `action.needed` of this kind is open |
| `wait_while` | Wait, without holding, while the condition is true |

A gate judges everything since the last shipment on its line, not only the newest merge, because a shipment releases all of it.

A held shipment always raises an `action.needed` event naming what is held, why, and the one thing a person must do.

### Evaluate

| Key | Meaning |
|---|---|
| `watch` | How long to watch after a shipment lands |
| `compare` | The earlier period used as the baseline |
| `on_breach` | `rollback` or `report` |

## `people`

```yaml
people:
  action_needed: [octocat]
```

Who is told when a line stops for a person.

## `events`

```yaml
events:
  - sink: https://events.example.com/factory
```

Where events are sent. Each sink receives every event in the [event format](event-format.md).

## `extensions`: third-party stations

```yaml
extensions:
  - station: licence-scan
    uses: acme/licence-scan@1
    on: intake
    may: [send_back]
```

| Key | Meaning |
|---|---|
| `on` | `intake` or a line id |
| `may` | What the station is allowed to do: `send_back`, `hold`. A station cannot hold a release unless `hold` is listed |

## Open questions

- **Rules that paths cannot express.** For example, a very large file where risk depends on which function changed. This needs a small rule language or a way to call a script.
- **Ordering between lines.** A change that feeds two lines may need one released before the other.
- **Several buildings.** Settings shared by a whole factory, such as `people`, should not have to be repeated in every repository.
