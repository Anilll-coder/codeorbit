"""Render the graph as a self-contained interactive HTML page, or as Mermaid.

The HTML embeds its data and its own force layout and ships zero external
requests - no CDN, no fonts, no network. That is not stylistic: the whole tool
is local-first, and a viewer that only works with an internet connection would
contradict that. It also means the file can be emailed, opened from a USB
stick, or committed as a build artefact and still work.

Two views, because they answer different questions:
  modules - one node per file, edges are imports. "How is this project shaped?"
  symbols - functions and methods around a focus, edges are calls. "What does
            this touch?"
"""
from __future__ import annotations

import json
from pathlib import Path

from . import query

MAX_NODES = 400          # beyond this a force layout is a hairball, not a picture


# ------------------------------------------------------------------ data

def module_graph(conn) -> dict:
    nodes = conn.execute(
        "SELECT n.id, n.qname, f.path AS path, f.loc AS loc, f.lang AS lang "
        "FROM nodes n JOIN files f ON f.id = n.file_id "
        "WHERE n.kind = 'module' ORDER BY f.loc DESC LIMIT ?",
        (MAX_NODES,),
    ).fetchall()
    keep = {r["id"] for r in nodes}

    edges = [
        {"source": r["src"], "target": r["dst"], "kind": "imports"}
        for r in conn.execute(
            "SELECT DISTINCT src, dst FROM edges WHERE kind = 'imports'"
        )
        if r["src"] in keep and r["dst"] in keep
    ]

    # A module's weight is how many symbols it defines - a better size signal
    # than raw line count, which rewards boilerplate.
    counts = {
        r["file_id"]: r["c"]
        for r in conn.execute(
            "SELECT file_id, count(*) c FROM nodes WHERE kind != 'module' GROUP BY file_id"
        )
    }
    fids = {r["id"]: r for r in conn.execute("SELECT id, file_id FROM nodes")}

    out_nodes = []
    for r in nodes:
        fid = fids[r["id"]]["file_id"] if r["id"] in fids else None
        out_nodes.append({
            "id": r["id"],
            "label": r["path"].rsplit("/", 1)[-1],
            "full": r["path"],
            "lang": r["lang"],
            "weight": counts.get(fid, 1),
            "meta": f"{r['loc']} lines - {counts.get(fid, 0)} symbols",
        })
    return {"view": "modules", "nodes": out_nodes, "edges": edges}


def symbol_graph(conn, focus_id: int | None = None, depth: int = 2) -> dict:
    """Functions and methods; the whole project, or a neighbourhood of `focus`."""
    if focus_id is None:
        rows = conn.execute(
            "SELECT n.id, n.name, n.qname, n.kind, f.path AS path, f.lang AS lang, "
            "(SELECT count(*) FROM edges e WHERE e.dst = n.id AND e.kind='calls') AS fan_in "
            "FROM nodes n JOIN files f ON f.id = n.file_id "
            "WHERE n.kind IN ('function','method','class') "
            "ORDER BY fan_in DESC LIMIT ?",
            (MAX_NODES,),
        ).fetchall()
        keep = {r["id"] for r in rows}
    else:
        keep = {focus_id}
        frontier = {focus_id}
        for _ in range(max(depth, 1)):
            nxt: set[int] = set()
            for nid in frontier:
                for r in query.callers(conn, nid, limit=40):
                    nxt.add(r["id"])
                for r in query.callees(conn, nid, limit=40):
                    nxt.add(r["id"])
            nxt -= keep
            keep |= nxt
            frontier = nxt
            if len(keep) > MAX_NODES:
                break
        keep = set(list(keep)[:MAX_NODES])
        marks = ",".join("?" * len(keep))
        rows = conn.execute(
            f"SELECT n.id, n.name, n.qname, n.kind, f.path AS path, f.lang AS lang, "
            f"(SELECT count(*) FROM edges e WHERE e.dst = n.id AND e.kind='calls') AS fan_in "
            f"FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.id IN ({marks})",
            tuple(keep),
        ).fetchall()

    edges = [
        {"source": r["src"], "target": r["dst"], "kind": "calls",
         "uncertain": r["resolution"] != "exact"}
        for r in conn.execute(
            "SELECT src, dst, resolution FROM edges WHERE kind = 'calls'"
        )
        if r["src"] in keep and r["dst"] in keep
    ]

    nodes = [{
        "id": r["id"],
        "label": r["name"],
        "full": r["qname"],
        "lang": r["lang"],
        "kind": r["kind"],
        "weight": max(r["fan_in"], 1),
        "focus": r["id"] == focus_id,
        "meta": f"{r['kind']} - {r['path']} - {r['fan_in']} caller(s)",
    } for r in rows]

    return {"view": "symbols", "nodes": nodes, "edges": edges,
            "focus": focus_id}


# ----------------------------------------------------------------- mermaid

def to_mermaid(data: dict, max_nodes: int = 60) -> str:
    """A Mermaid flowchart - pasteable into a report or a README."""
    nodes = sorted(data["nodes"], key=lambda n: -n["weight"])[:max_nodes]
    keep = {n["id"] for n in nodes}
    lines = ["flowchart LR"]

    safe = {}
    for n in nodes:
        ident = f"n{n['id']}"
        safe[n["id"]] = ident
        label = n["label"].replace('"', "'")
        lines.append(f'  {ident}["{label}"]')

    seen = set()
    for e in data["edges"]:
        if e["source"] not in keep or e["target"] not in keep:
            continue
        pair = (e["source"], e["target"])
        if pair in seen or e["source"] == e["target"]:
            continue
        seen.add(pair)
        arrow = "-.->" if e.get("uncertain") else "-->"
        lines.append(f"  {safe[e['source']]} {arrow} {safe[e['target']]}")

    return "\n".join(lines)


# -------------------------------------------------------------------- html

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #0f1117; --panel: #171a23; --line: #262b38; --ink: #e6e9ef;
    --dim: #8b93a7; --accent: #5aa9e6; --warn: #e6a15a; --focus: #7ee081;
    --edge: #3f4757;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f7f8fa; --panel:#fff; --line:#e2e5ea; --ink:#1b1f27;
            --dim:#5c6472; --accent:#1f6fb2; --warn:#a86a1f; --focus:#2b8a3e;
            --edge:#b9c0cc; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:14px 18px; border-bottom:1px solid var(--line);
           display:flex; gap:16px; align-items:baseline; flex-wrap:wrap; }
  h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.01em; }
  .sub { color:var(--dim); font-size:12.5px; }
  main { display:flex; height:calc(100vh - 53px); }
  #stage { flex:1; min-width:0; position:relative; }
  svg { width:100%; height:100%; display:block; cursor:grab; }
  svg.dragging { cursor:grabbing; }
  aside { width:320px; border-left:1px solid var(--line); background:var(--panel);
          padding:16px; overflow:auto; }
  aside h2 { font-size:13px; margin:0 0 4px; }
  aside .path { color:var(--dim); font-size:12px; word-break:break-all; margin-bottom:12px; }
  aside ul { list-style:none; padding:0; margin:0 0 14px; }
  aside li { padding:3px 0; border-bottom:1px solid var(--line); font-size:12.5px; }
  aside li:last-child { border-bottom:0; }
  .lbl { color:var(--dim); text-transform:uppercase; font-size:10.5px;
         letter-spacing:.08em; margin:14px 0 6px; }
  input { width:100%; padding:7px 9px; border-radius:6px; border:1px solid var(--line);
          background:var(--bg); color:var(--ink); font-size:13px; }
  .node { cursor:pointer; }
  .node circle { stroke:var(--bg); stroke-width:1.5; }
  .node text { font-size:10px; fill:var(--dim); pointer-events:none; }
  .node.small text { display:none; }
  .node.sel circle { stroke:var(--ink); stroke-width:2.5; }
  .node.sel text { fill:var(--ink); font-weight:600; }
  .node.faded { opacity:.12; }
  line.edge { stroke:var(--edge); stroke-width:1; opacity:.75; }
  line.edge.uncertain { stroke-dasharray:3 3; }
  line.edge.hot { stroke:var(--accent); stroke-width:2; }
  line.edge.faded { opacity:.06; }
  .legend { display:flex; gap:14px; font-size:12px; color:var(--dim); flex-wrap:wrap; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%;
         margin-right:5px; vertical-align:middle; }
  .empty { color:var(--dim); font-size:12.5px; }
  kbd { background:var(--bg); border:1px solid var(--line); border-radius:4px;
        padding:1px 5px; font-size:11px; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="sub">__SUBTITLE__</span>
  <span class="legend" style="margin-left:auto">
    <span><i class="dot" style="background:#5aa9e6"></i>python</span>
    <span><i class="dot" style="background:#e6a15a"></i>javascript</span>
    <span>dashed = uncertain edge</span>
    <span>drag to pan, scroll to zoom</span>
  </span>
</header>
<main>
  <div id="stage"><svg id="svg"></svg></div>
  <aside>
    <input id="find" placeholder="Filter by name..." autocomplete="off">
    <div class="lbl">Selection</div>
    <div id="detail"><p class="empty">Click a node.</p></div>
  </aside>
</main>
<script>
const DATA = __DATA__;

const svg = document.getElementById('svg');
const NS = 'http://www.w3.org/2000/svg';
const stage = document.getElementById('stage');
let W = stage.clientWidth, H = stage.clientHeight;

const nodes = DATA.nodes.map(n => ({...n,
  x: W/2 + (Math.random()-0.5)*Math.min(W,H)*0.8,
  y: H/2 + (Math.random()-0.5)*Math.min(W,H)*0.8,
  vx: 0, vy: 0}));
const byId = new Map(nodes.map(n => [n.id, n]));
const edges = DATA.edges.filter(e => byId.has(e.source) && byId.has(e.target))
                        .map(e => ({...e, s: byId.get(e.source), t: byId.get(e.target)}));

// Adjacency, so selecting a node can highlight exactly what it touches.
const adj = new Map(nodes.map(n => [n.id, new Set()]));
edges.forEach(e => { adj.get(e.source).add(e.target); adj.get(e.target).add(e.source); });

const root = document.createElementNS(NS, 'g');
const gEdges = document.createElementNS(NS, 'g');
const gNodes = document.createElementNS(NS, 'g');
root.append(gEdges, gNodes); svg.append(root);

const COLOR = { python: '#5aa9e6', javascript: '#e6a15a' };
const radius = n => Math.min(4 + Math.sqrt(n.weight) * 2.2, 22);

const els = new Map();
for (const n of nodes) {
  const g = document.createElementNS(NS, 'g');
  g.setAttribute('class', 'node');
  const c = document.createElementNS(NS, 'circle');
  c.setAttribute('r', radius(n));
  c.setAttribute('fill', n.focus ? 'var(--focus)' : (COLOR[n.lang] || '#8b93a7'));
  const t = document.createElementNS(NS, 'text');
  t.setAttribute('text-anchor', 'middle');
  t.setAttribute('dy', radius(n) + 11);
  t.textContent = n.label.length > 22 ? n.label.slice(0, 21) + '\\u2026' : n.label;
  g.append(c, t);
  g.addEventListener('click', ev => { ev.stopPropagation(); select(n); });
  gNodes.append(g); els.set(n.id, g);
}

const edgeEls = edges.map(e => {
  const l = document.createElementNS(NS, 'line');
  l.setAttribute('class', 'edge' + (e.uncertain ? ' uncertain' : ''));
  gEdges.append(l);
  return l;
});

// --- force layout: repulsion, spring edges, gentle centring -----------------
// Constants are tuned against a dense real graph (rich: 100 nodes / 404 edges).
// The first attempt used a strong spring and a short repulsion cutoff, and the
// whole graph collapsed into one unreadable knot: with ~8 edges per node the
// springs simply outvote repulsion. Repulsion has to be strong, long-range, and
// scaled by node count, or a dense graph implodes.
let alpha = 1;
const REPEL = 260 * Math.max(nodes.length, 30);
const SPRING = 0.0035;
const IDEAL = 110;
const CUTOFF2 = 1400 * 1400;      // generous: short cutoffs are what caused the knot

function tick() {
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      let d2 = dx*dx + dy*dy;
      if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.01; }
      if (d2 > CUTOFF2) continue;
      const d = Math.sqrt(d2);
      const f = REPEL / d2;
      const fx = (dx/d) * f, fy = (dy/d) * f;
      a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
    }
  }
  for (const e of edges) {
    const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
    const d = Math.hypot(dx, dy) || 0.01;
    const f = (d - IDEAL) * SPRING;
    const fx = (dx/d) * f, fy = (dy/d) * f;
    e.s.vx += fx; e.s.vy += fy; e.t.vx -= fx; e.t.vy -= fy;
  }
  for (const n of nodes) {
    n.vx += (W/2 - n.x) * 0.0006;
    n.vy += (H/2 - n.y) * 0.0006;
    const damp = 0.86;
    n.vx *= damp; n.vy *= damp;
    // Cap per-frame movement so a big repulsion spike cannot fling a node off
    // screen before the springs can answer.
    n.x += Math.max(-30, Math.min(30, n.vx));
    n.y += Math.max(-30, Math.min(30, n.vy));
  }
  alpha *= 0.992;
}

// Settle most of the way before the first paint. Animating every iteration
// looks lively but takes ~11s at 60fps to converge, and the file is opened to
// read a graph, not to watch one assemble. Warm up silently, then animate the
// remainder so it still arrives alive rather than frozen.
function step(warmup) {
  if (warmup) {
    for (let i = 0; i < 220 && alpha > 0.004; i++) tick();
    draw(); fitView();
  }
  if (alpha < 0.004) { draw(); fitView(); return; }
  for (let i = 0; i < 3; i++) { if (alpha > 0.004) tick(); }
  draw(); fitView();
  requestAnimationFrame(() => step(false));
}

// Frame the result rather than trusting it to land in view. Without this the
// drawing can settle anywhere, at any scale, and the user opens the file to an
// empty canvas with the graph somewhere off-screen.
function fitView() {
  if (!nodes.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of nodes) {
    const r = radius(n) + 14;
    x0 = Math.min(x0, n.x - r); y0 = Math.min(y0, n.y - r);
    x1 = Math.max(x1, n.x + r); y1 = Math.max(y1, n.y + r);
  }
  const w = Math.max(x1 - x0, 1), h = Math.max(y1 - y0, 1);
  const pad = 30;
  scale = Math.min((W - pad*2) / w, (H - pad*2) / h, 2.5);
  tx = pad + (W - pad*2 - w*scale)/2 - x0*scale;
  ty = pad + (H - pad*2 - h*scale)/2 - y0*scale;
  applyView();
}

function draw() {
  edges.forEach((e, i) => {
    const l = edgeEls[i];
    l.setAttribute('x1', e.s.x); l.setAttribute('y1', e.s.y);
    l.setAttribute('x2', e.t.x); l.setAttribute('y2', e.t.y);
  });
  for (const n of nodes) els.get(n.id).setAttribute('transform', `translate(${n.x},${n.y})`);
}

// --- pan + zoom -------------------------------------------------------------
let tx = 0, ty = 0, scale = 1, panning = false, px = 0, py = 0;
const applyView = () => root.setAttribute('transform', `translate(${tx},${ty}) scale(${scale})`);
svg.addEventListener('mousedown', e => { panning = true; px = e.clientX; py = e.clientY;
                                          svg.classList.add('dragging'); });
addEventListener('mouseup', () => { panning = false; svg.classList.remove('dragging'); });
addEventListener('mousemove', e => {
  if (!panning) return;
  tx += e.clientX - px; ty += e.clientY - py; px = e.clientX; py = e.clientY; applyView();
});
svg.addEventListener('wheel', e => {
  e.preventDefault();
  const k = e.deltaY < 0 ? 1.12 : 1/1.12;
  const r = svg.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  tx = mx - (mx - tx) * k; ty = my - (my - ty) * k; scale *= k; applyView();
}, {passive:false});
svg.addEventListener('click', () => select(null));

// --- selection --------------------------------------------------------------
const detail = document.getElementById('detail');
function select(n) {
  els.forEach(g => g.classList.remove('sel', 'faded'));
  edgeEls.forEach(l => l.classList.remove('hot', 'faded'));

  if (!n) { detail.innerHTML = '<p class="empty">Click a node.</p>'; return; }

  const near = adj.get(n.id);
  els.forEach((g, id) => {
    if (id === n.id) g.classList.add('sel');
    else if (!near.has(id)) g.classList.add('faded');
  });
  edges.forEach((e, i) => {
    if (e.source === n.id || e.target === n.id) edgeEls[i].classList.add('hot');
    else edgeEls[i].classList.add('faded');
  });

  const ins  = edges.filter(e => e.target === n.id).map(e => e.s.full);
  const outs = edges.filter(e => e.source === n.id).map(e => e.t.full);
  const list = (title, arr) => arr.length
    ? `<div class="lbl">${title} (${arr.length})</div><ul>` +
      arr.slice(0, 40).map(x => `<li>${esc(x)}</li>`).join('') + '</ul>'
    : `<div class="lbl">${title}</div><p class="empty">none</p>`;

  detail.innerHTML =
    `<h2>${esc(n.label)}</h2><div class="path">${esc(n.full)}<br>${esc(n.meta)}</div>` +
    list(DATA.view === 'modules' ? 'imported by' : 'called by', ins) +
    list(DATA.view === 'modules' ? 'imports' : 'calls', outs);
}
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

// --- filter -----------------------------------------------------------------
document.getElementById('find').addEventListener('input', e => {
  const q = e.target.value.trim().toLowerCase();
  els.forEach((g, id) => {
    const n = byId.get(id);
    g.classList.toggle('faded', !!q && !n.full.toLowerCase().includes(q));
  });
});

addEventListener('resize', () => {
  W = stage.clientWidth; H = stage.clientHeight; alpha = Math.max(alpha, 0.15);
  step(false);
});
step(true);
</script>
</body>
</html>
"""


def _js_safe_json(data: dict) -> str:
    """JSON safe to embed inside a <script> block.

    json.dumps does NOT escape `<`, so a symbol literally named `</script>`
    closes the block early and everything after it is parsed as HTML. Since
    every string in here comes from the codebase being analysed - symbol names,
    file paths - that is attacker-controlled input whenever someone points this
    at a repository they did not write. Escaping to \\u003c keeps the value
    identical to JavaScript while making the sequence impossible to form.
    """
    return (json.dumps(data)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace(" ", "\\u2028")   # JS line terminators, illegal raw
            .replace(" ", "\\u2029"))


def _html_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def render_html(data: dict, title: str, subtitle: str) -> str:
    return (HTML
            .replace("__DATA__", _js_safe_json(data))
            .replace("__TITLE__", _html_escape(title))
            .replace("__SUBTITLE__", _html_escape(subtitle)))
