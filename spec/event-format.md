# Event format

Draft 0.1. This will change.

A factory writes one event for each thing that happens to a change or a shipment. Dashboards, history stores and reports read these events and nothing else.

## Rules

- **One JSON object per event.** In a file or a stream, one object per line (JSON Lines).
- **Append only.** An event is never edited or deleted. A correction is a new event.
- **Repeats are harmless.** Every event has a unique `id`. A consumer that sees the same `id` twice keeps one.
- **Times are UTC**, in RFC 3339 form with a `Z`.
- **Unknown fields are ignored.** A consumer must not fail on a field it does not recognise.
- **The lists of types and outcomes are closed.** A new one needs a new spec version. Anything else goes in `data`.

## Fields

```jsonc
{
  "spec": "factory.event/0.1",
  "id": "01JDQ8Z4M6T3V9K2N5X7B1C8RW",   // unique for this event
  "time": "2026-01-15T12:16:00Z",       // when it happened
  "recorded": "2026-01-15T12:16:02Z",   // when it was written down
  "factory": "example",
  "building": "example/shop",           // the repository
  "line": null,                         // null on the shared belt, before the sorter
  "change": { "number": 12, "commit": "3f9c2ab1…" },
  "station": "qa",
  "type": "station.finished",
  "outcome": "sent_back",
  "actor": { "kind": "agent", "id": "reviewer" },
  "reason": { "code": "review.findings", "text": "1 blocking finding" },
  "evidence": "https://github.com/example/shop/pull/12#issuecomment-1",
  "data": {}
}
```

| Field | Required | Notes |
|---|---|---|
| `spec` | yes | `factory.event/` followed by the version |
| `id` | yes | Any unique string. A sortable id such as a ULID is recommended |
| `time` | yes | When the thing happened, not when it was noticed |
| `recorded` | no | When the event was written. Differs from `time` for backfilled history |
| `factory`, `building` | yes | |
| `line` | for line events | The line's `id` from the factory file. `null` before the sorter |
| `change` | for change events, and for station events before the sorter | `number` is required. `commit` is the head commit the event is about. A station event on a line is about a shipment and may carry no `change` |
| `shipment` | for shipment events | An id for the release, unique within the line |
| `carries` | for shipment events | The change numbers the shipment contains |
| `station` | for station events | The station's name from the factory file |
| `type` | yes | See below |
| `outcome` | on `station.finished` | See below |
| `actor` | no | `kind` is `agent`, `person` or `system` |
| `reason` | on a hold, a send-back or a rollback | `code` is for machines, `text` is one plain sentence for people |
| `evidence` | no | A link to the run, comment or issue that shows it |
| `data` | no | Extensions. Each writer uses its own key, such as `{"acme.scan": {…}}` |

## Types

| Type | Meaning | Also carries |
|---|---|---|
| `change.opened` | A change appears on the shared belt | |
| `change.updated` | The change has a new head commit | |
| `change.closed` | The change was abandoned without merging | |
| `station.entered` | Work starts at a station | |
| `station.finished` | Work ends at a station | `outcome` |
| `change.merged` | The change leaves the shared belt | |
| `change.sorted` | The change is split onto lines | `lines`, a list of line ids. Empty when there is nothing to ship |
| `shipment.held` | The gate wants a person | `reason`, `carries` |
| `shipment.started` | A release leaves | `shipment`, `carries`, `actor` |
| `shipment.landed` | The release is live | `shipment` |
| `shipment.rolled_back` | The previous version was put back | `shipment`, `reason` |
| `action.needed` | A person must act before anything moves | `assignees` (may be empty when nobody is named), `reason`; `change` and `station` when it is about one change at one station |
| `action.cleared` | They did, or it no longer applies | The same `change` and `station` as the `action.needed` it answers |

`python3 -m factory.events check` reads events and says which of them lack a required field or use a type or outcome that is not listed here.

## Outcomes

`pass`, `sent_back`, `failed`, `held`, `skipped`, `error`.

- `sent_back` means a station returned the change to production with findings. It is recorded every time, so a change that goes round review three times leaves three events.
- `error` means the station could not decide. Nothing moves on an error.

## Example: one change, start to finish

Fields that repeat are left out to keep the example readable.

```jsonl
{"time":"12:05:07Z","type":"change.opened","change":{"number":12}}
{"time":"12:05:07Z","type":"station.entered","station":"testing"}
{"time":"12:16:00Z","type":"station.finished","station":"qa","outcome":"sent_back","reason":{"code":"review.findings"}}
{"time":"12:24:53Z","type":"change.updated","change":{"number":12,"commit":"3f9c2ab1"}}
{"time":"12:41:13Z","type":"station.finished","station":"qa","outcome":"pass"}
{"time":"12:41:49Z","type":"change.merged"}
{"time":"12:41:49Z","type":"change.sorted","lines":["api","website"]}
{"time":"12:44:43Z","type":"shipment.landed","line":"website","shipment":"w-212","carries":[12]}
{"time":"12:45:15Z","type":"shipment.held","line":"api","carries":[11,12],"reason":{"code":"risk.high","text":"Touches data deletion"}}
{"time":"13:03:53Z","type":"shipment.started","line":"api","shipment":"a-97","carries":[11,12,13,14],"actor":{"kind":"person"}}
{"time":"13:09:26Z","type":"shipment.landed","line":"api","shipment":"a-97"}
{"time":"13:58:58Z","type":"station.finished","line":"api","station":"evaluate","outcome":"pass"}
```

## Open questions

- **Orders across buildings.** A piece of work can need changes in two repositories. There is no field yet that links them.
- **Identity.** `actor` assumes the agent that wrote a change can be told apart from the agent that reviewed it.
- **Transport.** This spec defines the event, not how it is delivered or stored. For a change's own life and the review station, the factory writes events out from what GitHub already holds, on request ([the record of every step](../docs/EVENTS.md)); it does not yet write the checks station's, and nothing sends events anywhere yet. That writer records what the gate decided, so a review the gate never ruled on (its commit had already been replaced) is not in its output, although a send-back is meant to be recorded every time.
- **Pairing.** A `station.finished` is not always preceded by a `station.entered` for the same station and commit, and a reader must not rely on one.
