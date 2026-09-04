# What broke and how I fixed it

Graded deliverable — Razorpay judges "failure recovery". Entries are appended the
moment something breaks. Format: symptom → cause → fix → the test that now guards it.

---

## 1. Daily settlements netted negative (generator, step 5)

**Symptom.** The first full generator run died on its own assertion:
`settlement on 2026-06-11 nets -3614347 paise`.

**Cause.** Not an arithmetic bug — a modelling error. The config asked for 600
payments and 340 refund events, a 57% refund rate. No merchant has that. On any
given day the refund debits outran the payment credits, so the batch payout came
out negative, which is impossible: Razorpay never claws money back through a
settlement, and refunds are paid from the merchant balance.

**Fix.** Raised `n_payments` to 3,400 against 340 refund events, a ~10% refund
rate. The graded population is still ~380 labelled refunds; the extra payments
are realistic context that make the settlement arithmetic possible.

**Guard.** `test_refund_rate_is_realistic` asserts the rate stays between 2% and
25%. `_group_into_settlements` keeps the hard assertion, so a bad config fails
loudly at generation instead of producing data the engine could never reconcile.

---

## 2. September settlements had refund debits but no payment credits

**Symptom.** Same assertion, moved to `2026-09-03`, after fix 1.

**Cause.** Payments were generated only up to `period_end` (31 August), but
refunds and their deductions continue into September up to `as_of`. Every
September settlement day therefore held debits with no offsetting credits.

**Fix.** Payments are now generated through `as_of`, not `period_end`. The
merchant keeps trading after the reporting month closes. Refund *events* are
still drawn only from payments created on or before `period_end`, so the graded
population stays tied to August activity.

**Guard.** `test_settlement_control_totals_tie` checks every batch's
`Σcredit − Σdebit` equals its payout and that no payout is negative.

This is the same class of mistake as the pull-window trap in CLAUDE.md §12: a
window that ends at the period boundary is always too narrow, because money
keeps moving after the period does.

---

## 3. Two test assertions were wrong, not the code (step 1)

**Symptom.** Two failures in the first test run: a UTC month-boundary test and a
rounding test.

**Cause.** Both were my errors in the tests. IST is UTC+5:30, so the date flips
for times *before* 05:30 IST, not after 23:00 as I had written — 31 Aug 23:30
IST is still 31 Aug in UTC. And my chosen partial-refund split happened to round
correctly by luck, so it did not demonstrate the failure it claimed to.

**Fix.** Rewrote the boundary test around 1 Sep 02:00 IST, which really is 31 Aug
in UTC. Replaced the rounding example with a ₹1,000 payment split three ways,
where naive rounding overshoots the parent fee by one paisa and largest-remainder
allocation does not.

**Lesson.** A green test proves nothing if the assertion encodes the wrong
expectation. Both tests now fail if the library regresses, which they previously
would not have.
