# 5-minute pitch — shot list and script

Structure follows SPEC.md §10. Timings total 300s with ~15s of slack.

Recording setup: dashboard on :8000 already loaded on **Overview**, a terminal
with the repo open, `out/report.md` in a second tab. Re-run the duplicate window
back to 24h before you start, or your opening numbers will be wrong.

---

## 0:00 – 0:35 · The problem (35s)

**On screen:** Razorpay's own Refund entity doc, `"status": "processed"` beside
`"arn": null`. Then your dashboard's leg bars.

> Razorpay tells you a refund is `processed`. That word means the gateway
> finished — not that the customer has their money, and not that it ever left
> your settlement.
>
> Their own documentation says a refund usually reaches `processed` *before* the
> bank reference arrives. Their own sample response shows `processed` with a null
> ARN.
>
> So between "processed" and the customer's bank there are two more places money
> goes missing.
>
> Razorpay does expose the links — a refund carries its settlement id, a recon row
> carries its dispute id. What it does not give you is one view that puts them
> together **across a period boundary**. Their own writing calls that the top
> source of unresolved reconciliation variances: a March refund reduces your
> April settlement. That join is the gap this closes.

**Why this opening works:** it is their documentation, not your opinion, and it
reframes reconciliation from a chore into a trust problem.

---

## 0:35 – 1:05 · The claim and the shape of the answer (30s)

**On screen:** the four leg bars on Overview.

> Four legs. A refund is initiated. The gateway marks it processed. It is
> deducted from a settlement. And it lands in the customer's bank.
>
> We verify three of those against records. The fourth we only *evidence*, with
> the acquirer reference number — because an ARN proves a bank reference was
> issued, not that an account was credited.
>
> **Three legs verified, one evidenced.** Every other tool I looked at claims
> four. That distinction is the whole product.

**Do not** say "four-leg lifecycle" anywhere. The repo has a test that fails if
that phrase appears in the README or the report.

---

## 1:05 – 1:50 · Architecture (45s)

**On screen:** the provenance table from README §2, then the join graph.

> Five sources. Four are Razorpay endpoints — payments, refunds, disputes, and
> the settlement recon endpoint. Every field in our synthetic data is shaped
> exactly like the documented response, so any number traces back to a real
> endpoint.
>
> The fifth is not Razorpay: the merchant's own returns ledger. Razorpay only
> enforces that refunds never exceed the captured amount. It has no idea what a
> specific return was *worth*. Refund twenty thousand rupees for a two thousand
> rupee item and every Razorpay check passes. Only the merchant's ledger catches
> that — which is why one of our nine exception codes needs this file.

**Join graph on screen:**
```
refunds.id          → recon.entity_id (type='refund')   1:0..n   leg 3
refunds.payment_id  → payments.id                        n:1      fee, tax
refunds.payment_id  → disputes.payment_id                1:0..n   chargeback
refunds.payment_id  → returns_ledger.payment_id          1:0..n   expected amount
recon.settlement_id → settlements.id                     n:1      control total
```

> Two join levels. `refund_id` binds refunds to settlement rows. `payment_id`
> binds that result to everything else.
>
> One thing that matters: the recon endpoint is keyed on **settlement** date, not
> transaction date. A refund created on 30 August is deducted in September. Pull
> a single month and you manufacture failures that never happened. We have a test
> that measures exactly that.

---

## 1:50 – 3:20 · Live run (90s) — the core of the video

**Beat 1 — Overview (20s).**

> 380 refunds for August. 279 closed clean, 49 still in flight, 49 exceptions,
> 3 rejected as bad input.
>
> That top line is an assertion, not a summary: N-in equals the sum of the four
> states, checked on every run, and the run fails if it doesn't hold. Zero silent
> drops is a property, not a hope.
>
> Below it, the settlement control total. Every paisa of difference between what
> settlements debited and what refunds were worth is explained by a specific
> exception code. Unexplained: zero.

**Beat 2 — Leakage (20s).** Switch to Leakage & timing.

> Every refund also leaks. Razorpay does not return the MDR or the GST on it.
> This month: ₹10,424 lost on ₹4.4 lakh refunded — 236 basis points on sales that
> earned nothing.
>
> That number is not typed in anywhere. It is 2% plus 18% GST falling out of the
> allocation. And the fee comes off the *payment* row — the refund row carries
> `fee = 0` by definition. Getting that backwards is the single most common way
> to be wrong here.

**Beat 3 — Exceptions (20s).** Switch to Exceptions.

> 49 refunds need a human, sorted by what they cost. Not one of these is a single
> lookup — each needs a join across three sources and, for most of them, across a
> month boundary.

**Beat 4 — the slider (30s).** Switch to Accuracy. This is your strongest beat.

> Eight of our nine rules are arithmetic. Exactly one is a heuristic — duplicate
> detection — and it is the only one that asks for a human.
>
> Someone will ask why we chose a 24-hour window. So don't answer, show them.

Drag to 30 minutes, release.

> The whole month just re-closed. Server-side, 380 refunds, forty milliseconds.
> Flagged drops from 10 to 3. Recall falls to 30%. Seven refunds move from
> exception to closed. Precision stays at 100% — narrowing the window removes
> pairs, it never invents them.
>
> That is the threshold doing real work, and it is why we publish it at three
> windows instead of asserting one.

Drag back to 24h before moving on.

---

## 3:20 – 4:00 · One exception, end to end (40s)

**On screen:** top row of Exceptions — the `REFUND_PLUS_CHARGEBACK` at ₹3,438.
Open the drawer.

> The most expensive finding this month. We refunded ₹1,719. The customer then
> won a chargeback on the same payment. The merchant paid twice — total exposure
> ₹3,438.
>
> Only that ordering is reachable in practice: Razorpay restricts refunds while a
> dispute is under investigation, so refund-then-chargeback is the shape we model
> and the only one the engine looks for.
>
> The drawer shows every leg, the evidence, and a written explanation with a
> recommended action.

**Then the AI boundary — say this explicitly, it is a judged criterion:**

> No model touches classification, matching, arithmetic, dates or state
> decisions. All of that is integer-paise code. The model writes prose about
> exceptions the engine already classified.
>
> And two guards sit between it and this screen. Every number it emits must
> appear in the facts object it was given. And a claim guard rejects anything
> asserting four verified legs, or that the customer was credited, or stating a
> flagged duplicate as fact.
>
> Both guards were earned. On its second live output the model wrote "all four
> settlement legs have been verified" — in a sentence with no numbers in it at
> all. The number guard couldn't see it. That's why the second one exists.

---

## 4:00 – 4:35 · What broke (35s)

**On screen:** `docs/what-broke.md`.

> Fourteen entries. The one worth telling:
>
> My ground truth was wrong. The generator correctly modelled refunds still in
> flight — no settlement row, no ARN yet — but never revisited their label, so 31
> refunds with one verified leg were labelled as three plus bank evidence.
>
> Every metric still printed. Every one of them flattered the engine. I would
> have reported 92% accuracy while the engine was right and the answer key was
> wrong.
>
> That is the dangerous class of bug in anything that scores itself, and it is
> why the evaluator is the only module in the repo permitted to read the labels —
> enforced by a test that parses the other modules and fails if any of them so
> much as names the file.

---

## 4:35 – 5:00 · Limits and close (25s)

> What this does not do. Leg four is evidenced, never verified — we do not know
> the customer was paid, and we say so on every screen.
>
> The accuracy numbers score against labels our own generator produced. That
> measures internal consistency, not truth. The number I'd actually stand behind
> is the strict match rate: 85%, and 49 exceptions found — each one a join across
> three sources that no single view assembles for you.
>
> Deterministic where determinism is possible. A model only where prose is the
> product. And a guard on the model, because it was wrong on its second try.

---

## Numbers to have memorised

| | |
|---|---|
| 380 refunds | 279 closed / 49 open / 49 exception / 3 rejected |
| Leakage | ₹10,424.41 on ₹4,41,736 refunded — **236 bps** |
| Split | ₹1,589.69 GST + ₹8,834.72 MDR |
| Strict match rate | 85.06% (279/328) |
| False auto-match | 0 of 52, and 0 of 101 including OPEN |
| Duplicates | 10 at 24h → 3 at 30min, recall 100% → 30% |
| Throughput | ~195,000 records/sec, whole pipeline ~40 ms |
| Tests | 310, no network |
| Top exception | ₹3,438 exposure, refund + won chargeback |

## Phrases to avoid

- "four-leg lifecycle" / "all four legs verified" — the claim is 3 + 1
- "orphan" — say `NO_PARENT_PAYMENT` or `NEVER_DEDUCTED`
- "we reconcile Razorpay data" — say *synthetic data shaped exactly like the
  documented API responses*
- "100% accurate" without the caveat in the same breath
- **"the dashboard doesn't show you this"** — it does show refund→settlement in
  Refund Details, and recon rows carry `dispute_id`. The defensible gap is the
  absence of a *unified, cross-period* view, not absence of the data. Overstating
  this is the one thing a Razorpay judge can counter on the spot.
- **quoting a specific 400 for "refund blocked by open dispute"** — the
  restriction is real, but that exact error string is not in the public error
  tables. Describe the behaviour, don't quote a code you can't show them.
