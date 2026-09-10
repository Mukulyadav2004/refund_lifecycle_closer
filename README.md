# Refund Lifecycle Closer

Razorpay AI Buildathon — AI Finance Controller track.

Closes out a month of refunds and tells you which ones actually finished. I built
it because "processed" doesn't mean what I assumed it meant.

video explanation - https://www.loom.com/share/288489d300694c08b356802e55693480

## The thing this is about

Razorpay marks a refund `processed` when the gateway is done with it. That is not
the same as the customer having their money, and it's not the same as the money
leaving your settlement.

Their own docs say a refund usually reaches `processed` *before* the bank
reference arrives, and their sample response shows `"status": "processed"` sitting
next to `"arn": null`.

So there are two more places money goes missing after your dashboard says done.

| Leg | Meaning | How we establish it |
|---|---|---|
| 1 initiated | the refund exists | Refunds API record |
| 2 gateway processed | Razorpay finished | `refund.status == "processed"` |
| 3 deducted from settlement | money left the payout | exactly one recon row, `debit == amount` |
| 4 landed in the customer's bank | — | **evidenced only**, by `acquirer_data.arn` |

Three verified, one evidenced. An ARN proves a reference number was issued for the
transfer, not that anyone's account was credited. We don't claim otherwise anywhere.

The other half of the problem is timing. A refund is deducted from a *later*
settlement, so an August refund reduces your September payout. Razorpay exposes
the links — a refund carries its settlement id, recon rows carry a dispute id —
but nothing assembles them into one view across a month boundary. Their own blog
calls that the top source of unresolved reconciliation variances.

## Run it

```bash
make install     # pyyaml + pytest, that's the whole dependency list
make data        # synthetic data + labels, seed 42
make close       # close every refund
make eval        # score against the labels, write out/report.md
make serve       # dashboard on localhost:8000
```

No Razorpay account, no API key, no network. `make all` runs the middle three.

Deploying: `Procfile`, `railway.json` and `nixpacks.toml` are in the repo. Point
Railway at it and go — `$PORT` comes from the environment, `/healthz` is the
health check, and the dataset generates itself on first boot.

## What it found

380 refunds, August 2026:

```
N_in 380 == 279 CLOSED_MATCHED + 49 OPEN + 49 EXCEPTION + 3 REJECTED_INPUT
```

That top line is an assertion, not a summary. It runs on every close and the run
fails if it doesn't hold, so nothing can be quietly dropped.

**49 refunds needed a human.** Seven never deducted from any settlement. Three
deducted twice. Five where the customer also won a chargeback on the same payment,
so the merchant paid twice. Eight where the refund didn't match what the return was
worth — Razorpay only checks a refund doesn't exceed the captured amount, it has no
idea what the item cost, so that one needs the merchant's own returns ledger.

**₹10,424.41 in fees you never get back**, on ₹4,41,736 refunded. That's 236 basis
points, split ₹1,589.69 GST and ₹8,834.72 MDR. The number isn't typed in anywhere;
it's 2% + 18% GST falling out of the allocation. Worth saying: the fee comes off the
*payment* row, because the recon refund row carries `fee = 0` by definition. Getting
that backwards is the easiest way to be wrong here, and `fee` already includes the
GST so you never add the two.

Strict match rate is **85.06% (279/328)**, excluding open and rejected refunds.
Throughput is around 195,000 records/second; the whole pipeline runs in ~40 ms.

`out/report.md` has the rest — confusion matrix, per-code precision and recall,
the sensitivity table, every assumption printed with its value.

## Where the data comes from

All synthetic, generated locally, byte-identical from seed 42. What makes it worth
anything is that every field is shaped exactly like the documented API response, so
you can trace any number back to a real endpoint.

| File | Endpoint |
|---|---|
| `payments.json` | `GET /v1/payments` |
| `refunds.json` | `GET /v1/refunds` |
| `disputes.json` | `GET /v1/disputes` |
| `settlements.json` | `GET /v1/settlements` |
| `settlement_recon.json` | `GET /v1/settlements/recon/combined` |
| `returns_ledger.csv` | **not Razorpay** — the merchant's own RMA export |

Swapping the generator for live test-mode calls should be a loader change rather
than an engine change. I haven't done it, so treat that as a claim, not a fact.

## Where the AI is, and isn't

Nothing about classification, matching, arithmetic, dates or state decisions goes
near a model. That's all integer-paise code. Eight of the nine exception codes are
pure arithmetic or evidence checks; exactly one is a heuristic (duplicate detection)
and it's the only one that attaches a confidence and asks for a human.

The model writes the prose explanation for an exception the engine already
classified. Provider is Gemini, over `urllib`, so it adds no dependency. It's off by
default and the whole thing runs in template mode with no API key.

Two guards sit between it and the report:

- every number it writes has to appear in the facts object it was given
- a claim guard blocks it from claiming the fourth leg is verified, from saying the
  customer was credited, or from stating a flagged duplicate as fact

Both got added because the model actually did those things. On its second live
output it wrote "All four settlement legs for the refund have been verified" — a
sentence with no numbers in it, so the first guard couldn't see it.

## What broke

Fourteen entries in [`docs/what-broke.md`](docs/what-broke.md), written as they
happened. The three that mattered:

**My test data was physically impossible.** 340 refunds against 600 payments is a
57% refund rate, and settlements kept going negative. I hunted for an arithmetic bug
for a while before working out the arithmetic was fine — no merchant refunds 57% of
revenue, so the debits outran the credits. Raised payments to 3,400.

**Then my answer key was wrong, which was worse.** The generator correctly modelled
refunds still in flight — no settlement row, no ARN — but never updated their label,
so 31 of them were marked as fully settled. The engine gave the right answer and got
scored 92%. Nothing crashed. If I'd trusted the score I'd have "fixed" the engine to
claim money had landed when it hadn't, which is the worst thing this tool could do.
Now only the grading module may open the labels file, enforced by a test that parses
the other modules and fails if any of them even names it.

**A guard that could be talked out of anything.** The number check stripped known
values from the model's output and objected to whatever digits were left — but a
bare "9" from a date erased every digit of an invented "999.99". Rewrote it to match
whole tokens.

## What it doesn't do

- Leg 4 is evidenced, never verified. We don't know the customer was paid.
- Accuracy is measured against labels this repo generated. That's internal
  consistency, not truth. The number I'd stand behind is the 85% strict match rate
  and the 49 exceptions found.
- Domestic INR only. Instant refunds off. Recon `transfer` and `adjustment` rows
  aren't generated.
- Refund status is modelled as `pending` / `processed` / `failed`. Razorpay's FAQ
  also mentions a `reversed` outcome; v1 doesn't handle it.
- Under ~100 refunds a month with no partials or disputes, the Razorpay dashboard is
  enough and this adds nothing.

## The rest

`SPEC.md` is the algorithm. `CLAUDE.md` is the working contract I held myself to.
`docs/what-broke.md` is the failure log. `docs/demo-checklist.md` is what I run
before recording.

310 tests, no network.
