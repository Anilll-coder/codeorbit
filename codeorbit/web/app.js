/* CodeOrbit graph explorer.
 *
 * Canvas, not SVG. One DOM element per node stops being viable somewhere around
 * a thousand nodes: style recalculation and hit testing dominate the frame and
 * panning turns to slideshow. A canvas draws the same graph in one pass, and
 * the two things that actually cost time at scale are handled explicitly here:
 * nothing outside the viewport is drawn, and detail drops out as you zoom away
 * from it.
 *
 * Layout runs in layout.worker.js. This file only ever draws the last positions
 * it was handed.
 */

const $ = (id) => document.getElementById(id);

const state = {
  meta: null,
  view: "modules",
  nodes: [],
  edges: [],
  index: new Map(),        // node id -> array index
  x: null, y: null,
  deg: null,
  visible: null,           // Uint8Array mask
  colorOf: null,           // Uint8Array palette slot per node
  radius: null,
  camera: { x: 0, y: 0, k: 1 },
  selected: null,
  hover: -1,
  dragNode: -1,
  filters: { lang: new Set(), kind: new Set(), dir: new Set(), minDeg: 0 },
  neighbours: null,        // Set of indices adjacent to selection
  settled: false,
};

/* The palette is fixed; which slots get used depends on the project. Chosen to
 * stay distinguishable in both themes and for the common forms of colour
 * blindness (no red/green pair carrying meaning on its own). */
const PALETTE = [
  "#4c9aff", "#f0883e", "#3fb950", "#bc8cff",
  "#39c5cf", "#e5534b", "#d29922", "#8b98a8",
];

let ctx, canvas, worker, dpr = 1;
let rafPending = false;

/* Draw on demand, never on a timer. A permanent requestAnimationFrame loop
 * wakes the compositor sixty times a second to decide there is nothing to do,
 * which costs battery on a page that is idle most of the time and keeps the
 * tab from ever reporting itself as rendered. */
function invalidate() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; draw(); });
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  canvas = $("canvas");
  ctx = canvas.getContext("2d", { alpha: false });
  initTheme();
  wireChrome();
  resize();
  new ResizeObserver(resize).observe(canvas.parentElement);

  try {
    state.meta = await json("/api/meta");
  } catch (e) {
    return fail("Could not reach the CodeOrbit server.", String(e));
  }

  $("project").textContent = state.meta.name;
  $("root").textContent = state.meta.root;
  $("root").title = state.meta.root;
  document.title = state.meta.name + " - CodeOrbit";

  if (!state.meta.indexed) {
    return fail(
      "This project is not indexed yet.",
      "Run codeorbit index in the project, then reload this page.");
  }

  renderStats();

  // `codeorbit viz --view symbols --focus foo` opens straight into that view.
  const q = new URLSearchParams(location.search);
  const view = q.get("view") === "symbols" ? "symbols" : "modules";
  await load(view, q.get("focus") || undefined);
}

function json(url) {
  return fetch(url).then((r) => {
    if (!r.ok) throw new Error(r.status + " " + r.statusText);
    return r.json();
  });
}

function fail(title, detail) {
  const o = $("overlay");
  o.hidden = false;
  o.innerHTML = "";
  const h = document.createElement("h3");
  h.textContent = title;
  const p = document.createElement("p");
  p.textContent = detail;
  o.append(h, p);
}

function busy(msg) {
  const o = $("overlay");
  o.hidden = false;
  o.innerHTML =
    '<div class="skeleton"><div class="dots"><i></i><i></i><i></i><i></i><i></i></div>' +
    '<p id="overlay-msg"></p></div>';
  $("overlay-msg").textContent = msg;
}

/* ------------------------------------------------------------------ data */

async function load(view, focus) {
  state.view = view;
  $("v-modules").setAttribute("aria-pressed", String(view === "modules"));
  $("v-symbols").setAttribute("aria-pressed", String(view === "symbols"));
  busy(view === "modules" ? "Reading modules and imports" : "Reading symbols and calls");

  let data;
  try {
    const q = new URLSearchParams({ view });
    if (focus) q.set("focus", focus);
    data = await json("/api/graph?" + q);
  } catch (e) {
    return fail("Could not build that view.", String(e));
  }

  if (!data.nodes.length) {
    return fail("Nothing to draw here.",
      view === "symbols"
        ? "No functions or methods were found in the index."
        : "No modules were found in the index.");
  }

  state.nodes = data.nodes;
  state.edges = data.edges;
  state.truncated = data.truncated || 0;
  state.index = new Map(data.nodes.map((n, i) => [n.id, i]));

  const n = data.nodes.length;
  state.x = new Float32Array(n);
  state.y = new Float32Array(n);
  state.deg = new Uint16Array(n);
  state.visible = new Uint8Array(n).fill(1);
  state.radius = new Float32Array(n);
  state.colorOf = new Uint8Array(n);
  state.selected = null;
  state.neighbours = null;
  state.userMoved = false;

  const src = new Int32Array(data.edges.length);
  const dst = new Int32Array(data.edges.length);
  let m = 0;
  for (const e of data.edges) {
    const a = state.index.get(e.source), b = state.index.get(e.target);
    if (a === undefined || b === undefined || a === b) continue;
    src[m] = a; dst[m] = b; m++;
    state.deg[a]++; state.deg[b]++;
  }
  state.edgeSrc = src.subarray(0, m);
  state.edgeDst = dst.subarray(0, m);

  const mass = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    // Radius reads fan-in: the things everything calls should be findable.
    state.radius[i] = 3.1 + Math.sqrt(state.nodes[i].weight || 1) * 1.5;
    mass[i] = 1 + Math.min(state.deg[i], 40) * 0.06;
  }

  buildFilters();
  assignColors();
  startLayout(n, mass);
  $("overlay").hidden = true;
  updateHud();
}

function startLayout(n, mass) {
  if (worker) worker.terminate();
  try {
    worker = new Worker("/layout.worker.js");
  } catch (e) {
    return fail("This browser blocked the layout worker.", String(e));
  }
  state.settled = false;
  worker.onmessage = (ev) => {
    const m = ev.data;
    if (m.type === "tick") {
      state.x = m.x; state.y = m.y;
      gridDirty = true;
      // Follow the layout while it is still expanding. Fitting once on the
      // first tick framed the seed cloud, not the graph, and a large project
      // then settled into a dot in the middle of an empty canvas.
      if (!state.userMoved && m.alpha > 0.05 && (state.tick = (state.tick | 0) + 1) % 5 === 0) fit();
      invalidate();
    } else if (m.type === "settled") {
      state.settled = true;
      updateHud();
    }
  };
  const degCopy = new Float32Array(n);
  for (let i = 0; i < n; i++) degCopy[i] = Math.max(state.deg[i], 1);
  worker.postMessage({
    type: "init", n, mass, deg: degCopy,
    src: state.edgeSrc.slice(), dst: state.edgeDst.slice(),
  });
}

/* --------------------------------------------------------- project chrome */
/* Everything below is built from what the index holds. A Python-only project
 * gets one language row, not a legend of languages it does not contain. */

function renderStats() {
  const s = state.meta.stats;
  const rows = [
    ["Files", s.files], ["Symbols", s.symbols],
    ["Calls", s.calls], ["Imports", s.imports],
  ];
  const host = $("stats");
  host.innerHTML = "";
  for (const [k, v] of rows) {
    if (!v) continue;
    const el = document.createElement("div");
    el.className = "stat";
    const a = document.createElement("span"); a.className = "k"; a.textContent = k;
    const b = document.createElement("span"); b.className = "v";
    b.textContent = v.toLocaleString();
    el.append(a, b);
    host.append(el);
  }
}

function facets() {
  const lang = new Map(), kind = new Map(), dir = new Map();
  for (const nd of state.nodes) {
    if (nd.lang) lang.set(nd.lang, (lang.get(nd.lang) || 0) + 1);
    if (nd.kind) kind.set(nd.kind, (kind.get(nd.kind) || 0) + 1);
    const d = topDir(nd.full || "");
    if (d) dir.set(d, (dir.get(d) || 0) + 1);
  }
  return { lang, kind, dir };
}

function topDir(p) {
  const parts = String(p).replace(/\\/g, "/").split("/");
  return parts.length > 1 ? parts[0] : "";
}

function buildFilters() {
  const f = facets();
  state.facets = f;
  state.filters.lang = new Set(f.lang.keys());
  state.filters.kind = new Set(f.kind.keys());
  state.filters.dir = new Set(f.dir.keys());

  // A facet with one value explains nothing, so it does not get a section.
  section("sec-lang", "langs", f.lang, "lang", true);
  section("sec-kind", "kinds", f.kind, "kind", false);
  section("sec-dir", "dirs", f.dir, "dir", false);

  const maxDeg = Math.min(12, Math.max(...state.deg, 1));
  const deg = $("deg");
  deg.max = String(maxDeg);
  deg.value = "0";
  state.filters.minDeg = 0;
  $("degv").textContent = "0";
  deg.parentElement.parentElement.hidden = maxDeg < 2;
}

function section(secId, hostId, counts, key, swatches) {
  const sec = $(secId), host = $(hostId);
  host.innerHTML = "";
  if (counts.size < 2) { sec.hidden = true; return; }
  sec.hidden = false;

  const entries = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 14);
  for (const [name, count] of entries) {
    const label = document.createElement("label");
    label.className = "filter";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.addEventListener("change", () => {
      if (cb.checked) state.filters[key].add(name);
      else state.filters[key].delete(name);
      label.dataset.off = cb.checked ? "0" : "1";
      applyFilters();
    });
    label.append(cb);
    if (swatches) {
      const sw = document.createElement("span");
      sw.className = "sw";
      sw.style.background = PALETTE[paletteSlot(name)];
      label.append(sw);
    }
    const nm = document.createElement("span");
    nm.className = "name"; nm.textContent = name; nm.title = name;
    const n = document.createElement("span");
    n.className = "n"; n.textContent = count.toLocaleString();
    label.append(nm, n);
    host.append(label);
  }
}

const slots = new Map();
function paletteSlot(name) {
  if (!slots.has(name)) slots.set(name, slots.size % PALETTE.length);
  return slots.get(name);
}

function assignColors() {
  const byKind = state.view === "symbols";
  for (let i = 0; i < state.nodes.length; i++) {
    const nd = state.nodes[i];
    state.colorOf[i] = paletteSlot(byKind ? (nd.kind || "symbol") : (nd.lang || "?"));
  }
}

function applyFilters() {
  const { lang, kind, dir, minDeg } = state.filters;
  const f = state.facets;
  for (let i = 0; i < state.nodes.length; i++) {
    const nd = state.nodes[i];
    let ok = state.deg[i] >= minDeg;
    if (ok && f.lang.size > 1 && nd.lang) ok = lang.has(nd.lang);
    if (ok && f.kind.size > 1 && nd.kind) ok = kind.has(nd.kind);
    if (ok && f.dir.size > 1) {
      const d = topDir(nd.full || "");
      if (d) ok = dir.has(d);
    }
    state.visible[i] = ok ? 1 : 0;
  }
  gridDirty = true;
  invalidate();
  updateHud();
}

function updateHud() {
  let shown = 0;
  for (let i = 0; i < state.visible.length; i++) shown += state.visible[i];
  const total = state.nodes.length;
  const unit = state.view === "modules" ? "modules" : "symbols";
  const parts = [`<b>${shown.toLocaleString()}</b> of ${total.toLocaleString()} ${unit}`];
  parts.push(`<b>${state.edgeSrc.length.toLocaleString()}</b> edges`);
  if (state.truncated) parts.push(`${state.truncated.toLocaleString()} not shown`);
  if (!state.settled) parts.push("settling");
  $("hud").innerHTML = parts.join(" &nbsp;/&nbsp; ");
}

/* -------------------------------------------------------------- rendering */

function resize() {
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  const r = canvas.parentElement.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(r.width * dpr));
  canvas.height = Math.max(1, Math.round(r.height * dpr));
  invalidate();
}

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

let theme = {};
function readTheme() {
  theme = {
    bg: css("--bg"), edge: css("--edge"), uncertain: css("--edge-uncertain"),
    ink: css("--ink"), ink3: css("--ink-3"), accent: css("--accent"),
    surface: css("--surface"),
  };
}

function draw() {
  const w = canvas.width, h = canvas.height;
  const { k } = state.camera;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = theme.bg;
  ctx.fillRect(0, 0, w, h);

  const n = state.nodes.length;
  if (!n) return;

  // World-space rectangle actually on screen, with a small margin so nodes
  // straddling the edge still draw their circle.
  const pad = 60 / k;
  const view = {
    x0: -state.camera.x / k - pad,
    y0: -state.camera.y / k - pad,
    x1: (w / dpr - state.camera.x) / k + pad,
    y1: (h / dpr - state.camera.y) / k + pad,
  };

  ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * state.camera.x, dpr * state.camera.y);

  const X = state.x, Y = state.y, vis = state.visible;
  const sel = state.selected;
  const nb = state.neighbours;

  /* Edges. Below a certain zoom they stop carrying information and start
   * costing frames, so they fade out and then stop being drawn at all. */
  if (k > 0.16) {
    const edgeAlpha = Math.min(0.85, 0.2 + k * 0.5);
    ctx.lineWidth = Math.min(1.4 / k, 1.4);

    // Faded first, then the selection's own edges on top. Hiding the rest
    // outright looked like a rendering failure whenever the selected node
    // happened to have no edges in this view.
    for (const strong of nb ? [0, 1] : [1]) {
      for (const pass of [0, 1]) {
        ctx.beginPath();
        let drew = false;
        for (let e = 0; e < state.edgeSrc.length; e++) {
          const a = state.edgeSrc[e], b = state.edgeDst[e];
          if (!vis[a] || !vis[b]) continue;
          const isUncertain = state.edges[e] && state.edges[e].uncertain ? 1 : 0;
          if (isUncertain !== pass) continue;
          if (nb) {
            const touches = (nb.has(a) || nb.has(b)) ? 1 : 0;
            if (touches !== strong) continue;
          }
          const ax = X[a], ay = Y[a], bx = X[b], by = Y[b];
          if ((ax < view.x0 && bx < view.x0) || (ax > view.x1 && bx > view.x1) ||
              (ay < view.y0 && by < view.y0) || (ay > view.y1 && by > view.y1)) continue;
          ctx.moveTo(ax, ay);
          ctx.lineTo(bx, by);
          drew = true;
        }
        if (!drew) continue;
        ctx.globalAlpha = strong
          ? (nb ? Math.min(1, edgeAlpha + 0.3) : edgeAlpha)
          : edgeAlpha * 0.22;
        ctx.strokeStyle = pass ? theme.uncertain : theme.edge;
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
  }

  /* Nodes, batched one fill per colour. Switching fillStyle is the expensive
   * part of drawing thousands of circles, so it happens eight times, not n. */
  const buckets = [];
  for (let i = 0; i < PALETTE.length; i++) buckets.push([]);
  let onscreen = 0;

  for (let i = 0; i < n; i++) {
    if (!vis[i]) continue;
    const px = X[i], py = Y[i];
    if (px < view.x0 || px > view.x1 || py < view.y0 || py > view.y1) continue;
    buckets[state.colorOf[i]].push(i);
    onscreen++;
  }

  const dim = nb ? 0.22 : 1;
  for (let c = 0; c < buckets.length; c++) {
    const list = buckets[c];
    if (!list.length) continue;

    // Faded pass first: everything not adjacent to the selection.
    for (const strong of [0, 1]) {
      ctx.beginPath();
      let drew = false;
      for (const i of list) {
        const near = !nb || nb.has(i) ? 1 : 0;
        if (near !== strong) continue;
        const r = state.radius[i];
        ctx.moveTo(X[i] + r, Y[i]);
        ctx.arc(X[i], Y[i], r, 0, 6.283185307179586);
        drew = true;
      }
      if (!drew) continue;
      ctx.globalAlpha = strong ? 1 : dim;
      ctx.fillStyle = PALETTE[c];
      ctx.fill();
    }
  }
  ctx.globalAlpha = 1;

  // Selection and hover rings.
  for (const [i, color, width] of [
    [state.hover, theme.ink, 1.5],
    [sel === null ? -1 : sel, theme.accent, 2.2],
  ]) {
    if (i < 0 || !vis[i]) continue;
    ctx.beginPath();
    ctx.arc(X[i], Y[i], state.radius[i] + 2.5 / k, 0, 6.283185307179586);
    ctx.strokeStyle = color;
    ctx.lineWidth = width / k;
    ctx.stroke();
  }

  /* Labels are the first thing to go. Text is the most expensive thing on the
   * canvas, and a label smaller than a few pixels is noise either way. */
  if (k > 0.42 && onscreen < 900) {
    ctx.font = `${11 / k}px ${css("--mono") || "monospace"}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = theme.ink3;

    const labelled = [];
    for (const list of buckets) for (const i of list) labelled.push(i);
    // When the screen is busy, only the nodes that matter get a name.
    labelled.sort((a, b) => state.radius[b] - state.radius[a]);
    const cap = Math.min(labelled.length, k > 1 ? 320 : 140);

    for (let j = 0; j < cap; j++) {
      const i = labelled[j];
      if (nb && !nb.has(i)) continue;
      if (state.radius[i] * k < 3 && j > 40) continue;
      ctx.fillText(state.nodes[i].label, X[i], Y[i] + state.radius[i] + 2.5 / k);
    }
  }
}

/* ---------------------------------------------------------- hit testing */
/* A linear scan is fine at 400 nodes and not fine at 5000 once it runs on
 * every mousemove. Bucketing into a uniform grid makes a hover test look at a
 * handful of candidates instead of all of them. */

let grid = new Map(), gridCell = 48, gridDirty = true;

function buildGrid() {
  grid = new Map();
  const X = state.x, Y = state.y, vis = state.visible;
  for (let i = 0; i < state.nodes.length; i++) {
    if (!vis[i]) continue;
    const key = ((X[i] / gridCell) | 0) + "," + ((Y[i] / gridCell) | 0);
    let cell = grid.get(key);
    if (!cell) grid.set(key, cell = []);
    cell.push(i);
  }
  gridDirty = false;
}

function pick(wx, wy) {
  if (gridDirty) buildGrid();
  const cx = (wx / gridCell) | 0, cy = (wy / gridCell) | 0;
  let best = -1, bestD = Infinity;
  for (let gx = cx - 1; gx <= cx + 1; gx++) {
    for (let gy = cy - 1; gy <= cy + 1; gy++) {
      const cell = grid.get(gx + "," + gy);
      if (!cell) continue;
      for (const i of cell) {
        const dx = state.x[i] - wx, dy = state.y[i] - wy;
        const d = dx * dx + dy * dy;
        const r = state.radius[i] + 3;
        if (d < r * r && d < bestD) { best = i; bestD = d; }
      }
    }
  }
  return best;
}

function toWorld(ev) {
  const r = canvas.getBoundingClientRect();
  return {
    x: (ev.clientX - r.left - state.camera.x) / state.camera.k,
    y: (ev.clientY - r.top - state.camera.y) / state.camera.k,
  };
}

/* ------------------------------------------------------------ interaction */

function wireChrome() {
  $("v-modules").onclick = () => load("modules");
  $("v-symbols").onclick = () => load("symbols");
  $("zin").onclick = () => zoomBy(1.25);
  $("zout").onclick = () => zoomBy(1 / 1.25);
  $("zfit").onclick = fit;
  $("theme").onclick = toggleTheme;

  $("deg").addEventListener("input", (e) => {
    state.filters.minDeg = +e.target.value;
    $("degv").textContent = e.target.value;
    applyFilters();
  });

  let panning = false, last = null;

  canvas.addEventListener("pointerdown", (ev) => {
    canvas.setPointerCapture(ev.pointerId);
    const w = toWorld(ev);
    const hit = pick(w.x, w.y);
    if (hit >= 0) {
      state.dragNode = hit;
      worker && worker.postMessage({ type: "pin", i: hit, x: w.x, y: w.y });
    } else {
      panning = true;
      canvas.classList.add("dragging");
    }
    last = { x: ev.clientX, y: ev.clientY, moved: false };
  });

  canvas.addEventListener("pointermove", (ev) => {
    const w = toWorld(ev);

    if (state.dragNode >= 0) {
      worker && worker.postMessage({ type: "pin", i: state.dragNode, x: w.x, y: w.y });
      state.x[state.dragNode] = w.x; state.y[state.dragNode] = w.y;
      last.moved = true;
      invalidate();
      return;
    }

    if (panning && last) {
      state.userMoved = true;
      state.camera.x += ev.clientX - last.x;
      state.camera.y += ev.clientY - last.y;
      last.x = ev.clientX; last.y = ev.clientY; last.moved = true;
      invalidate();
      hideTip();
      return;
    }

    const hit = pick(w.x, w.y);
    if (hit !== state.hover) {
      state.hover = hit;
      invalidate();
      hit >= 0 ? showTip(ev, hit) : hideTip();
    } else if (hit >= 0) {
      showTip(ev, hit);
    }
  });

  canvas.addEventListener("pointerup", (ev) => {
    if (state.dragNode >= 0) {
      worker && worker.postMessage({ type: "pin", i: -1 });
      if (!last.moved) select(state.dragNode);
      state.dragNode = -1;
    } else if (panning && last && !last.moved) {
      select(null);
    }
    panning = false;
    canvas.classList.remove("dragging");
  });

  canvas.addEventListener("pointerleave", () => {
    state.hover = -1; hideTip(); invalidate();
  });

  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = canvas.getBoundingClientRect();
    zoomAt(ev.clientX - r.left, ev.clientY - r.top,
           Math.pow(1.0015, -ev.deltaY));
  }, { passive: false });

  wireSearch();

  window.addEventListener("keydown", (ev) => {
    if (ev.key === "/" && document.activeElement !== $("q")) {
      ev.preventDefault(); $("q").focus();
    } else if (ev.key === "Escape") {
      $("results").innerHTML = "";
      select(null);
    } else if (ev.key === "f" && !ev.metaKey && !ev.ctrlKey &&
               document.activeElement !== $("q")) {
      fit();
    }
  });
}

function zoomBy(f) {
  const r = canvas.getBoundingClientRect();
  zoomAt(r.width / 2, r.height / 2, f);
}

function zoomAt(px, py, f) {
  state.userMoved = true;
  const c = state.camera;
  const k = Math.max(0.04, Math.min(6, c.k * f));
  c.x = px - (px - c.x) * (k / c.k);
  c.y = py - (py - c.y) * (k / c.k);
  c.k = k;
  invalidate();
}

/* Frame where the mass is, not where the outliers are.
 *
 * A real project always has a few nodes with one edge or none, and a force
 * layout flings them a long way from everything else. Fitting to the true
 * bounding box then zooms out far enough to include those strays, and the part
 * anyone came to look at collapses into a dot in the middle. Trimming a couple
 * of percent off each axis frames the body of the graph and lets the strays sit
 * outside the viewport, where scrolling will still find them. */
function fit() {
  const n = state.nodes.length;
  if (!n) return;

  const xs = [], ys = [];
  for (let i = 0; i < n; i++) {
    if (!state.visible[i]) continue;
    xs.push(state.x[i]); ys.push(state.y[i]);
  }
  if (!xs.length) return;
  xs.sort((a, b) => a - b);
  ys.sort((a, b) => a - b);
  const cut = Math.floor(xs.length * 0.02);
  const hi = xs.length - 1 - cut;
  let x0 = xs[cut], x1 = xs[hi], y0 = ys[cut], y1 = ys[hi];
  if (!isFinite(x0) || x1 - x0 < 1) { x0 = xs[0]; x1 = xs[xs.length - 1]; }
  if (!isFinite(y0) || y1 - y0 < 1) { y0 = ys[0]; y1 = ys[ys.length - 1]; }
  if (!isFinite(x0)) return;
  const r = canvas.getBoundingClientRect();
  const k = Math.min(6, Math.max(0.04,
    Math.min(r.width / (x1 - x0 + 90), r.height / (y1 - y0 + 90))));
  state.camera.k = k;
  state.camera.x = r.width / 2 - ((x0 + x1) / 2) * k;
  state.camera.y = r.height / 2 - ((y0 + y1) / 2) * k;
  invalidate();
}

function showTip(ev, i) {
  const tip = $("tip");
  const nd = state.nodes[i];
  tip.innerHTML = "";
  const b = document.createElement("div");
  b.textContent = nd.label;
  const s = document.createElement("div");
  s.className = "t-sub";
  s.textContent = nd.meta || nd.full || "";
  tip.append(b, s);
  tip.hidden = false;
  const r = canvas.getBoundingClientRect();
  const x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
  tip.style.left = Math.min(x, r.width - tip.offsetWidth - 8) + "px";
  tip.style.top = Math.min(y, r.height - tip.offsetHeight - 8) + "px";
}

function hideTip() { $("tip").hidden = true; }

/* ---------------------------------------------------------------- detail */

async function select(i) {
  state.selected = i;
  if (i === null) {
    state.neighbours = null;
    invalidate();
    $("panel").innerHTML =
      '<p class="empty">Click a node to see what calls it, what it calls, and ' +
      'its source. Drag to pan, scroll to zoom.</p>';
    return;
  }

  // Dim everything that is not adjacent, so one node's neighbourhood reads
  // clearly even in a dense graph.
  const nb = new Set([i]);
  for (let e = 0; e < state.edgeSrc.length; e++) {
    if (state.edgeSrc[e] === i) nb.add(state.edgeDst[e]);
    else if (state.edgeDst[e] === i) nb.add(state.edgeSrc[e]);
  }
  state.neighbours = nb;
  invalidate();

  const nd = state.nodes[i];
  const panel = $("panel");
  panel.innerHTML = '<p class="empty">Loading</p>';

  let d;
  try {
    d = await json("/api/node/" + nd.id);
  } catch (e) {
    panel.innerHTML = "";
    const err = document.createElement("div");
    err.className = "err";
    err.textContent = "Could not load this node. " + e;
    panel.append(err);
    return;
  }
  if (state.selected !== i) return;   // a later click won

  panel.innerHTML = "";
  const h = document.createElement("h3");
  h.textContent = d.name;
  const where = document.createElement("div");
  where.className = "where";
  where.textContent = d.path + (d.line ? ":" + d.line : "");
  panel.append(h, where);

  const badges = document.createElement("div");
  badges.className = "badges";
  for (const b of [d.kind, d.lang, d.loc ? d.loc + " lines" : null]) {
    if (!b) continue;
    const s = document.createElement("span");
    s.className = "badge"; s.textContent = b;
    badges.append(s);
  }
  panel.append(badges);

  addList(panel, "Called by", d.callers);
  addList(panel, "Calls", d.callees);
  addList(panel, "Defines", d.members);

  if (d.source) {
    const h4 = document.createElement("h4");
    h4.textContent = "Source";
    const pre = document.createElement("pre");
    pre.className = "src";
    pre.textContent = d.source;
    panel.append(h4, pre);
  }
}

function addList(panel, title, items) {
  if (!items || !items.length) return;
  const h = document.createElement("h4");
  h.textContent = title + " (" + items.length + ")";
  const box = document.createElement("div");
  box.className = "list";
  for (const it of items) {
    const b = document.createElement("button");
    const nm = document.createElement("span");
    nm.textContent = it.name;
    const m = document.createElement("span");
    m.className = "m" + (it.uncertain ? " uncertain" : "");
    m.textContent = it.uncertain ? "by name" : (it.kind || "");
    b.append(nm, m);
    b.onclick = () => jumpTo(it.id);
    box.append(b);
  }
  panel.append(h, box);
}

async function jumpTo(id) {
  const i = state.index.get(id);
  if (i === undefined) {
    // Not in this view: switch to the symbol view centred on it.
    await load("symbols", id);
    const j = state.index.get(id);
    if (j !== undefined) { centreOn(j); select(j); }
    return;
  }
  centreOn(i);
  select(i);
}

function centreOn(i) {
  const r = canvas.getBoundingClientRect();
  state.camera.k = Math.max(state.camera.k, 0.9);
  state.camera.x = r.width / 2 - state.x[i] * state.camera.k;
  state.camera.y = r.height / 2 - state.y[i] * state.camera.k;
  invalidate();
}

/* ---------------------------------------------------------------- search */

function wireSearch() {
  const q = $("q"), out = $("results");
  let timer = null, active = -1;

  q.addEventListener("input", () => {
    clearTimeout(timer);
    const term = q.value.trim();
    if (term.length < 2) { out.innerHTML = ""; return; }
    timer = setTimeout(async () => {
      let hits;
      try {
        hits = await json("/api/search?q=" + encodeURIComponent(term));
      } catch { return; }
      out.innerHTML = "";
      active = -1;
      if (!hits.length) {
        const b = document.createElement("button");
        b.disabled = true;
        b.textContent = "No match for " + term;
        out.append(b);
        return;
      }
      for (const hit of hits) {
        const b = document.createElement("button");
        b.textContent = hit.name;
        const s = document.createElement("span");
        s.className = "r-sub";
        s.textContent = (hit.kind ? hit.kind + " - " : "") + hit.path;
        b.append(s);
        b.onclick = () => { out.innerHTML = ""; q.blur(); jumpTo(hit.id); };
        out.append(b);
      }
    }, 140);
  });

  q.addEventListener("keydown", (ev) => {
    const items = [...out.querySelectorAll("button:not([disabled])")];
    if (!items.length) return;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      active = (active + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items.forEach((el, j) => el.dataset.active = j === active ? "1" : "0");
      items[active].scrollIntoView({ block: "nearest" });
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      (items[active] || items[0]).click();
    }
  });
}

/* ----------------------------------------------------------------- theme */

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("codeorbit-theme"); } catch { /* private mode */ }
  if (saved === "light" || saved === "dark") {
    document.documentElement.dataset.theme = saved;
  }
  readTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    readTheme(); invalidate();
  });
}

function toggleTheme() {
  const now = document.documentElement.dataset.theme;
  const dark = now ? now === "dark"
                   : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("codeorbit-theme", next); } catch { /* ignore */ }
  readTheme();
  invalidate();
}

boot();
