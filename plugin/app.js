// Audio Meter UI: receives analysis frames from the agent over WebSocket and
// draws level meters, spectrum and stereo correlation on canvases.

const DB_MIN = -60;
const PEAK_FALL_DB_S = 24;     // peak bar release
const HOLD_S = 1.5;            // peak-hold marker time
const SPEC_FALL_DB_S = 40;     // spectrum bar release
const SPEC_MIN = -90;
const FLOOR_TXT = -99;         // below this the peak readout shows -∞

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const COL = {
  bg: css("--bg"), line: css("--line"), field: css("--field"), text: css("--text"), muted: css("--muted"),
  graphLo: css("--graph-lo"), graphHi: css("--graph-hi"), ok: css("--ok"), warn: css("--warn"), hot: css("--hot"),
};

const state = {
  config: null,
  peak: [], hold: [], holdT: [],
  maxPeak: [],      // latched per-channel maximum, shown as a number until reset
  bands: [], bandHold: [],
  corr: null, clip: false,
  target: null,     // latest frame, applied with ballistics on each animation frame
  ltc: null,        // latest LTC summary from the agent
  ltcBase: null,    // counters at the last reset; the UI shows the difference
  ltcBadAt: 0,      // wall time when corrupted frames last increased
  ltcBadSeen: 0,
};
const LTC_RECENT_S = 10;   // an event this recent keeps the status yellow
const LTC_HOT_DB = -1;     // LTC peak above this (since reset) is reported as overload

// ---------- connection ----------

function defaultAgent() {
  const q = new URLSearchParams(location.search).get("agent");
  if (q) return q;
  try { const s = localStorage.getItem("audioMeter.agent"); if (s) return s; } catch {}
  // Served by the agent itself -> same origin; served by Designer -> local agent.
  return location.port === "8765" ? location.host : "localhost:8765";
}

let ws = null;
let retryTimer = null;

function connect() {
  clearTimeout(retryTimer);
  if (ws) { ws.onclose = null; ws.close(); }
  const agent = $("agent").value.trim();
  try { localStorage.setItem("audioMeter.agent", agent); } catch {}
  ws = new WebSocket(`ws://${agent}/ws`);
  ws.onopen = () => $("status").classList.add("on");
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = () => {
    $("status").classList.remove("on");
    $("format").textContent = `нет подключения к ${agent}, повтор…`;
    retryTimer = setTimeout(connect, 2000);
  };
}

function onMessage(msg) {
  if (msg.type === "config") {
    const prev = state.config;
    state.config = msg;
    const n = msg.channels;
    // The agent re-sends config on every device switch; a new device starts clean.
    if (!prev || prev.device !== msg.device || prev.channels !== n) {
      state.peak = Array(n).fill(DB_MIN);
      state.hold = Array(n).fill(DB_MIN); state.holdT = Array(n).fill(0);
      state.bands = Array(msg.bandCentres.length).fill(SPEC_MIN);
      state.bandHold = Array(msg.bandCentres.length).fill(SPEC_MIN);
      state.target = null;
      resetPeaks();
    }
    $("format").textContent = `${msg.samplerate / 1000} kHz · ${n} ch`;
    showError(null);
    $("ltcBar").hidden = !msg.ltc;
    if (!prev || prev.id !== msg.id) { state.ltc = null; state.ltcBase = null; }
    // Grows with the window, but never narrower than the labels need.
    document.querySelector(".meters").style.width = `clamp(${40 + n * 24}px, ${8 + n * 6}vw, 50%)`;
    resize();
  } else if (msg.type === "frame" && state.config) {
    if (!$("errorBar").hidden) showError(null);  // capture is running again
    state.target = msg;
    msg.peak.forEach((p, i) => { if (p > state.maxPeak[i]) state.maxPeak[i] = p; });
    if ("ltc" in msg) updateLtc(msg.ltc);
    if (msg.clip) setClip(true);
  } else if (msg.type === "devices") {
    fillDevices(msg);
  } else if (msg.type === "error") {
    showError(msg.message);
  }
}

// Full-width, wrapping banner: device errors are long and must be readable.
function showError(text) {
  $("errorBar").hidden = !text;
  $("errorBar").textContent = text || "";
  resize();
}

// Each window keeps its own device: sessionStorage is per window (survives a
// reload), localStorage only seeds new windows with the last pick.
function savedDevice() {
  const q = new URLSearchParams(location.search).get("device");
  if (q) return q;
  try { return sessionStorage.getItem("audioMeter.device") || localStorage.getItem("audioMeter.device"); } catch {}
  return null;
}

function fillDevices(msg) {
  const sel = $("deviceSelect");
  sel.replaceChildren();
  const groups = { out: "Выходы", in: "Входы (LTC)" };
  for (const kind of ["out", "in"]) {
    const g = document.createElement("optgroup");
    g.label = groups[kind];
    for (const d of msg.devices.filter((x) => x.kind === kind)) {
      g.append(new Option(d.id === msg.default ? `${d.name} (системное)` : d.name, d.id));
    }
    if (g.children.length) sel.append(g);
  }
  const ids = msg.devices.map((d) => d.id);
  let want = savedDevice();
  if (want && !ids.includes(want) && ids.includes(`out:${want}`)) want = `out:${want}`;
  sel.value = ids.includes(want) ? want : msg.default;
  selectDevice();
}

function selectDevice() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const device = $("deviceSelect").value;
  try {
    sessionStorage.setItem("audioMeter.device", device);
    localStorage.setItem("audioMeter.device", device);
  } catch {}
  ws.send(JSON.stringify({ type: "select", device }));
}

function setClip(on) {
  state.clip = on;
  $("resetPeaks").classList.toggle("active", on);
}

function resetPeaks() {
  state.maxPeak = state.peak.map(() => -Infinity);
  setClip(false);
  state.ltcBase = state.ltc && { ...state.ltc };
  state.ltcBadAt = 0;
  renderLtc();
}

// ---------- LTC ----------

function updateLtc(ltc) {
  if (ltc && !state.ltcBase) state.ltcBase = { jumps: 0, dropouts: 0, good: 0, lost: 0 };
  if (ltc) {
    if (ltc.lost > state.ltcBadSeen) state.ltcBadAt = Date.now();
    state.ltcBadSeen = ltc.lost;
  }
  state.ltc = ltc;
}

function setVal(id, text, bad = false) {
  const el = $(id);
  el.textContent = text;
  el.classList.toggle("bad", bad);
}

function renderLtc() {
  if ($("ltcBar").hidden) return;
  const l = state.ltc, base = state.ltcBase;
  const status = $("ltcStatus");
  let cls = "bad", text = "LTC не найден";
  if (l) {
    const d = (k) => l[k] - (base ? base[k] : 0);
    const ch = l.ch;
    const peak = state.maxPeak[ch], now = state.peak[ch];
    const recentEvent = l.lastEvent && l.lastEvent.ago < LTC_RECENT_S;
    const recentBad = Date.now() - state.ltcBadAt < LTC_RECENT_S * 1000;
    if (!l.locked) { cls = "bad"; text = `НЕТ СИГНАЛА · ${l.missing} кадр.`; }
    // LTC has no business near full scale: -1 dBFS already risks clipped edges.
    else if (peak > LTC_HOT_DB) { cls = "warn"; text = "ПЕРЕГРУЗ"; }
    else if (recentEvent || recentBad) { cls = "warn"; text = "НЕСТАБИЛЬНО"; }
    else if (now < -40) { cls = "warn"; text = "СЛАБЫЙ"; }
    else { cls = "ok"; text = "LTC OK"; }

    $("ltcTc").textContent = l.tc + (l.reverse ? " ◀" : "");
    $("ltcTc").classList.toggle("stale", !l.locked);
    // Lost frames since the last reset, as a count and a share of all expected frames.
    const lost = d("lost"), total = lost + d("good");
    const pct = total ? (100 * lost / total) : 0;
    setVal("ltcRate", l.rate ? `${l.rate} fps` : "…");
    setVal("ltcCh", channelName(ch, state.peak.length));
    setVal("ltcLevel", now > -99 ? `${now.toFixed(0)} dB` : "-∞");
    setVal("ltcLost", lost ? `${lost} (${pct < 0.01 ? "<0.01" : pct.toFixed(2)}%)` : "0", lost > 0);
    setVal("ltcMinute", l.lostMinute, l.lostMinute > 0);
    setVal("ltcJumps", d("jumps"), d("jumps") > 0);
    setVal("ltcDrops", d("dropouts"), d("dropouts") > 0);
    setVal("ltcJitter", l.locked ? `${l.jitter}%` : "—");
    $("ltcEvent").textContent = l.lastEvent && l.lastEvent.ago < 120
      ? `${Math.round(l.lastEvent.ago)} с назад: ${l.lastEvent.text}` : "";
  } else {
    $("ltcTc").textContent = "--:--:--:--";
    $("ltcTc").classList.add("stale");
    for (const id of ["ltcRate", "ltcCh", "ltcLevel", "ltcLost", "ltcMinute", "ltcJumps", "ltcDrops", "ltcJitter"]) setVal(id, "—");
    $("ltcEvent").textContent = "ищу таймкод на всех каналах…";
  }
  status.className = `d3-status ${cls}`;
  status.textContent = text;
}

function setSpectrum(show) {
  document.body.classList.toggle("no-spectrum", !show);
  $("toggleSpectrum").classList.toggle("on", show);
  try { localStorage.setItem("audioMeter.spectrum", show ? "1" : "0"); } catch {}
  resize();
}

// ---------- canvases ----------

const canvases = ["meters", "spectrum", "corr"].map((id) => $(id));

function resize() {
  const dpr = window.devicePixelRatio || 1;
  for (const c of canvases) {
    const r = c.getBoundingClientRect();
    c.width = Math.max(1, Math.round(r.width * dpr));
    c.height = Math.max(1, Math.round(r.height * dpr));
    c.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
  }
}

const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
// Peak-hold line: red above -3 dB warns before the bar itself (green/yellow only) would.
const holdColor = (d) => (d > -3 ? COL.hot : d > -12 ? COL.warn : COL.ok);

// ---------- ballistics ----------

let lastT = performance.now();

function step(now) {
  const dt = Math.min(0.1, (now - lastT) / 1000);
  lastT = now;
  const t = state.target;
  if (t) {
    for (let i = 0; i < state.peak.length; i++) {
      state.peak[i] = Math.max(t.peak[i], state.peak[i] - PEAK_FALL_DB_S * dt);
      if (t.peak[i] >= state.hold[i]) { state.hold[i] = t.peak[i]; state.holdT[i] = now; }
      else if (now - state.holdT[i] > HOLD_S * 1000) state.hold[i] -= PEAK_FALL_DB_S * dt;
    }
    for (let i = 0; i < state.bands.length; i++) {
      state.bands[i] = Math.max(t.bands[i], state.bands[i] - SPEC_FALL_DB_S * dt);
      state.bandHold[i] = Math.max(state.bands[i], state.bandHold[i] - SPEC_FALL_DB_S * 0.3 * dt);
    }
    if (t.corr !== null) state.corr = state.corr === null ? t.corr : state.corr + (t.corr - state.corr) * Math.min(1, dt * 5);
  }
  drawMeters(); drawSpectrum(); drawCorr(); renderLtc();
  requestAnimationFrame(step);
}

// ---------- drawing ----------

const FONT = "Segoe UI, sans-serif";
// Label size follows the panel so text stays readable when the window is scaled.
const labelSize = (H) => Math.round(clamp(H / 26, 9, 16));

// Largest font (<= max) at which `text` fits into `width`; never below 7px.
function fitFont(g, text, width, max, weight = "") {
  let size = max;
  for (; size > 7; size--) {
    g.font = `${weight} ${size}px ${FONT}`;
    if (g.measureText(text).width <= width) break;
  }
  return size;
}

// Draws tick labels top-down, skipping any that would overlap the previous one.
function drawTicks(g, values, y, x, fs, gridFrom, gridTo) {
  g.font = `${fs}px ${FONT}`;
  g.textAlign = "right"; g.textBaseline = "middle";
  let lastY = -Infinity;
  for (const d of values) {
    const yy = y(d);
    g.fillStyle = COL.line; g.fillRect(gridFrom, Math.round(yy), gridTo - gridFrom, 1);
    if (yy - lastY < fs + 2) continue;
    g.fillStyle = COL.muted; g.fillText(d, x, yy);
    lastY = yy;
  }
}

function drawMeters() {
  const c = $("meters"), g = c.getContext("2d");
  const W = c.clientWidth, H = c.clientHeight;
  g.clearRect(0, 0, W, H);
  const fs = labelSize(H);
  g.font = `${fs}px ${FONT}`;
  const scaleW = Math.ceil(g.measureText("-60").width) + 8;
  const n = state.peak.length;
  const wide = document.body.classList.contains("no-spectrum");
  // With the spectrum hidden the meters get the whole panel: wider bars, centred.
  const avail = W - scaleW - 6;
  const gap = n ? Math.max(3, Math.min(wide ? 12 : 6, avail / n * 0.15)) : 0;
  const bw = n ? Math.max(3, Math.min(wide ? 160 : 64, avail / n - gap)) : 0;
  const peakFs = n ? fitFont(g, "-88.8", bw + gap - 2, Math.round(clamp(bw * 0.45, 10, 18)), "bold") : fs;
  const top = peakFs + 8, bottom = H - fs - 6;
  const y = (d) => top + (1 - (clamp(d, DB_MIN, 0) - DB_MIN) / -DB_MIN) * (bottom - top);

  drawTicks(g, [0, -3, -6, -12, -18, -24, -36, -48, -60], y, scaleW - 4, fs, scaleW, W - 2);
  if (!n) return;

  const x0 = scaleW + 2 + (wide ? Math.max(0, (avail - n * (bw + gap)) / 2) : 0);
  const chFs = fitFont(g, "LFE", bw + gap - 2, fs);
  g.textAlign = "center"; g.textBaseline = "middle";
  for (let i = 0; i < n; i++) {
    const x = x0 + i * (bw + gap);
    g.fillStyle = COL.field; g.fillRect(x, top, bw, bottom - top);
    // peak bar: green below -12 dB, yellow above
    const p = state.peak[i];
    for (const [lo, hi, col] of [[DB_MIN, -12, COL.ok], [-12, 0, COL.warn]]) {
      if (p <= lo) continue;
      const y0 = y(Math.min(p, hi)), y1 = y(lo);
      g.fillStyle = col; g.fillRect(x, y0, bw, y1 - y0);
    }
    // hold marker
    if (state.hold[i] > DB_MIN) { g.fillStyle = holdColor(state.hold[i]); g.fillRect(x, y(state.hold[i]) - 1, bw, 2); }
    g.font = `${chFs}px ${FONT}`;
    g.fillStyle = COL.muted; g.fillText(channelName(i, n), x + bw / 2, bottom + fs / 2 + 3);
    // latched peak value
    const m = state.maxPeak[i];
    g.font = `bold ${peakFs}px ${FONT}`;
    g.fillStyle = m >= -0.1 ? COL.hot : m > -3 ? COL.warn : COL.text;
    g.fillText(m > FLOOR_TXT ? m.toFixed(1) : "-∞", x + bw / 2, top / 2);
  }
}

function channelName(i, n) {
  if (n === 2) return i ? "R" : "L";
  const names = ["L", "R", "C", "LFE", "Ls", "Rs", "Lb", "Rb"];
  return n <= 8 ? names[i] : String(i + 1);
}

function drawSpectrum() {
  if (document.body.classList.contains("no-spectrum")) return;
  const c = $("spectrum"), g = c.getContext("2d");
  const W = c.clientWidth, H = c.clientHeight;
  g.clearRect(0, 0, W, H);
  const fs = labelSize(H);
  g.font = `${fs}px ${FONT}`;
  const left = Math.ceil(g.measureText("-90").width) + 8, right = W - 6;
  const top = fs / 2 + 4, bottom = H - fs - 6;
  const y = (d) => top + (1 - (clamp(d, SPEC_MIN, 0) - SPEC_MIN) / -SPEC_MIN) * (bottom - top);

  const ticks = [];
  for (let d = 0; d >= SPEC_MIN; d -= 12) ticks.push(d);
  drawTicks(g, ticks, y, left - 4, fs, left, right);

  const n = state.bands.length;
  if (!n) return;
  const bw = (right - left) / n;
  const grad = g.createLinearGradient(0, bottom, 0, top);
  grad.addColorStop(0, COL.graphLo); grad.addColorStop(1, COL.graphHi);
  const pad = bw > 4 ? 1 : 0;
  for (let i = 0; i < n; i++) {
    const x = left + i * bw, v = state.bands[i];
    g.fillStyle = grad;
    g.fillRect(x + pad, y(v), Math.max(1, bw - 2 * pad), bottom - y(v));
    g.fillStyle = COL.text;
    g.fillRect(x + pad, y(state.bandHold[i]) - 1, Math.max(1, bw - 2 * pad), 2);
  }

  // frequency labels at the nearest band centre, skipped when they would collide
  const centres = state.config.bandCentres;
  g.font = `${fs}px ${FONT}`;
  g.textAlign = "center"; g.textBaseline = "middle"; g.fillStyle = COL.muted;
  let lastRight = -Infinity;
  for (const f of [50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]) {
    let best = 0;
    centres.forEach((cf, i) => { if (Math.abs(Math.log(cf / f)) < Math.abs(Math.log(centres[best] / f))) best = i; });
    if (Math.abs(Math.log(centres[best] / f)) > 0.3) continue;
    const label = f >= 1000 ? `${f / 1000}k` : String(f);
    const w = g.measureText(label).width;
    const cx = clamp(left + (best + 0.5) * bw, left + w / 2, right - w / 2);
    if (cx - w / 2 < lastRight + 4) continue;
    g.fillText(label, cx, bottom + fs / 2 + 3);
    lastRight = cx + w / 2;
  }
}

function drawCorr() {
  const c = $("corr"), g = c.getContext("2d");
  const W = c.clientWidth, H = c.clientHeight;
  g.clearRect(0, 0, W, H);
  g.fillStyle = COL.field; g.fillRect(0, 0, W, H);
  g.fillStyle = COL.line; g.fillRect(W / 2, 0, 1, H);
  const v = state.corr;
  $("corrValue").textContent = v === null ? "—" : (v > 0 ? "+" : "") + v.toFixed(2);
  if (v === null) return;
  const x = ((v + 1) / 2) * W;
  g.fillStyle = v < 0 ? COL.hot : v < 0.3 ? COL.warn : COL.ok;
  g.fillRect(Math.min(x, W / 2), 2, Math.abs(x - W / 2), H - 4);
}

// ---------- init ----------

$("agent").value = defaultAgent();
$("reconnect").onclick = connect;
$("agent").onkeydown = (e) => { if (e.key === "Enter") connect(); };
$("resetPeaks").onclick = resetPeaks;
$("meters").onclick = resetPeaks;
$("deviceSelect").onchange = selectDevice;
$("toggleSpectrum").onclick = () => setSpectrum(document.body.classList.contains("no-spectrum"));
let showSpectrum = true;
try { showSpectrum = localStorage.getItem("audioMeter.spectrum") !== "0"; } catch {}
setSpectrum(showSpectrum);
// Collapsible LTC statistics, remembered like the spectrum toggle.
$("ltcSection").onclick = () => {
  const collapsed = $("ltcSection").classList.toggle("collapsed");
  try { localStorage.setItem("audioMeter.ltcStats", collapsed ? "0" : "1"); } catch {}
  resize();
};
try { if (localStorage.getItem("audioMeter.ltcStats") === "0") $("ltcSection").classList.add("collapsed"); } catch {}
$("toggleSettings").onclick = () => {
  const panel = $("settings");
  panel.hidden = !panel.hidden;
  $("toggleSettings").classList.toggle("on", !panel.hidden);
};
// Canvas backing stores follow their boxes (window resize, wrapping bars, toggles).
new ResizeObserver(resize).observe(document.body);
for (const cv of canvases) new ResizeObserver(resize).observe(cv);
resize();
connect();
requestAnimationFrame(step);
