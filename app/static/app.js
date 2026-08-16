/* FINCORE — Financial Operation Chaos Lab (presentation layer)
 *
 * THE ONE RULE IN THIS FILE
 * -------------------------
 * Nothing here invents a result. Every number, state, timeline entry and coin
 * is read out of the response to POST /api/demo/run. The animation controls the
 * ORDER and PACE at which already-computed facts appear; it never manufactures
 * a fact, and it never runs ahead of the backend.
 *
 * Plain-English labels are chosen here, but only ever keyed off a backend event
 * `type` that actually occurred. If the backend stops emitting an event, its
 * line disappears from the page rather than being drawn anyway.
 */
'use strict';

const QS = new URLSearchParams(location.search);
const STEP_MS = Number(QS.get('step')) || 620;
const RECORDING = QS.get('recording') === '1';

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rupees = (paise) =>
  '₹' + (paise / 100).toLocaleString('en-IN', { maximumFractionDigits: 2 });

/* ------------------------------------------------------------------ modes */

const MODES = [
  {
    id: 'response_loss',
    label: 'Lose the response',
    note: 'The provider completes the refund, then the response never arrives. The caller has to decide what to do without knowing whether the money moved.',
    hint: 'Compares a naive retry against Financial Operation Core.',
    split: true,
  },
  {
    id: 'worker_crash',
    label: 'Crash the worker',
    note: 'The provider completes the refund and the process dies before it can record that. A different worker has to pick the operation back up.',
    hint: 'Recovery is driven by a recover() sweep over expired leases.',
    split: false,
  },
  {
    id: 'concurrency',
    label: 'Fire 20 callers',
    note: 'Many workers submit the same refund at the same instant, the way a retry storm arrives after a timeout.',
    hint: 'Concurrent callers, not attempts — one caller wins the lease.',
    split: false,
  },
  {
    id: 'intent_conflict',
    label: 'Change the amount',
    note: 'Retry the same refund job, but ask for a different amount. Same operation reference, different financial intent.',
    hint: 'Try ₹200, then try ₹100.',
    split: false,
  },
];

/* Plain English first. Keyed on backend event types; an entry is only ever
 * rendered when the backend actually emitted that event. */
const PLAIN = {
  operation_started:          ['Refund job created', 'info'],
  provider_key_created:       ['Retry identity generated', 'info'],
  provider_invoked:           ['Provider called', 'neutral'],
  provider_effect_created:    ['Provider created the refund', 'good'],
  response_lost:              ['Response disappeared', 'bad'],
  operation_unknown:          ["The runtime doesn't know the outcome yet", 'warn'],
  retry_started:              ['Same refund job is retried', 'info'],
  provider_key_reused:        ['Same provider identity reused', 'good'],
  provider_replayed_original: ['Provider returned the original refund', 'good'],
  operation_reconciled:       ['Original result recovered', 'good'],
  operation_succeeded:        ['Refund settled', 'good'],
  duplicate_effect_created:   ['Provider created a SECOND refund', 'bad'],
  worker_crashed:             ['Worker crashed', 'bad'],
  lease_expired:              ['Its lock expired', 'warn'],
  recovery_started:           ['A new worker took over', 'info'],
  operation_loaded:           ['Same refund job loaded', 'good'],
  execution_owner_elected:    ['One caller won the right to execute', 'good'],
  intent_conflict:            ['Refused — different amount', 'bad'],
  operation_replayed:         ['Same amount, nothing new happened', 'good'],
  provider_untouched:         ['The provider was never called', 'good'],
};

const BADGE = {
  operation_unknown: ['UNKNOWN', 'warn'],
  intent_conflict: ['CONFLICT', 'bad'],
  operation_succeeded: ['SUCCEEDED', 'good'],
  duplicate_effect_created: ['DUPLICATE', 'bad'],
  worker_crashed: ['EXECUTING', 'warn'],
};

let mode = MODES[0];
let busy = false;
let skipped = false;
let retryAmountPaise = 20000;
let callers = 20;
let lastResult = null;

/* Set when the reviewer scrolls by hand during a run. Auto-scrolling to the
 * result is a convenience, not a right to take the viewport off someone. */
let userScrolled = false;
addEventListener(
  'wheel', () => { if (busy) userScrolled = true; }, { passive: true }
);
addEventListener(
  'touchmove', () => { if (busy) userScrolled = true; }, { passive: true }
);
addEventListener('keydown', (e) => {
  if (busy && ['PageDown', 'PageUp', 'ArrowDown', 'ArrowUp', 'End', 'Home'].includes(e.key)) {
    userScrolled = true;
  }
});

/* ------------------------------------------------------------------ controls */

function renderModes() {
  const host = $('modes');
  host.innerHTML = '';
  MODES.forEach((m, i) => {
    const b = document.createElement('button');
    b.className = 'mode';
    b.type = 'button';
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(m.id === mode.id));
    b.dataset.mode = m.id;
    b.innerHTML =
      `<span class="mode-idx">0${i + 1}</span><span class="mode-name"></span>`;
    b.querySelector('.mode-name').textContent = m.label;
    b.addEventListener('click', () => selectMode(m));
    host.appendChild(b);
  });
}

function renderModeControl() {
  const host = $('mode-control');
  host.innerHTML = '';

  if (mode.id === 'concurrency') {
    const seg = document.createElement('div');
    seg.className = 'seg';
    seg.innerHTML = '<span class="seg-label">Callers</span>';
    for (const n of [2, 5, 10, 20]) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = String(n);
      b.setAttribute('aria-pressed', String(n === callers));
      b.addEventListener('click', () => { callers = n; renderModeControl(); });
      seg.appendChild(b);
    }
    host.appendChild(seg);
  }

  if (mode.id === 'intent_conflict') {
    const wrap = document.createElement('div');
    wrap.className = 'amount-field';
    wrap.innerHTML =
      '<label for="amt">Retry the same job as</label>' +
      '<span class="amount-input"><span>₹</span>' +
      '<input id="amt" type="number" min="1" max="1000" step="1" inputmode="numeric"></span>' +
      '<span class="amount-hint" id="amt-hint">Original was ₹100 · try 1–1000</span>';
    host.appendChild(wrap);

    const input = wrap.querySelector('#amt');
    input.value = String(Math.round(retryAmountPaise / 100));
    input.addEventListener('input', () => {
      const hint = $('amt-hint');
      const v = Number(input.value);
      if (!Number.isInteger(v) || v < 1 || v > 1000) {
        hint.textContent = 'Enter a whole number of rupees between 1 and 1000';
        hint.dataset.error = '1';
        $('run').disabled = true;
      } else {
        retryAmountPaise = v * 100;
        hint.textContent =
          v === 100 ? 'Same as the original — watch what does NOT happen' : 'Original was ₹100';
        delete hint.dataset.error;
        $('run').disabled = busy;
      }
    });
  }
}

function selectMode(m) {
  if (busy) return;
  mode = m;
  for (const b of document.querySelectorAll('.mode')) {
    b.setAttribute('aria-selected', String(b.dataset.mode === m.id));
  }
  $('mode-note').textContent = m.note;
  $('cta-hint').textContent = m.hint;
  renderModeControl();
  clearResult();
}

function clearResult() {
  $('canvas').hidden = true;
  $('verdict').hidden = true;
  $('flow').innerHTML = '';
  $('answer').innerHTML = '';
  $('stage-viz').innerHTML = '';
  $('money-lanes').innerHTML = '';
  $('scoreboard').innerHTML = '';
  $('internals-body').innerHTML = '';
  $('internals').open = false;
  setStatus('Ready', '');
  lastResult = null;
}

function setStatus(text, tone) {
  const el = $('subject-status');
  el.textContent = text;
  if (tone) el.dataset.tone = tone; else delete el.dataset.tone;
}

/* -------------------------------------------------------------------- steps */

function stepEl(ev) {
  const [plain, tone] = PLAIN[ev.type] || [ev.title, ev.tone];
  const li = document.createElement('li');
  li.className = 'step';
  li.dataset.tone = tone || 'neutral';
  li.dataset.type = ev.type;

  const mark = document.createElement('div');
  mark.className = 'step-mark';
  const dot = document.createElement('span');
  dot.className = 'step-dot';
  mark.appendChild(dot);

  const text = document.createElement('div');
  text.className = 'step-text';

  const line = document.createElement('div');
  line.className = 'step-plain';
  line.textContent = plain;

  const badge = BADGE[ev.type];
  if (badge) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.dataset.tone = badge[1];
    b.textContent = badge[0];
    line.appendChild(b);
  }
  text.appendChild(line);

  // The technical line is the backend's own `short`, never re-derived here.
  if (ev.short && ev.short !== plain) {
    const tech = document.createElement('div');
    tech.className = 'step-tech';
    tech.textContent = ev.short;
    text.appendChild(tech);
  }

  li.appendChild(mark);
  li.appendChild(text);
  return li;
}

function makeSpine(kind, title) {
  const wrap = document.createElement('div');
  wrap.className = 'spine';
  wrap.dataset.kind = kind;
  const h = document.createElement('div');
  h.className = 'spine-title';
  h.textContent = title;
  const ol = document.createElement('ol');
  ol.className = 'steps';
  wrap.appendChild(h);
  wrap.appendChild(ol);
  return { wrap, list: ol };
}

/* ============================================================ visualisations
 *
 * Every element below is built from a verified backend field. Marker counts,
 * owner counts, effect tokens and "not called" markers are read from the
 * response; none of them is a constant chosen to look right. If the backend
 * returned 5 callers, five markers are drawn.
 */

const STAGE_MS = 260;  // beat between causal stages; whole diagram lands ~1.5s

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/** One simulated financial effect. Deliberately a single reusable token: the
 *  crash diagram carries ONE of these across the worker transition, because
 *  one is what the runtime produced. */
function effectToken(paise, label) {
  const t = el('span', 'effect-token');
  t.appendChild(el('span', 'effect-dot'));
  t.appendChild(el('span', 'effect-amt', rupees(paise)));
  if (label) t.appendChild(el('span', 'effect-note', label));
  return t;
}

function stage(delayIndex) {
  const s = el('div', 'vstage');
  s.style.animationDelay = `${delayIndex * STAGE_MS}ms`;
  return s;
}

/* ------------------------------------------------- 1. answer, before all else */
function renderAnswer(result) {
  const host = $('answer');
  host.innerHTML = '';
  const q = el('div', 'answer-q', 'Did another financial effect happen?');
  host.appendChild(q);

  const row = el('div', 'answer-row');

  const card = (label, yes, detail) => {
    const c = el('div', 'answer-card');
    c.dataset.yes = yes ? '1' : '0';
    c.appendChild(el('span', 'answer-label', label));
    c.appendChild(el('span', 'answer-verdict', yes ? 'Yes — a second effect' : 'No second effect'));
    c.appendChild(el('span', 'answer-detail', detail));
    return c;
  };

  if (result.experiment === 'response_loss') {
    const n = result.naive, f = result.fincore;
    row.appendChild(card('Without a durable operation', n.financial_effects > 1,
      `${n.financial_effects} refunds · ${rupees(n.amount_refunded_paise)} of effects`));
    row.appendChild(card('With Financial Operation Core', f.financial_effects > 1,
      `${f.financial_effects} refund · ${rupees(f.amount_refunded_paise)} of effects`));
  } else if (result.experiment === 'intent_conflict') {
    row.appendChild(card('The changed retry',
      result.financial_effects_caused_by_retry > 0,
      `${result.provider_calls_caused_by_retry} extra provider calls · ` +
      `${result.financial_effects_caused_by_retry} extra effects`));
  } else {
    row.appendChild(card('After the failure', result.financial_effects > 1,
      `${result.financial_effects} refund · ${result.provider_invocations} provider invocations`));
  }

  host.appendChild(row);
}

/* --------------------------------------------------- 2. worker crash diagram */
function renderCrashDiagram(result) {
  const box = el('div', 'viz viz-crash');
  const [workerA, workerB] = result.workers;
  const refund = result.refund_ids[0];

  // Stage 1 — worker A reaches the provider, then dies.
  const s1 = stage(0);
  s1.appendChild(el('span', 'viz-actor', workerA));
  const s1b = el('div', 'viz-body');
  s1b.appendChild(el('div', 'viz-line', 'Provider effect created'));
  // THE token. Exactly one exists in this diagram, for the whole diagram.
  s1b.appendChild(effectToken(result.amount_refunded_paise ?? 10000, refund));
  s1b.appendChild(el('div', 'viz-crash-mark', 'Crash'));
  s1.appendChild(s1b);
  box.appendChild(s1);

  // Stage 2 — what survived the process.
  const s2 = stage(1);
  s2.appendChild(el('span', 'viz-actor viz-actor-quiet', 'Durable operation'));
  const s2b = el('div', 'viz-body');
  s2b.appendChild(el('div', 'viz-line', 'Still exists after the process died'));
  s2b.appendChild(el('div', 'viz-sub',
    `${result.operation_ref} · left in ${result.state_after_crash}`));
  s2.appendChild(s2b);
  box.appendChild(s2);

  // Stage 3 — recovery. Explicitly invoked, and labelled as such.
  const s3 = stage(2);
  s3.appendChild(el('span', 'viz-actor', workerB));
  const s3b = el('div', 'viz-body');
  s3b.appendChild(el('div', 'viz-line', 'Recovery started'));
  if (result.provider_key_reused) {
    s3b.appendChild(el('div', 'viz-sub', 'Same provider identity reused'));
  }
  if (result.replayed) {
    s3b.appendChild(el('div', 'viz-line viz-ok', `Original refund replayed — ${refund}`));
  }
  s3.appendChild(s3b);
  box.appendChild(s3);

  // Stage 4 — the count that matters.
  const s4 = stage(3);
  s4.className += ' vstage-final';
  const tally = el('div', 'viz-tally');
  tally.appendChild(el('b', null, String(result.worker_count)));
  tally.appendChild(el('span', null, result.worker_count === 1 ? 'worker' : 'workers'));
  tally.appendChild(el('i', null, '·'));
  tally.appendChild(el('b', 'viz-ok', String(result.financial_effects)));
  tally.appendChild(el('span', null,
    result.financial_effects === 1 ? 'financial effect' : 'financial effects'));
  s4.appendChild(tally);
  box.appendChild(s4);

  return box;
}

/* ------------------------------------------------- 3. concurrent callers viz */
function renderSwarm(result) {
  const box = el('div', 'viz viz-swarm');

  // Exactly `callers` markers -- the backend's number, whatever it is.
  const approach = el('div', 'swarm-row');
  const owner = result.execution_owners;
  for (let i = 0; i < result.callers; i++) {
    const d = el('span', 'caller');
    // The first marker is the one that crosses; the rest are turned away.
    if (i < owner) d.dataset.owner = '1';
    else d.dataset.turned = '1';
    d.style.animationDelay = `${i * 22}ms`;
    approach.appendChild(d);
  }
  box.appendChild(approach);

  const b1 = el('div', 'viz-boundary');
  b1.appendChild(el('span', 'viz-boundary-label', 'Financial Operation Core'));
  box.appendChild(b1);

  // The marker that got through. Deliberately NOT class `caller`: the caller
  // markers must number exactly what the backend reported, and exactly one of
  // them carries data-owner. Counting this echo as a caller would inflate both.
  const crossed = el('div', 'swarm-row swarm-crossed');
  for (let i = 0; i < owner; i++) {
    const d = el('span', 'caller-crossed');
    d.dataset.crossed = '1';
    crossed.appendChild(d);
  }
  crossed.appendChild(el('span', 'swarm-caption',
    `${owner} execution owner · ${result.turned_away} turned away`));
  box.appendChild(crossed);

  const b2 = el('div', 'viz-boundary viz-boundary-provider');
  b2.appendChild(el('span', 'viz-boundary-label', 'Provider'));
  box.appendChild(b2);

  const out = el('div', 'swarm-row swarm-out');
  out.appendChild(el('span', 'swarm-caption',
    `${result.provider_invocations} provider call`));
  if (result.financial_effects > 0) {
    out.appendChild(effectToken(result.amount_refunded_paise ?? 10000, result.refund_ids[0]));
  }
  box.appendChild(out);

  const head = el('div', 'viz-headline');
  head.appendChild(el('b', null, String(result.callers)));
  head.appendChild(el('span', null, result.callers === 1 ? 'caller.' : 'callers.'));
  head.appendChild(el('b', 'viz-ok', String(owner)));
  head.appendChild(el('span', null, 'execution owner.'));
  head.appendChild(el('b', 'viz-ok', String(result.financial_effects)));
  head.appendChild(el('span', null,
    result.financial_effects === 1 ? 'financial effect.' : 'financial effects.'));
  box.appendChild(head);

  return box;
}

/* ------------------------------------------------- 4. intent conflict boundary */
function renderConflictDiagram(result) {
  const box = el('div', 'viz viz-conflict');

  const lane = (label, amount, delayIndex, blocked, note, tone) => {
    const s = stage(delayIndex);
    s.className += ' viz-lane';
    s.appendChild(el('span', 'viz-amount', rupees(amount)));
    s.appendChild(el('span', 'viz-arrow', '→'));
    s.appendChild(el('span', 'viz-gate', 'FinCore'));
    s.appendChild(el('span', blocked ? 'viz-stop' : 'viz-arrow', blocked ? '✕' : '→'));
    const end = el('span', 'viz-endpoint', blocked ? 'Provider not called' : 'Provider');
    if (blocked) end.dataset.blocked = '1';
    s.appendChild(end);
    const n = el('span', 'viz-lane-note', note);
    if (tone) n.dataset.tone = tone;
    s.appendChild(n);
    s.insertBefore(el('span', 'viz-lane-label', label), s.firstChild);
    return s;
  };

  // The original DID reach the provider -- that is why an effect exists.
  box.appendChild(lane('Original', result.original_amount_paise, 0, false,
    `${result.refund_ids[0]} created`, 'good'));

  // The retry did not. Which reason applies is read from the backend.
  const blockedNote = result.conflict
    ? 'Different financial intent — refused'
    : 'Same intent — already completed, nothing new';
  box.appendChild(lane('Retry', result.retry_amount_paise, 1, true, blockedNote,
    result.conflict ? 'bad' : 'good'));

  const s = stage(2);
  s.className += ' vstage-final';
  const tally = el('div', 'viz-tally');
  tally.appendChild(el('b', 'viz-ok', String(result.provider_calls_caused_by_retry)));
  tally.appendChild(el('span', null, 'extra provider calls'));
  tally.appendChild(el('i', null, '·'));
  tally.appendChild(el('b', 'viz-ok', String(result.financial_effects_caused_by_retry)));
  tally.appendChild(el('span', null, 'extra financial effects'));
  s.appendChild(tally);
  box.appendChild(s);

  return box;
}

function renderStageViz(result) {
  const host = $('stage-viz');
  host.innerHTML = '';
  if (result.experiment === 'worker_crash') host.appendChild(renderCrashDiagram(result));
  else if (result.experiment === 'concurrency') host.appendChild(renderSwarm(result));
  else if (result.experiment === 'intent_conflict') host.appendChild(renderConflictDiagram(result));
}

/* ------------------------------------------------------------------- money */

function moneyLane(label, effects, paise, dup) {
  const lane = document.createElement('div');
  lane.className = 'money-lane';

  const l = document.createElement('div');
  l.className = 'money-lane-label';
  l.textContent = label;
  lane.appendChild(l);

  const coins = document.createElement('div');
  coins.className = 'coins';
  for (let i = 0; i < effects; i++) {
    const c = document.createElement('span');
    c.className = 'coin';
    if (dup && i > 0) c.dataset.dup = '1';
    c.style.animationDelay = `${i * 130}ms`;
    c.textContent = rupees(paise / Math.max(effects, 1));
    coins.appendChild(c);
  }
  if (effects === 0) {
    const none = document.createElement('span');
    none.className = 'money-total';
    none.textContent = 'no financial effect';
    coins.appendChild(none);
  }
  lane.appendChild(coins);

  const total = document.createElement('div');
  total.className = 'money-total';
  if (dup) total.dataset.dup = '1';
  total.innerHTML = `Total effect <b>${rupees(paise)}</b>`;
  lane.appendChild(total);
  return lane;
}

/* --------------------------------------------------------------- scoreboard */

function counterRow(label, value, tone) {
  const row = document.createElement('div');
  row.className = 'counter';
  const dt = document.createElement('dt');
  dt.textContent = label;
  const dd = document.createElement('dd');
  dd.textContent = String(value);
  if (tone) dd.dataset.tone = tone;
  row.appendChild(dt);
  row.appendChild(dd);
  return row;
}

function scoreLane(kind, head, num, numTone, cap, state, stateTone, counters) {
  const lane = document.createElement('div');
  lane.className = 'score-lane';
  lane.dataset.kind = kind;

  const h = document.createElement('div');
  h.className = 'score-head';
  h.textContent = head;
  lane.appendChild(h);

  const hl = document.createElement('div');
  hl.className = 'headline';
  const n = document.createElement('span');
  n.className = 'headline-num';
  n.dataset.tone = numTone;
  n.textContent = num;
  const c = document.createElement('span');
  c.className = 'headline-cap';
  c.textContent = cap;
  hl.appendChild(n);
  hl.appendChild(c);
  lane.appendChild(hl);

  if (state) {
    const s = document.createElement('span');
    s.className = 'final-state';
    s.dataset.tone = stateTone;
    s.textContent = state;
    lane.appendChild(s);
  }

  const dl = document.createElement('dl');
  dl.className = 'counters';
  for (const [label, value, tone] of counters) dl.appendChild(counterRow(label, value, tone));
  lane.appendChild(dl);

  return lane;
}

/* ---------------------------------------------------------------- internals */

function intGroup(title, rows) {
  const g = document.createElement('div');
  g.className = 'int-group';
  const h = document.createElement('h4');
  h.textContent = title;
  g.appendChild(h);
  for (const [k, v] of rows) {
    if (v === undefined || v === null || v === '') continue;
    const r = document.createElement('div');
    r.className = 'int-row';
    const a = document.createElement('span');
    a.textContent = k;
    const b = document.createElement('span');
    b.textContent = Array.isArray(v) ? v.join('\n') : String(v);
    r.appendChild(a);
    r.appendChild(b);
    g.appendChild(r);
  }
  return g;
}

function renderInternals(result) {
  const host = $('internals-body');
  host.innerHTML = '';

  const common = [
    ['run id', result.run_id],
    ['experiment', result.experiment],
    ['provider fixture', result.demo_provider_fixture],
  ];

  if (result.experiment === 'response_loss') {
    const n = result.naive;
    const f = result.fincore;
    host.appendChild(intGroup('Naive retry', [
      ['provider invocations', n.provider_invocations],
      ['financial effects', n.financial_effects],
      ['refund ids', n.refund_ids],
      ['provider key fingerprints', n.provider_key_fingerprints],
      ['provider key reused', n.provider_key_reused],
      ['amount refunded (paise)', n.amount_refunded_paise],
    ]));
    host.appendChild(intGroup('Financial Operation Core', [
      ['operation_ref', f.operation_ref],
      ['operation id', f.operation_id],
      ['attempt ids', f.attempt_ids],
      ['state transitions', `${f.intermediate_state} → ${f.final_state}`],
      ['provider key fingerprint', f.provider_key_fingerprint],
      ['provider key reused', f.provider_key_reused],
      ['replayed', f.replayed],
      ['reconciled', f.reconciled],
      ['provider invocations', f.provider_invocations],
      ['financial effects', f.financial_effects],
      ['refund ids', f.refund_ids],
      ['amount refunded (paise)', f.amount_refunded_paise],
    ]));
    host.appendChild(intGroup('Run', common));
    return;
  }

  const rows = {
    worker_crash: [
      ['operation_ref', result.operation_ref],
      ['operation id', result.operation_id],
      ['workers', result.workers],
      ['state at crash', result.state_after_crash],
      ['crash after provider effect', result.crashed_after_provider_effect],
      ['attempt rows', result.attempt_rows],
      ['provider key fingerprint', result.provider_key_fingerprint],
      ['provider key reused', result.provider_key_reused],
      ['replayed', result.replayed],
      ['reconciled', result.reconciled],
      ['final state', result.final_state],
    ],
    concurrency: [
      ['operation_ref', result.operation_ref],
      ['callers', result.callers],
      ['execution owners', result.execution_owners],
      ['turned away', result.turned_away],
      ['persisted attempt rows', result.attempt_rows],
      ['operations created', result.operations],
      ['distinct operation ids', result.distinct_operation_ids],
      ['distinct provider keys', result.distinct_provider_keys],
      ['provider key fingerprint', result.provider_key_fingerprint],
      ['final state', result.final_state],
    ],
    intent_conflict: [
      ['operation_ref', result.operation_ref],
      ['original intent (paise)', result.original_amount_paise],
      ['retry intent (paise)', result.retry_amount_paise],
      ['same intent', result.same_intent],
      ['retry outcome', result.retry_outcome],
      ['conflict', result.conflict],
      ['conflict reason', result.conflict_reason],
      ['provider calls caused by retry', result.provider_calls_caused_by_retry],
      ['effects caused by retry', result.financial_effects_caused_by_retry],
      ['attempt rows', result.attempt_rows],
      ['final state', result.final_state],
    ],
  }[result.experiment] || [];

  host.appendChild(intGroup('Runtime', rows));
  host.appendChild(intGroup('Totals', [
    ['provider invocations', result.provider_invocations],
    ['financial effects', result.financial_effects],
    ['refund ids', result.refund_ids],
  ]));
  host.appendChild(intGroup('Run', common));
}

/* -------------------------------------------------------------- the verdict */

function renderVerdict(result) {
  const money = $('money-lanes');
  const score = $('scoreboard');
  money.innerHTML = '';
  score.innerHTML = '';

  renderAnswer(result);
  renderStageViz(result);

  const split = result.experiment === 'response_loss';
  money.dataset.split = split ? '1' : '0';
  score.dataset.split = split ? '1' : '0';

  if (split) {
    const n = result.naive;
    const f = result.fincore;

    money.appendChild(moneyLane('Without a durable operation', n.financial_effects,
      n.amount_refunded_paise, n.financial_effects > 1));
    money.appendChild(moneyLane('With Financial Operation Core', f.financial_effects,
      f.amount_refunded_paise, false));

    score.appendChild(scoreLane('naive', 'Without FinCore',
      rupees(n.amount_refunded_paise), 'bad', 'of refund effects', null, null, [
        ['Provider calls', n.provider_invocations],
        ['Financial effects', n.financial_effects, 'bad'],
        ['Refunds created', n.refund_ids.length, 'bad'],
      ]));

    score.appendChild(scoreLane('fincore', 'With FinCore',
      rupees(f.amount_refunded_paise), 'good', 'of refund effects',
      f.final_state, 'good', [
        ['Provider calls', f.provider_invocations],
        ['Financial effects', f.financial_effects, 'good'],
        ['Refunds created', f.refund_ids.length, 'good'],
      ]));
    return;
  }

  const effects = result.financial_effects;
  money.appendChild(moneyLane('Financial effects created', effects,
    result.amount_refunded_paise ?? effects * 10000, effects > 1));

  if (result.experiment === 'worker_crash') {
    score.appendChild(scoreLane('fincore', 'Result', String(effects), 'good',
      effects === 1 ? 'financial effect' : 'financial effects',
      result.final_state, 'good', [
        ['Workers involved', result.worker_count],
        ['Provider invocations', result.provider_invocations],
        ['Financial effects', effects, 'good'],
        ['State at crash', result.state_after_crash],
        ['Original refund replayed', result.replayed ? 'yes' : 'no'],
      ]));
  } else if (result.experiment === 'concurrency') {
    score.appendChild(scoreLane('fincore', 'Result', String(effects), 'good',
      effects === 1 ? 'financial effect' : 'financial effects',
      result.final_state, 'good', [
        ['Callers', result.callers],
        ['Execution owners', result.execution_owners, 'good'],
        ['Turned away', result.turned_away],
        ['Provider calls', result.provider_invocations],
        ['Financial effects', effects, 'good'],
        ['Persisted attempt rows', result.attempt_rows],
      ]));
  } else {
    const conflict = result.conflict;
    score.appendChild(scoreLane('fincore', 'Result',
      String(result.financial_effects_caused_by_retry), conflict ? 'good' : 'good',
      'extra financial effects from the retry',
      conflict ? 'CONFLICT' : result.final_state, conflict ? 'bad' : 'good', [
        ['Original intent', rupees(result.original_amount_paise)],
        ['Retry intent', rupees(result.retry_amount_paise)],
        ['Extra provider calls', result.provider_calls_caused_by_retry, 'good'],
        ['Extra financial effects', result.financial_effects_caused_by_retry, 'good'],
        ['Total financial effects', result.financial_effects],
        ['Retry outcome', result.retry_outcome],
      ]));
  }
}

/* ------------------------------------------------------------------- driver */

async function play(result) {
  const flow = $('flow');
  flow.innerHTML = '';
  const split = result.experiment === 'response_loss';
  flow.dataset.split = split ? '1' : '0';
  $('canvas').hidden = false;
  $('canvas-title').textContent = mode.label;

  const lanes = [];
  if (split) {
    const a = makeSpine('naive', 'Without a durable operation');
    const b = makeSpine('fincore', 'Financial Operation Core');
    flow.appendChild(a.wrap);
    flow.appendChild(b.wrap);
    lanes.push({ list: a.list, events: result.naive.events });
    lanes.push({ list: b.list, events: result.fincore.events });
  } else {
    const s = makeSpine('fincore', 'Financial Operation Core');
    flow.appendChild(s.wrap);
    lanes.push({ list: s.list, events: result.events });
  }

  setStatus('Executing', 'run');

  await Promise.all(lanes.map(async ({ list, events }) => {
    for (const ev of events) {
      if (skipped) break;
      list.appendChild(stepEl(ev));
      await sleep(STEP_MS);
    }
    for (const ev of events.slice(list.children.length)) list.appendChild(stepEl(ev));
  }));

  renderVerdict(result);
  renderInternals(result);
  $('verdict').hidden = false;

  // Bring the result to the reviewer rather than making them hunt for it --
  // but never yank the page out from under someone who is already reading it.
  // Any manual scroll during the run cancels this.
  if (!userScrolled) {
    $('verdict').scrollIntoView({
      behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
      block: 'start',
    });
  }

  const effects = split ? result.fincore.financial_effects : result.financial_effects;
  if (result.experiment === 'intent_conflict' && result.conflict) setStatus('Conflict', 'bad');
  else if (effects === 1) setStatus('One effect', 'good');
  else setStatus(`${effects} effects`, 'bad');
}

async function run() {
  if (busy) return;
  busy = true;
  skipped = false;
  userScrolled = false;
  $('run').disabled = true;
  $('skip').hidden = false;
  $('run').textContent = 'Running…';
  clearResult();
  document.body.classList.add('ran');

  const body = { experiment: mode.id };
  if (mode.id === 'concurrency') body.callers = callers;
  if (mode.id === 'intent_conflict') body.retry_amount_paise = retryAmountPaise;

  let result;
  try {
    const res = await fetch('/api/demo/run', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    result = await res.json();
    if (!res.ok) throw new Error(result.error || `HTTP ${res.status}`);
  } catch (err) {
    $('canvas').hidden = false;
    $('flow').innerHTML = `<p class="error-note">${err.message}</p>`;
    setStatus('Failed', 'bad');
    busy = false;
    $('run').disabled = false;
    $('skip').hidden = true;
    $('run').textContent = 'Break the refund';
    return;
  }

  lastResult = result;
  await play(result);

  busy = false;
  $('run').disabled = false;
  $('skip').hidden = true;
  $('run').textContent = 'Run again';
}

/* -------------------------------------------------------------------- wiring */

$('run').addEventListener('click', run);
$('skip').addEventListener('click', () => { skipped = true; });
$('reset').addEventListener('click', () => {
  if (busy) return;
  clearResult();
  $('run').textContent = 'Break the refund';
  fetch('/api/demo/reset', { method: 'POST' }).catch(() => {});
});

const openDrawer = (open) => {
  $('trust').hidden = !open;
  $('trust-scrim').hidden = !open;
  if (open) $('trust-close').focus();
};
$('trust-open').addEventListener('click', () => openDrawer(true));
$('trust-close').addEventListener('click', () => openDrawer(false));
$('trust-scrim').addEventListener('click', () => openDrawer(false));

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') openDrawer(false);
  if (e.target instanceof HTMLInputElement) return;
  if ((e.key === 'r' || e.key === 'R') && !busy) run();
});

renderModes();
selectMode(MODES[0]);
if (RECORDING) document.body.classList.add('recording');
