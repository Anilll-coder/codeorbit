/* Force layout, off the main thread.
 *
 * The naive version of this is O(n^2) per tick: every node repels every other
 * node. At 300 nodes nobody notices. At 3000 it is 9 million distance
 * calculations per frame and the page stops responding to the mouse, which is
 * exactly when a graph of a real repository gets interesting.
 *
 * So: Barnes-Hut. Build a quadtree each tick, and when a whole subtree is far
 * enough away relative to its size, treat it as one body at its centre of mass
 * instead of descending into it. That takes the tick to O(n log n).
 *
 * Running it in a Worker matters just as much. Layout and rendering want the
 * same milliseconds, and if they share a thread the drag lags behind the
 * cursor. Here the main thread only ever draws the last positions it was sent.
 */

const THETA = 0.9;          // higher approximates more aggressively
const THETA2 = THETA * THETA;
const MAX_DEPTH = 26;

let N = 0;
let x, y, vx, vy, mass, deg;
let eSrc, eDst;             // edge endpoints, as node indices
let alpha = 1, alphaTarget = 0;
const ALPHA_MIN = 0.0015;
const ALPHA_DECAY = 0.0225;
const VELOCITY_DECAY = 0.6;
let running = false;
let pinned = -1, pinX = 0, pinY = 0;

let repulsion = -260;
let linkDist = 34;
let gravity = 0.055;

/* ------------------------------------------------------------- quadtree */
/* Flat arrays, grown once and reused. child[] encodes:
 *   -1            empty
 *   <= -2         a single point, index -(v + 2)
 *   >= 0          an internal node index                                  */

let qChild, qMass, qCx, qCy, qBx, qBy, qHalf;
let qCap = 0, qCount = 0;

function ensureTreeCapacity(cap) {
  if (cap <= qCap) return;
  qCap = Math.max(cap, 64);
  qChild = new Int32Array(qCap * 4);
  qMass = new Float32Array(qCap);
  qCx = new Float32Array(qCap);
  qCy = new Float32Array(qCap);
  qBx = new Float32Array(qCap);
  qBy = new Float32Array(qCap);
  qHalf = new Float32Array(qCap);
}

function newNode(bx, by, half) {
  const i = qCount++;
  const o = i * 4;
  qChild[o] = qChild[o + 1] = qChild[o + 2] = qChild[o + 3] = -1;
  qMass[i] = 0;
  qCx[i] = 0;
  qCy[i] = 0;
  qBx[i] = bx;
  qBy[i] = by;
  qHalf[i] = half;
  return i;
}

function buildTree() {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (let i = 0; i < N; i++) {
    if (x[i] < minX) minX = x[i];
    if (x[i] > maxX) maxX = x[i];
    if (y[i] < minY) minY = y[i];
    if (y[i] > maxY) maxY = y[i];
  }
  if (!isFinite(minX)) return -1;

  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(maxX - minX, maxY - minY) / 2 + 1;

  // Worst case one internal node per level per point; this bound is generous
  // and allocated once, so the tick itself never allocates.
  ensureTreeCapacity(4 * N + 64);
  qCount = 0;
  const root = newNode(cx, cy, half);

  for (let p = 0; p < N; p++) insert(root, p);
  return root;
}

function insert(root, p) {
  const px = x[p], py = y[p], pm = mass[p];
  let node = root;

  for (let depth = 0; depth < MAX_DEPTH; depth++) {
    // Accumulate the centre of mass on the way down, so no second pass is
    // needed to make internal nodes usable as approximations.
    const m = qMass[node] + pm;
    qCx[node] = (qCx[node] * qMass[node] + px * pm) / m;
    qCy[node] = (qCy[node] * qMass[node] + py * pm) / m;
    qMass[node] = m;

    const half = qHalf[node] / 2;
    const right = px >= qBx[node] ? 1 : 0;
    const below = py >= qBy[node] ? 1 : 0;
    const q = right + below * 2;
    const o = node * 4 + q;
    const c = qChild[o];
    const bx = qBx[node] + (right ? half : -half);
    const by = qBy[node] + (below ? half : -half);

    if (c === -1) { qChild[o] = -(p + 2); return; }

    if (c <= -2) {
      // Occupied by one point: push both down into a fresh internal node.
      const other = -c - 2;
      const inner = newNode(bx, by, half);
      qChild[o] = inner;
      // Seed the new node with the point that was already there.
      qMass[inner] = mass[other];
      qCx[inner] = x[other];
      qCy[inner] = y[other];
      const oHalf = half / 2;
      const oRight = x[other] >= bx ? 1 : 0;
      const oBelow = y[other] >= by ? 1 : 0;
      qChild[inner * 4 + oRight + oBelow * 2] = -(other + 2);
      void oHalf;
      node = inner;
      continue;
    }
    node = c;
  }
  // Deeper than MAX_DEPTH means coincident points. Their mass is already
  // accumulated above, which is the behaviour we want: they push as one.
}

function applyRepulsion(root, p) {
  const px = x[p], py = y[p];
  let fx = 0, fy = 0;
  const stack = repelStack;
  let top = 0;
  stack[top++] = root;

  while (top > 0) {
    const node = stack[--top];
    const o = node * 4;

    for (let q = 0; q < 4; q++) {
      const c = qChild[o + q];
      if (c === -1) continue;

      let cx, cy, m, size;
      if (c <= -2) {
        const other = -c - 2;
        if (other === p) continue;
        cx = x[other]; cy = y[other]; m = mass[other]; size = 0;
      } else {
        cx = qCx[c]; cy = qCy[c]; m = qMass[c]; size = qHalf[c] * 2;
      }

      let dx = cx - px, dy = cy - py;
      let d2 = dx * dx + dy * dy;
      if (d2 < 0.01) {
        // Coincident: nudge deterministically so they separate instead of
        // sitting on top of each other forever.
        dx = ((p % 7) - 3) * 0.5;
        dy = ((p % 5) - 2) * 0.5;
        d2 = dx * dx + dy * dy || 1;
      }

      if (size === 0 || (size * size) < THETA2 * d2) {
        const f = (repulsion * m) / (d2 * Math.sqrt(d2));
        fx += dx * f;
        fy += dy * f;
      } else {
        stack[top++] = c;
      }
    }
  }
  vx[p] += fx;
  vy[p] += fy;
}

let repelStack = new Int32Array(4096);

/* ------------------------------------------------------------------ tick */

function tick() {
  const root = buildTree();
  if (root < 0) return;

  if (repelStack.length < qCount + 8) repelStack = new Int32Array(qCount * 2 + 64);

  for (let p = 0; p < N; p++) applyRepulsion(root, p);

  // Springs. One pass over edges, symmetric.
  const k = alpha;
  for (let e = 0; e < eSrc.length; e++) {
    const a = eSrc[e], b = eDst[e];
    let dx = x[b] - x[a], dy = y[b] - y[a];
    let d = Math.sqrt(dx * dx + dy * dy) || 1;
    // Well-connected nodes should not be dragged around by every neighbour.
    const bias = deg[a] / (deg[a] + deg[b] || 1);
    const f = ((d - linkDist) / d) * k * 0.42;
    dx *= f; dy *= f;
    vx[b] -= dx * bias; vy[b] -= dy * bias;
    vx[a] += dx * (1 - bias); vy[a] += dy * (1 - bias);
  }

  // Gravity toward the origin keeps disconnected components from drifting off.
  for (let p = 0; p < N; p++) {
    vx[p] -= x[p] * gravity * alpha;
    vy[p] -= y[p] * gravity * alpha;
  }

  for (let p = 0; p < N; p++) {
    if (p === pinned) { x[p] = pinX; y[p] = pinY; vx[p] = vy[p] = 0; continue; }
    vx[p] *= VELOCITY_DECAY;
    vy[p] *= VELOCITY_DECAY;
    x[p] += vx[p] * alpha;
    y[p] += vy[p] * alpha;
  }

  alpha += (alphaTarget - alpha) * ALPHA_DECAY;
}

function loop() {
  if (!running) return;

  // Converge fast while the layout is still hot, then settle into one tick per
  // frame so dragging stays responsive.
  const budget = alpha > 0.35 ? 4 : alpha > 0.1 ? 2 : 1;
  for (let i = 0; i < budget; i++) tick();

  const px = new Float32Array(x), py = new Float32Array(y);
  self.postMessage({ type: "tick", x: px, y: py, alpha }, [px.buffer, py.buffer]);

  if (alpha < ALPHA_MIN && alphaTarget === 0 && pinned < 0) {
    running = false;
    self.postMessage({ type: "settled" });
    return;
  }
  setTimeout(loop, 16);
}

self.onmessage = (ev) => {
  const m = ev.data;

  if (m.type === "init") {
    N = m.n;
    eSrc = m.src;
    eDst = m.dst;
    x = new Float32Array(N);
    y = new Float32Array(N);
    vx = new Float32Array(N);
    vy = new Float32Array(N);
    mass = m.mass;
    deg = m.deg;

    // A phyllotaxis seed spreads the first frame evenly. Random seeding makes
    // the layout spend its whole budget undoing clumps it created itself.
    const spread = Math.sqrt(N) * linkDist * 0.55;
    for (let i = 0; i < N; i++) {
      const a = i * 2.399963229728653;
      const r = spread * Math.sqrt((i + 0.5) / N);
      x[i] = Math.cos(a) * r;
      y[i] = Math.sin(a) * r;
    }

    // Denser graphs need more room, or every node lands on top of its
    // neighbours and the picture is a blot.
    // Repulsion has to grow with the node count or a big graph collapses into
    // a single dense knot: the springs and gravity scale with n, a fixed charge
    // does not. Logarithmic rather than linear, which would blow a large graph
    // apart faster than the springs can pull it back.
    const density = eSrc.length / Math.max(N, 1);
    linkDist = 30 + Math.min(density * 6, 40);
    repulsion = -(140 + 46 * Math.log2(Math.max(N, 2))) * (1 + Math.min(density, 3) * 0.2);
    // Gravity only exists to keep disconnected pieces from drifting off the
    // canvas. On a big graph it otherwise fights the repulsion and wins.
    gravity = 0.055 * Math.min(1, 420 / Math.max(N, 1));

    alpha = 1;
    alphaTarget = 0;
    running = true;
    loop();
    return;
  }

  if (m.type === "pin") {
    pinned = m.i; pinX = m.x; pinY = m.y;
    if (m.i >= 0) { alphaTarget = 0.22; if (!running) { running = true; loop(); } }
    else { alphaTarget = 0; }
    return;
  }

  if (m.type === "reheat") {
    alpha = Math.max(alpha, m.to || 0.5);
    alphaTarget = 0;
    if (!running) { running = true; loop(); }
    return;
  }

  if (m.type === "stop") { running = false; }
};
