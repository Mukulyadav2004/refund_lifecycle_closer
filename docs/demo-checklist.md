# Demo checklist

Run before recording. Everything in §1–§2 I have verified on this machine and in
a clean clone; §3 is visual and **only you can check it** — I have no browser.

---

## 1. Machine checks

Each line states the expected result. Anything else is a stop.

```bash
make test
```
- [ ] `310 passed`. Not "310 passed, 2 skipped" — the two node-dependent web
      tests skip only where node is absent, and node is present here.

```bash
make data && make close && make eval
```
- [ ] `make data` prints the manifest and ends in `written`
- [ ] `N_in 380 == 279 closed + 49 open + 49 exception + 3 rejected  ->  HOLDS`
- [ ] `unexplained 0 (must be 0)`
- [ ] `match_rate_strict 85.06% (279/328)`
- [ ] `FALSE AUTO-MATCH RATE 0.00% (0/52)` and `including OPEN 0.00% (0/101)`
- [ ] `leakage_bps 236`
- [ ] every one of the nine codes shows `F1 1.000`
- [ ] duplicate sensitivity: `R=30.00% (3/10)` at 1800s, `100.00% (10/10)` at 86400s
- [ ] exits 0 (`echo $?`)

```bash
ls -la out/
```
- [ ] `report.md`, `results.jsonl`, `exceptions.csv`, `run.log`, `evaluation.json`
- [ ] `wc -l out/results.jsonl` is `380`
- [ ] `wc -l out/exceptions.csv` is `50` (49 + header)

**Determinism** — the claim judges are most likely to test:
```bash
shasum -a 256 data/synthetic/* | shasum -a 256
make data
shasum -a 256 data/synthetic/* | shasum -a 256
```
- [ ] identical hashes

**Clean clone** — the state a judge cloning your repo will be in:
```bash
git clone <repo> /tmp/rlc && cd /tmp/rlc
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest && .venv/bin/python -m rlc.server
```
- [ ] tests pass with no `PYTHONPATH` set
- [ ] the server generates `data/synthetic/` on first boot (it is gitignored)
- [ ] the numbers match the ones above exactly

---

## 2. API checks

With `make serve` running on :8000.

```bash
curl -s localhost:8000/healthz
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/static/app.js
curl -s -o /dev/null -w "%{http_code}\n" "localhost:8000/static/../config.yaml"
```
- [ ] `{"ok": true, "refunds": 380}`
- [ ] `200`, `200`, then **`404`** — path traversal must be refused

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"duplicate_window_seconds":1800}' localhost:8000/api/rerun \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['codes'].get('DUPLICATE_SUSPECT'),d['states'])"
```
- [ ] prints `3 {'CLOSED_MATCHED': 286, ..., 'EXCEPTION': 42, ...}` — then re-run
      at `86400` and confirm it returns to `10` and `279/49/49/3` before recording

---

## 3. Dashboard walkthrough — visual, only you can check

Open http://localhost:8000 and step through every view.

**Overview**
- [ ] green banner, the identity equation reads correctly
- [ ] four state cards: 279 / 49 / 49 / 3
- [ ] leg bars render; leg 4 says **evidenced only** and is a different colour
- [ ] settlement control shows unexplained **0 p** in green
- [ ] clicking an exception-code row jumps to Exceptions filtered by that code

**Exceptions**
- [ ] 49 rows, highest exposure first (a `REFUND_PLUS_CHARGEBACK` at ₹3,438.00 top)
- [ ] clicking a row opens the drawer; Esc and the backdrop close it
- [ ] drawer shows legs, the evidence JSON, and an explanation with its source
- [ ] a `DUPLICATE_SUSPECT` row shows the amber `human · 0.7` pill

**All refunds**
- [ ] 380 total; the state and code filters and the id search all work

**Accuracy**
- [ ] confusion matrix is diagonal (green on the diagonal, no red)
- [ ] **drag the slider to ~30 min and release** — the panel re-runs on its own
- [ ] the live panel changes: flagged 10 → 3, recall 100% → 30%, exceptions 49 → 42
- [ ] the fixed three-window table below does **not** change (it is labelled as fixed)
- [ ] drag back to 24 h and confirm it returns to 10 / 100% / 49

**Leakage & timing**
- [ ] leakage ₹10,424.41 = ₹1,589.69 GST + ₹8,834.72 MDR, 236 bps
- [ ] the histogram draws four bars; the `4 wd` bar is amber (past threshold)

**Assumptions**
- [ ] every row reads as an assumption
- [ ] `DUPLICATE_SUSPECT` is the only bolded HEURISTIC

**Report**
- [ ] renders fully — headings, ~18 tables, the fenced identity block
- [ ] does **not** hang (this was a real bug; the fix is tested, but look anyway)

**Responsiveness**
- [ ] narrow the window to phone width — the sidebar goes horizontal, nothing overflows

---

## 4. Deployment

- [ ] Railway build succeeds; logs show the manifest then `ready — 380 refunds`
- [ ] `/healthz` returns 200 at the public URL
- [ ] the deployed numbers match local exactly (same seed, same everything)
- [ ] leave `llm.enabled: false` — the free-tier Gemini key 429s after ~12 calls,
      and template mode is deterministic
- [ ] open the public URL on your phone once — judges may

---

## 5. Questions judges will ask, and where the answer is

| Question | Where |
|---|---|
| "Is this just an LLM wrapper?" | Assumptions view — 8 of 9 codes arithmetic, 1 heuristic; the model only writes prose |
| "How do you know it works?" | Accuracy view, then the slider — the metric moves |
| "Why a 24-hour duplicate window?" | Drag the slider. 30 min loses 70% of recall |
| "100% looks too good" | Say it first: it is agreement with our own generator. The report says so, and `match_rate_strict` is 85% |
| "What if the money never left?" | Overview → settlement-leg vs evidence-leg failures, never summed |
| "Did you verify the customer got paid?" | No — leg 4 is evidenced by an ARN, never verified. Lead with this |
| "What broke?" | `docs/what-broke.md`, 14 entries. The ground-truth one (entry 4) is the best story |

---

## 6. Decide before recording

- [ ] **The `as_of` clamp.** 27 refunds sit on 4 September and 13% of the month
      reads as `OPEN`. Labels are correct either way — this is realism, not
      correctness. Either fix the clamp or have a one-line answer ready.
- [ ] **Rotate the Gemini key** after the event; it has been pasted into a chat.
- [ ] **`match_rate_strict` is 85%, not 100%.** Lead with 49 exceptions found.
