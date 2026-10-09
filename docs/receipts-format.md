# The receipts format

*Design document — `receipts.jsonl`, version 1, and how receipts meet the
derived events, as decided in ADR-0013 of the project's register. This
page is the specification a reader of their own receipts needs; the
reasoning is in the decision.*

**Status:** the file, the `vledger receipt` verbs, `derive match`, the
matching inside L1 and receipt entry in Home Assistant (ADR-0015) exist.

## The file

```
<base>/vehicle-<subject>/receipts.jsonl
```

One file per vehicle, next to `l0/` and apart from it. JSON Lines, UTF-8,
one JSON object per line, appended whole and flushed; a torn last line is
skipped on reading, as in L0. Nothing in it is ever rewritten.

Every line carries:

| key | meaning |
|---|---|
| `v` | schema version, integer; this page defines 1 |
| `t` | when the receipt was entered: UTC, ISO 8601, milliseconds, `Z`. For the record only — matching never reads it |
| `kind` | `refuelling`, `charging` or `cancel` |
| `subject` | the vehicle |
| `id` | a UUID minted when the receipt is written; never entered, never reused |

A reader skips a line of an unknown kind, and refuses a version newer
than its own.

## The three kinds

**`refuelling`**

```json
{"v":1,"t":"2026-10-12T18:03:11.020Z","kind":"refuelling","subject":"a7c1…",
 "id":"5b0e…","anchor":"2026-10-12T16:40:00.000Z","exact":false,
 "quantity_l":41.37,"total_price":72.36,"unit_price":1.749,"full":true,
 "place":"motorway services","fuel":"E10","note":null}
```

`quantity_l` (positive) and `full` are required, and at least one of
`total_price` and `unit_price`. `place`, `fuel` and `note` are free text
or `null`.

**`charging`**

```json
{"v":1,"t":"…","kind":"charging","subject":"a7c1…","id":"9d42…",
 "anchor":"2026-10-14T09:10:00.000Z","exact":false,
 "energy_kwh":11.8,"total_price":6.49,"place":null,"provider":"roaming card","note":null}
```

`energy_kwh` (positive) and `total_price` are required; `place`,
`provider` and `note` optional.

**`cancel`**

```json
{"v":1,"t":"…","kind":"cancel","subject":"a7c1…","id":"…","cancels":"5b0e…","note":"double entry"}
```

Values are numbers in the unit their key names. Prices are amounts in
Home Assistant's currency, like tariffs; a receipt names none of its own.

## Anchor time

`anchor` is when the refuelling or charge happened, as the receipt says.
Entered from a detected event, it is that event's `start` exactly, and
`exact` is `true`; typed freely, `exact` is `false`.

## Corrections and cancellations

A receipt is never edited. A **correction** is a complete receipt of the
same kind, with a new `id` and `"replaces": "<id>"`; a **cancellation**
is a `cancel` line naming the receipt in `cancels`. Either may name only a
*current* receipt — one that no line replaces or cancels — so the history
of a receipt is a chain. The receipts that count are the current ones,
which follows from the file's content and not from its order. A cancelled
receipt is not revived; it is entered again.

## Entering

Three ways write the same line through the same functions: the
`vledger receipt` verbs, the Home Assistant actions and the integration's
dashboard form ([user guide](user-guide.md#receipts-in-home-assistant)).
The actions take the verbs' options under the same names without the
dashes, plus `config_entry_id` for the vehicle, and answer with the line
written:

| Verb | Action | Fields beyond the verb's options |
|---|---|---|
| `receipt add refuelling` | `vledger.add_refuelling_receipt` | `full: true\|false` for `--full`/`--partial` |
| `receipt add charging` | `vledger.add_charging_receipt` | — |
| `receipt cancel UUID` | `vledger.cancel_receipt` | `receipt` for the positional UUID |

Exactly one of `anchor` and `from_candidate`. Times in an action are the
instance's local time unless they carry a zone; the line holds UTC. A
refusal writes nothing and says why — the same reasons the verbs give.

## Matching

Matching runs on every derivation, separately for refuellings and
charging sessions, and depends on nothing but the current receipts and
the detected events:

1. A refuelling receipt meets refuellings; a charging receipt meets every
   charging session, those at a configured charge point included.
2. The distance from an anchor to an event is 0 inside `[start, end]`,
   else to the nearer end.
3. An `exact` receipt meets the event starting at its anchor. If none
   does any more, it is matched like any other.
4. Every other receipt meets the nearest remaining event within the
   matching tolerance (`matching_tolerance_s`, default 6 h).
5. A receipt with two nearest events, and receipts sharing their nearest
   event, are **ambiguous**: nothing is assigned, and the events name the
   receipts that compete for them.
6. A receipt that meets nothing is an event of its own.

`vledger derive match` prints this pairing, one line per receipt.

## What it does to L1

The keys a receipt adds to a refuelling or charging event are in
[l1-format.md](l1-format.md#receipts-in-events). Every change to
`receipts.jsonl` changes its hash in the manifest, so the next derivation
rebuilds L1.
