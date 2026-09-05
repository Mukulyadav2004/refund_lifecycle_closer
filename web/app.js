/* Refund Lifecycle Closer — dashboard.
   Vanilla JS, no build step, no CDN: everything the page needs is served from
   this origin, so a fresh container is self-contained. */

const $ = (sel) => document.querySelector(sel);
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const state = { summary: null, accuracy: null, assumptions: null, current: "overview" };

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

const pill = (s) => `<span class="pill ${esc(s)}">${esc(s.replace(/_/g, " "))}</span>`;
const codePills = (codes) =>
  (codes || []).map((c) => `<span class="pill code">${esc(c)}</span>`).join("") || "<span class=muted>—</span>";
const ratio = (r) => r ? `${r.pct.toFixed(2)}% <span class="muted num">(${r.numerator}/${r.denominator})</span>` : "—";

/* ------------------------------------------------------------- overview */
function renderOverview() {
  const s = state.summary;
  const rates = s.rates;
  const identityOk = s.identity.holds;
  const cards = [
    { label: "Closed matched", value: s.states.CLOSED_MATCHED, sub: "all three legs verified, ARN present", cls: "green" },
    { label: "Open", value: s.states.OPEN, sub: "in flight — not yet judgeable", cls: "amber" },
    { label: "Exceptions", value: s.states.EXCEPTION, sub: "money needs a human", cls: "red" },
    { label: "Rejected input", value: s.states.REJECTED_INPUT, sub: "integrity failures, not merchant problems", cls: "" },
  ];
  view.innerHTML = `
    <div class="banner ${identityOk ? "" : "bad"}">
      <div class="tick">${identityOk ? "✓" : "!"}</div>
      <div>
        <b>N_in ${s.identity.n_in} = ${s.states.CLOSED_MATCHED} closed + ${s.states.OPEN} open
        + ${s.states.EXCEPTION} exception + ${s.states.REJECTED_INPUT} rejected</b><br>
        Zero silent drops: every refund resolves to exactly one closure state, asserted on every run.
      </div>
    </div>

    <div class="grid k4 section">
      ${cards.map((c) => `
        <div class="card stat ${c.cls}">
          <div class="label">${c.label}</div>
          <div class="value num">${c.value}</div>
          <div class="sub">${c.sub}</div>
        </div>`).join("")}
    </div>

    <div class="grid k4 section">
      <div class="card stat blue">
        <div class="label">Match rate (strict)</div>
        <div class="value num">${rates ? rates.match_rate_strict.pct.toFixed(2) + "%" : "—"}</div>
        <div class="sub">${rates ? `${rates.match_rate_strict.numerator}/${rates.match_rate_strict.denominator} — excludes OPEN and REJECTED` : "no ground truth"}</div>
      </div>
      <div class="card stat">
        <div class="label">Fee leakage</div>
        <div class="value num">${esc(s.leakage.total_inr)}</div>
        <div class="sub">${s.leakage.bps} bps of ${esc(s.leakage.refunded_inr)} refunded</div>
      </div>
      <div class="card stat red">
        <div class="label">False auto-match</div>
        <div class="value num">${rates ? rates.false_auto_match.pct.toFixed(2) + "%" : "—"}</div>
        <div class="sub">${rates ? `${rates.false_auto_match.numerator}/${rates.false_auto_match.denominator} seeded failures closed anyway` : "—"}</div>
      </div>
      <div class="card stat">
        <div class="label">Throughput</div>
        <div class="value num">${s.throughput.records_per_second.toLocaleString()}</div>
        <div class="sub">records/sec — full pipeline ${s.throughput.pipeline_ms} ms</div>
      </div>
    </div>

    <div class="grid k2 section">
      <div class="card">
        <h2>Lifecycle legs</h2>
        <p class="hint">Three verified against records. The fourth is evidenced by an ARN and never verified —
        Razorpay marks a refund <code>processed</code> before the ARN arrives.</p>
        <div class="legs">
          ${s.legs.map((l) => {
            const pctv = (l.count / s.identity.n_in) * 100;
            const ev = l.leg === 4;
            return `<div class="leg ${ev ? "evidenced" : ""}">
              <div class="n">${l.leg}</div>
              <div class="cap">${esc(l.name)}<br><em>${esc(l.status)}</em></div>
              <div class="bar"><i style="width:${pctv.toFixed(1)}%"></i></div>
              <div class="val num">${l.count}/${s.identity.n_in}</div>
            </div>`;
          }).join("")}
        </div>
        <table style="margin-top:16px">
          <tr><td>Settlement-leg failures <span class="muted">(money did not move)</span></td>
              <td class="right num">${s.leg_failures.settlement}</td></tr>
          <tr><td>Evidence-leg failures <span class="muted">(no bank reference)</span></td>
              <td class="right num">${s.leg_failures.evidence}</td></tr>
        </table>
        <p class="hint" style="margin:10px 0 0">Never summed. A refund with no ARN may well have reached
        the customer; one that never left the payout certainly did not.</p>
      </div>

      <div class="card">
        <h2>Settlement control total</h2>
        <p class="hint">Every paisa of difference between what settlements debited and what refunds were
        worth has to be explained by an exception code, or the report is understating something.</p>
        <table>
          <tr><td>Σ debit − Σ amount</td><td class="right num">${s.control.difference_paise.toLocaleString()} p</td></tr>
          <tr><td>explained by <code>SETTLEMENT_AMOUNT_DELTA</code></td><td class="right num">${s.control.amount_delta_paise.toLocaleString()} p</td></tr>
          <tr><td>explained by <code>DOUBLE_DEDUCTED</code></td><td class="right num">${s.control.double_deducted_paise.toLocaleString()} p</td></tr>
          <tr><td><b>unexplained</b></td>
              <td class="right num"><b style="color:${s.control.unexplained_paise === 0 ? "var(--green)" : "var(--red)"}">
              ${s.control.unexplained_paise} p</b></td></tr>
        </table>
        <h2 style="margin-top:22px">Data errors</h2>
        <p class="hint">Integrity failures are a data problem — usually a pull window that is too narrow.
        Counted on their own channel, excluded from the match-rate denominator, never mixed into exceptions.</p>
        <table>
          ${Object.entries(s.rejections).map(([k, v]) => `<tr><td><code>${esc(k)}</code></td><td class="right num">${v}</td></tr>`).join("")
            || '<tr><td class="muted">no rejections</td><td></td></tr>'}
          ${Object.entries(s.data_errors).filter(([, v]) => v > 0).map(([k, v]) => `<tr><td><code>${esc(k)}</code></td><td class="right num">${v}</td></tr>`).join("")}
        </table>
      </div>
    </div>

    <div class="section">
      <h2>Exception codes found</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Code</th><th>Count</th><th>Nature</th></tr></thead>
          <tbody>
            ${Object.entries(s.codes).map(([code, n]) => {
              const meta = (state.assumptions?.codes || []).find((c) => c.code === code);
              return `<tr class="clickable" data-code="${esc(code)}">
                <td><span class="pill code">${esc(code)}</span></td>
                <td class="num">${n}</td>
                <td class="muted">${esc(meta ? meta.nature : "")}</td></tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
    </div>`;

  view.querySelectorAll("[data-code]").forEach((row) =>
    row.addEventListener("click", () => go("exceptions", { code: row.dataset.code })));
}

/* ------------------------------------------------------------- records */
async function renderRecords(opts = {}, onlyExceptions = false) {
  const params = new URLSearchParams();
  if (onlyExceptions) params.set("state", "EXCEPTION");
  else if (opts.state) params.set("state", opts.state);
  if (opts.code) params.set("code", opts.code);
  if (opts.q) params.set("q", opts.q);
  params.set("limit", "200");

  view.innerHTML = `<div class="spinner">loading records…</div>`;
  const data = await api(`/api/records?${params}`);
  const codes = Object.keys(state.summary.codes);

  view.innerHTML = `
    <div class="card">
      <div class="controls">
        ${onlyExceptions ? "" : `
        <select id="f-state">
          <option value="">All states</option>
          ${["CLOSED_MATCHED", "OPEN", "EXCEPTION", "REJECTED_INPUT"].map((s) =>
            `<option value="${s}" ${opts.state === s ? "selected" : ""}>${s.replace(/_/g, " ")}</option>`).join("")}
        </select>`}
        <select id="f-code">
          <option value="">All codes</option>
          ${codes.map((c) => `<option value="${c}" ${opts.code === c ? "selected" : ""}>${c}</option>`).join("")}
        </select>
        <input type="search" id="f-q" placeholder="refund or payment id" value="${esc(opts.q || "")}">
        <span class="muted num">${data.total} record${data.total === 1 ? "" : "s"}${data.total > data.items.length ? ` — showing ${data.items.length}` : ""}</span>
      </div>
    </div>
    <div class="table-wrap section">
      <table>
        <thead><tr>
          <th>Refund</th><th>State</th><th>Codes</th>
          <th class="right">Amount</th><th class="right">Exposure</th><th class="right">Leakage</th>
          <th>Review</th>
        </tr></thead>
        <tbody>
          ${data.items.map((r) => `
            <tr class="clickable" data-id="${esc(r.refund_id)}">
              <td class="mono">${esc(r.refund_id)}</td>
              <td>${pill(r.closure_state)}</td>
              <td>${codePills(r.exception_codes.length ? r.exception_codes : r.open_reasons.length ? r.open_reasons : r.rejection_reasons)}</td>
              <td class="right num">${esc(r.amount_inr)}</td>
              <td class="right num">${r.exposure_paise ? esc(r.exposure_inr) : "<span class=muted>—</span>"}</td>
              <td class="right num">${esc(r.leakage_inr)}</td>
              <td>${r.needs_human_review ? `<span class="pill human">human · ${r.confidence}</span>` : "<span class=muted>—</span>"}</td>
            </tr>`).join("") || `<tr><td colspan="7" class="empty">nothing matches those filters</td></tr>`}
        </tbody>
      </table>
    </div>`;

  view.querySelectorAll("tr[data-id]").forEach((row) =>
    row.addEventListener("click", () => openDrawer(row.dataset.id)));
  const rerun = (extra) => renderRecords({ ...opts, ...extra }, onlyExceptions);
  $("#f-code")?.addEventListener("change", (e) => rerun({ code: e.target.value }));
  $("#f-state")?.addEventListener("change", (e) => rerun({ state: e.target.value }));
  $("#f-q")?.addEventListener("change", (e) => rerun({ q: e.target.value }));
}

/* -------------------------------------------------------------- drawer */
async function openDrawer(refundId) {
  const r = await api(`/api/record/${encodeURIComponent(refundId)}`);
  const legRow = (n, label, ok, evidenced) => `
    <div class="leg ${evidenced ? "evidenced" : ""}">
      <div class="n" style="${ok ? "" : "background:var(--grey-bg);color:var(--grey)"}">${n}</div>
      <div class="cap">${label}</div>
      <div class="val">${ok ? (evidenced ? "evidenced" : "verified") : "not established"}</div>
    </div>`;
  $("#drawer-body").innerHTML = `
    <h1 style="font-size:17px" class="mono">${esc(r.refund_id)}</h1>
    <p class="muted" style="margin:4px 0 18px">${pill(r.closure_state)} ${codePills(r.exception_codes)}</p>
    ${r.explanation ? `
      <div class="quote">
        ${esc(r.explanation)}
        <div class="action">→ ${esc(r.recommended_action)}</div>
        <div class="muted" style="font-size:11.5px;margin-top:8px">
          written by: ${esc(r.explanation_source)}${r.explanation_source === "template_after_rejection"
            ? " — the model's output failed a guard and was replaced" : ""}
        </div>
      </div>` : ""}
    <h2 style="font-size:14px;margin:22px 0 10px">Legs</h2>
    <div class="legs">
      ${legRow(1, "initiated", r.legs["1"], false)}
      ${legRow(2, "gateway processed", r.legs["2"], false)}
      ${legRow(3, "settlement deducted", r.legs["3"], false)}
      ${legRow(4, "bank credited", r.legs["4"], true)}
    </div>
    <h2 style="font-size:14px;margin:22px 0 10px">Record</h2>
    <dl class="kv">
      <dt>Payment</dt><dd class="mono">${esc(r.payment_id || "—")}</dd>
      <dt>Amount</dt><dd class="num">${esc(r.amount_inr)}</dd>
      <dt>Gateway status</dt><dd><code>${esc(r.status)}</code></dd>
      <dt>Method</dt><dd>${esc(r.method || "—")}</dd>
      <dt>Receipt</dt><dd class="mono">${esc(r.receipt || "none")}</dd>
      <dt>ARN</dt><dd class="mono">${esc(r.arn || "not yet issued")}</dd>
      <dt>Settlement</dt><dd class="mono">${esc(r.settlement_id || "—")}</dd>
      <dt>Settle lag</dt><dd>${r.settle_lag_wd === null ? "—" : r.settle_lag_wd + " working days"}</dd>
      <dt>Fee leakage</dt><dd class="num">${esc(r.leakage_inr)} <span class="muted">(${esc(r.leakage_gst_inr)} GST + ${esc(r.leakage_mdr_inr)} MDR)</span></dd>
      <dt>Exposure</dt><dd class="num">${r.exposure_paise ? esc(r.exposure_inr) : "—"}</dd>
      <dt>Open reasons</dt><dd>${codePills(r.open_reasons)}</dd>
      <dt>Timing</dt><dd>${(r.timing_flags || []).map((f) => `<span class="pill flag">${esc(f)}</span>`).join("") || "—"}</dd>
      <dt>Annotations</dt><dd>${codePills(r.annotations.concat(r.rejection_reasons))}</dd>
    </dl>
    ${Object.keys(r.evidence || {}).length ? `
      <h2 style="font-size:14px;margin:22px 0 10px">Evidence</h2>
      <pre class="evidence">${esc(JSON.stringify(r.evidence, null, 2))}</pre>` : ""}`;
  $("#drawer").hidden = false;
}

/* ------------------------------------------------------------ accuracy */
function renderAccuracy() {
  const a = state.accuracy;
  if (!a.available) { view.innerHTML = `<div class="card empty">${esc(a.reason)}</div>`; return; }
  const s = state.summary;
  const win = s.duplicate_window_seconds;
  view.innerHTML = `
    <div class="card">
      <h2>What this scores against</h2>
      <p class="hint">These figures compare the engine to labels this repository's own generator produced.
      They measure internal consistency between generator and engine — not accuracy against a real merchant's
      books. The two controls below are where the numbers demonstrably move.</p>
      <div class="grid k4" style="margin-top:6px">
        ${[["match_rate_strict", "Match rate (strict)"], ["match_rate_all", "Match rate (all)"],
           ["state_accuracy", "State accuracy"], ["exact_agreement", "Exact agreement"]]
          .map(([k, label]) => `<div class="stat"><div class="label">${label}</div>
            <div class="value num" style="font-size:22px">${s.rates[k].pct.toFixed(2)}%</div>
            <div class="sub num">${s.rates[k].numerator}/${s.rates[k].denominator}</div></div>`).join("")}
      </div>
    </div>

    <div class="grid k2 section">
      <div class="card">
        <h2>State confusion matrix</h2>
        <p class="hint">Rows are ground truth, columns are the engine.</p>
        <table class="matrix">
          <thead><tr><th>expected \\ engine</th>${a.states.map((x) => `<th class="right">${esc(x.slice(0, 9))}</th>`).join("")}</tr></thead>
          <tbody>
            ${a.states.map((exp) => `<tr><td><b>${esc(exp)}</b></td>${
              a.confusion[exp].map((n, i) => {
                const cls = a.states[i] === exp ? (n ? "diag" : "zero") : (n ? "off" : "zero");
                return `<td class="right num ${cls}">${n}</td>`;
              }).join("")}</tr>`).join("")}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h2>Duplicate window sensitivity</h2>
        <p class="hint"><code>DUPLICATE_SUSPECT</code> is the only non-arithmetic rule in the engine.
        Move the window and the whole pipeline re-runs — 380 refunds, live.</p>
        <div class="controls" style="margin-bottom:14px">
          <input type="range" id="win" min="600" max="259200" step="600" value="${win}">
          <span class="chip num" id="win-label">${(win / 3600).toFixed(1)} h</span>
          <button class="btn" id="win-run">Re-run</button>
        </div>
        <table>
          <thead><tr><th>window</th><th class="right">TP</th><th class="right">FP</th><th class="right">FN</th><th class="right">precision</th><th class="right">recall</th></tr></thead>
          <tbody>
            ${a.duplicate_sensitivity.map((d) => `<tr>
              <td class="num">${d.window_seconds >= 3600 ? (d.window_seconds / 3600) + " h" : (d.window_seconds / 60) + " min"}</td>
              <td class="right num">${d.tp}</td><td class="right num">${d.fp}</td><td class="right num">${d.fn}</td>
              <td class="right num">${d.precision.pct.toFixed(0)}%</td>
              <td class="right num">${d.recall.pct.toFixed(0)}%</td></tr>`).join("")}
          </tbody>
        </table>
        <p class="hint" style="margin-top:10px">Precision holds at 100% across all three: a narrower window
        removes pairs, it never invents them. The receipt clause is what keeps legitimate multi-partial
        refunds out of this list entirely.</p>
      </div>
    </div>

    <div class="section">
      <h2>Per exception code</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Code</th><th>Nature</th><th class="right">TP</th><th class="right">FP</th><th class="right">FN</th>
            <th class="right">Precision</th><th class="right">Recall</th><th class="right">F1</th></tr></thead>
          <tbody>
            ${a.codes.map((c) => `<tr>
              <td><span class="pill code">${esc(c.code)}</span></td>
              <td class="muted">${esc(c.nature)}</td>
              <td class="right num">${c.tp}</td><td class="right num">${c.fp}</td><td class="right num">${c.fn}</td>
              <td class="right num">${ratio(c.precision)}</td>
              <td class="right num">${ratio(c.recall)}</td>
              <td class="right num">${c.f1.toFixed(3)}</td></tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>`;

  const slider = $("#win"), label = $("#win-label");
  slider.addEventListener("input", () => {
    const v = Number(slider.value);
    label.textContent = v >= 3600 ? (v / 3600).toFixed(1) + " h" : Math.round(v / 60) + " min";
  });
  $("#win-run").addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "re-running…";
    await api("/api/rerun", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ duplicate_window_seconds: Number(slider.value) }),
    });
    await loadAll();
    renderAccuracy();
  });
}

/* -------------------------------------------------------- money & time */
function renderMoney() {
  const s = state.summary;
  const max = Math.max(...s.timing.histogram.map((h) => h.count), 1);
  view.innerHTML = `
    <div class="grid k2">
      <div class="card">
        <h2>Fee leakage</h2>
        <p class="hint">The recon <b>refund</b> row carries <code>fee = 0, tax = 0</code>. The money lost is the
        original <b>payment</b> row's fee, which is already GST-inclusive — so leakage is <code>fee</code>,
        never <code>fee + tax</code>. Pro-rated across a payment's non-failed refunds with largest-remainder
        allocation, so partials sum exactly to the parent fee.</p>
        <div class="grid k3" style="margin:4px 0 16px">
          <div class="stat"><div class="label">Total</div><div class="value num" style="font-size:21px">${esc(s.leakage.total_inr)}</div></div>
          <div class="stat"><div class="label">GST</div><div class="value num" style="font-size:21px">${esc(s.leakage.gst_inr)}</div></div>
          <div class="stat"><div class="label">MDR</div><div class="value num" style="font-size:21px">${esc(s.leakage.mdr_inr)}</div></div>
        </div>
        <table>
          <thead><tr><th>Method</th><th class="right">Refunds</th><th class="right">Refunded</th><th class="right">Leakage</th></tr></thead>
          <tbody>${s.leakage.by_method.map((m) => `<tr><td>${esc(m.method)}</td>
            <td class="right num">${m.records}</td><td class="right num">${esc(m.refunded_inr)}</td>
            <td class="right num">${esc(m.leakage_inr)}</td></tr>`).join("")}</tbody>
        </table>
        <p class="hint" style="margin-top:12px"><b>${s.leakage.bps} bps</b> of refunded principal — the
        2% + 18% GST base rate falling out of the allocation, not a number typed in.</p>
      </div>

      <div class="card">
        <h2>Settlement timing</h2>
        <p class="hint">Reported as a distribution, not asserted as an SLA — no settlement-side SLA for
        refunds is documented. <code>CROSS_PERIOD</code> is the binary IST month test and needs no assumption
        at all: it is exactly the "an August refund reduces September's settlement" problem.</p>
        <div class="hist">
          ${s.timing.histogram.map((h) => `
            <div class="col ${h.lag > s.timing.threshold_wd ? "late" : ""}">
              <b class="num">${h.count}</b>
              <i style="height:${(h.count / max) * 100}%"></i>
              <span>${h.lag} wd</span>
            </div>`).join("")}
        </div>
        <table style="margin-top:16px">
          <tr><td><code>CROSS_PERIOD</code></td><td class="right num">${s.timing.cross_period_count} refunds · ${esc(s.timing.cross_period_inr)}</td></tr>
          <tr><td><code>LATE_VS_THRESHOLD</code></td><td class="right num">${s.timing.late_count} (lag &gt; ${s.timing.threshold_wd} wd)</td></tr>
          <tr><td>Median settle lag</td><td class="right num">${s.timing.median_lag_wd} working day${s.timing.median_lag_wd === 1 ? "" : "s"}</td></tr>
          <tr><td>Measured on</td><td class="right num">${s.timing.measured} of ${s.identity.n_in} <span class="muted">(needs exactly one recon row)</span></td></tr>
        </table>
      </div>
    </div>

    <div class="section">
      <h2>Where the data comes from</h2>
      <div class="grid k3">
        ${Object.entries(s.sources).map(([k, v]) => `
          <div class="card stat"><div class="label">${esc(k.replace(/_/g, " "))}</div>
          <div class="value num" style="font-size:22px">${v.toLocaleString()}</div></div>`).join("")}
      </div>
    </div>`;
}

/* ---------------------------------------------------------- assumptions */
function renderAssumptions() {
  const a = state.assumptions, s = state.summary;
  view.innerHTML = `
    <div class="card">
      <h2>Assumptions, not facts</h2>
      <p class="hint">Every value below lives in <code>config.yaml</code>, is printed in the report, and is
      described as an assumption. The refund settlement lag is deliberately not hardcoded to "T+5 to T+7" —
      that figure comes from a marketing blog and conflates the customer-receipt SLA with the settlement clock.</p>
      <table>
        <thead><tr><th>Key</th><th class="right">Value</th><th>Why it is an assumption</th></tr></thead>
        <tbody>${a.assumptions.map((r) => `<tr><td><code>${esc(r.key)}</code></td>
          <td class="right num">${esc(r.value)}</td><td class="muted">${esc(r.note)}</td></tr>`).join("")}</tbody>
      </table>
    </div>

    <div class="section">
      <h2>Exception codes and their nature</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Code</th><th>Nature</th><th>Fields used</th><th class="right">Found</th></tr></thead>
          <tbody>${a.codes.map((c) => `<tr>
            <td><span class="pill code">${esc(c.code)}</span></td>
            <td>${c.nature.includes("HEURISTIC") ? `<b style="color:var(--amber)">${esc(c.nature)}</b>` : esc(c.nature)}</td>
            <td class="muted mono">${esc(c.fields)}</td>
            <td class="right num">${s.codes[c.code] || 0}</td></tr>`).join("")}</tbody>
        </table>
      </div>
      <p class="hint" style="margin-top:12px">Eight of nine are arithmetic or evidence. Exactly one is a
      heuristic, and it is the only one that attaches a confidence and routes to a human.</p>
    </div>

    <div class="card section">
      <h2>Where the model sits</h2>
      <p class="hint">No model touches classification, matching, arithmetic, date logic or state decisions —
      all of that is deterministic integer-paise code. The model writes prose about exceptions the engine has
      already classified. Two guards stand between it and this dashboard: every number in its output must
      appear in the facts object, and a claim guard rejects any assertion of four verified legs, any claim the
      customer was credited, and any unhedged statement of a finding marked for human review. A rejected
      explanation falls back to that code's template.</p>
      <table>
        <tr><td>Model</td><td class="right">${s.explanations.model_enabled ? `<code>${esc(s.explanations.model || "")}</code>` : "template mode — no API key needed"}</td></tr>
        ${Object.entries(s.explanations.counts).map(([k, v]) => `<tr><td>Explanations written by <code>${esc(k)}</code></td><td class="right num">${v}</td></tr>`).join("")}
        ${s.explanations.provider_errors ? `<tr><td>Provider errors <span class="muted">(each fell back, none dropped)</span></td><td class="right num">${s.explanations.provider_errors}</td></tr>` : ""}
      </table>
    </div>`;
}

/* -------------------------------------------------------------- report */
async function renderReport() {
  view.innerHTML = `<div class="spinner">rendering report…</div>`;
  const { markdown } = await api("/api/report");
  view.innerHTML = `<div class="report">${md(markdown)}</div>`;
}

/* A deliberately small markdown renderer — the report is generated by us, so it
   only needs headings, tables, fences, lists and inline code. */
function md(src) {
  const lines = src.split("\n");
  const out = [];
  let i = 0;
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<i>$2</i>");
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
    } else if (/^\|/.test(line) && /^\|[\s:|-]+\|$/.test(lines[i + 1] || "")) {
      const cells = (r) => r.split("|").slice(1, -1).map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && /^\|/.test(lines[i])) body.push(cells(lines[i++]));
      out.push(`<table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${
        body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
    } else if (/^#{1,4} /.test(line)) {
      const level = line.match(/^#+/)[0].length;
      out.push(`<h${level}>${inline(line.replace(/^#+ /, ""))}</h${level}>`);
      i++;
    } else if (/^[-*] /.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*] /.test(lines[i])) items.push(lines[i++].replace(/^[-*] /, ""));
      out.push(`<ul>${items.map((t) => `<li>${inline(t)}</li>`).join("")}</ul>`);
    } else if (line.trim() === "") {
      i++;
    } else {
      const buf = [];
      while (i < lines.length && lines[i].trim() !== "" && !/^[#|`\-*]/.test(lines[i])) buf.push(lines[i++]);
      out.push(`<p>${inline(buf.join(" "))}</p>`);
    }
  }
  return out.join("\n");
}

/* --------------------------------------------------------------- shell */
const TITLES = {
  overview: ["Overview", "One month of refunds, closed and reconciled"],
  exceptions: ["Exceptions", "Sorted by exposure — the list a controller works from"],
  records: ["All refunds", "Every record, including the ones that closed cleanly"],
  accuracy: ["Accuracy", "Measured against seeded ground truth, with the caveats attached"],
  money: ["Leakage & timing", "Fees Razorpay never returns, and when the money actually moved"],
  assumptions: ["Assumptions", "Every threshold, stated as an assumption"],
  report: ["Report", "The full generated report.md"],
};

async function go(name, opts = {}) {
  state.current = name;
  document.querySelectorAll(".nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name));
  const [title, sub] = TITLES[name];
  $("#page-title").textContent = title;
  $("#page-sub").textContent = sub;
  if (name === "overview") renderOverview();
  else if (name === "exceptions") await renderRecords(opts, true);
  else if (name === "records") await renderRecords(opts, false);
  else if (name === "accuracy") renderAccuracy();
  else if (name === "money") renderMoney();
  else if (name === "assumptions") renderAssumptions();
  else if (name === "report") await renderReport();
}

async function loadAll() {
  const [summary, accuracy, assumptions] = await Promise.all([
    api("/api/summary"), api("/api/accuracy"), api("/api/assumptions"),
  ]);
  state.summary = summary;
  state.accuracy = accuracy;
  state.assumptions = assumptions;
  $("#chip-period").textContent = `${summary.period.start} → ${summary.period.end} IST`;
  $("#chip-seed").textContent = `as of ${summary.period.as_of} · seed ${summary.period.seed}`;
  const idc = $("#chip-identity");
  idc.textContent = summary.identity.holds
    ? `identity holds · ${summary.identity.n_in} refunds`
    : "IDENTITY BROKEN";
  idc.className = "chip " + (summary.identity.holds ? "ok" : "bad");
  $("#nav-exceptions").textContent = summary.states.EXCEPTION;
  $("#nav-records").textContent = summary.identity.n_in;
}

document.querySelectorAll(".nav-item").forEach((b) =>
  b.addEventListener("click", () => go(b.dataset.view)));
$("#drawer-close").addEventListener("click", () => ($("#drawer").hidden = true));
$("#drawer").addEventListener("click", (e) => {
  if (e.target === $("#drawer")) $("#drawer").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#drawer").hidden = true;
});

loadAll().then(() => go("overview")).catch((err) => {
  view.innerHTML = `<div class="card empty">could not load: ${esc(err.message)}</div>`;
});
