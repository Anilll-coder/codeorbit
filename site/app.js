(() => {
  'use strict';

  const reduceMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  const reducedMotion = () => reduceMotionQuery.matches;

  /* ------------------------------------------------------------ nav */

  const nav = document.getElementById('nav');
  const onScroll = () => {
    nav.classList.toggle('is-scrolled', window.scrollY > 8);
  };
  onScroll();
  window.addEventListener('scroll', onScroll, { passive: true });

  const menuToggle = document.getElementById('menu-toggle');
  const mobileMenu = document.getElementById('mobile-menu');
  menuToggle.addEventListener('click', () => {
    const open = mobileMenu.classList.toggle('is-open');
    menuToggle.setAttribute('aria-expanded', String(open));
    document.body.style.overflow = open ? 'hidden' : '';
  });
  mobileMenu.querySelectorAll('a').forEach((a) => {
    a.addEventListener('click', () => {
      mobileMenu.classList.remove('is-open');
      menuToggle.setAttribute('aria-expanded', 'false');
      document.body.style.overflow = '';
    });
  });

  /* ------------------------------------------------------------ scroll reveal */

  const revealItems = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && revealItems.length) {
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-visible');
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.15, rootMargin: '0px 0px -40px 0px' }
    );
    revealItems.forEach((el) => io.observe(el));
  } else {
    revealItems.forEach((el) => el.classList.add('is-visible'));
  }

  /* ------------------------------------------------------------ install tabs */

  const COMMANDS = {
    sh: {
      prompt: '$',
      code: 'curl -fsSL https://raw.githubusercontent.com/Anilll-coder/codeorbit/main/install.sh | sh',
    },
    ps1: {
      prompt: '>',
      code: 'irm https://raw.githubusercontent.com/Anilll-coder/codeorbit/main/install.ps1 | iex',
    },
  };

  const tabs = document.querySelectorAll('.install-tab');
  const codeEl = document.getElementById('install-cmd');
  const promptEl = document.querySelector('.install-code .prompt');

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      tabs.forEach((t) => {
        t.classList.remove('is-active');
        t.setAttribute('aria-selected', 'false');
      });
      tab.classList.add('is-active');
      tab.setAttribute('aria-selected', 'true');
      const target = COMMANDS[tab.dataset.target];
      codeEl.textContent = target.code;
      promptEl.textContent = target.prompt;
    });
  });

  const copyBtn = document.getElementById('copy-install');
  const copyIcon = document.getElementById('copy-icon');
  const CHECK_PATH = '<polyline points="4 10.5 8 14.5 16 5.5" />';
  const COPY_PATH =
    '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M4 13V5a2 2 0 012-2h8"/>';

  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(codeEl.textContent.trim());
      copyBtn.classList.add('is-copied');
      copyIcon.innerHTML = CHECK_PATH;
      copyBtn.setAttribute('aria-label', 'Copied');
      setTimeout(() => {
        copyBtn.classList.remove('is-copied');
        copyIcon.innerHTML = COPY_PATH;
        copyBtn.setAttribute('aria-label', 'Copy install command');
      }, 1600);
    } catch (err) {
      /* clipboard unavailable; command remains selectable as text */
    }
  });

  /* ------------------------------------------------------------ terminal demo */

  const terminalBody = document.getElementById('terminal-body');
  const terminalWindow = document.getElementById('terminal');

  const SCRIPT = [
    { type: 'cmd', text: 'codeorbit index .' },
    { type: 'out', cls: 'out', text: '1,193 symbols · 5,381 edges · 61% of call sites resolved' },
    { type: 'gap' },
    { type: 'cmd', text: 'codeorbit ask "How does Console.print render output?"' },
    {
      type: 'out',
      cls: 'accent-out',
      text:
        'Console.print resolves each renderable through __rich_console__, then Console._collect_renderables walks the results and Console._render_buffer writes segments to the terminal.',
    },
    { type: 'out', cls: 'dim', text: 'Grounded in 4 symbols, 6 call edges. 1 edge marked [uncertain].' },
  ];

  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderStatic() {
    terminalBody.innerHTML = SCRIPT.map((line) => {
      if (line.type === 'gap') return '<div class="line">&nbsp;</div>';
      if (line.type === 'cmd') {
        return `<div class="line"><span class="prompt">$</span> <span class="cmd">${escapeHtml(line.text)}</span></div>`;
      }
      return `<div class="line ${line.cls}">${escapeHtml(line.text)}</div>`;
    }).join('');
  }

  function typeSequence() {
    let i = 0;

    function nextLine() {
      if (i >= SCRIPT.length) {
        const caretLine = document.createElement('div');
        caretLine.className = 'line';
        caretLine.innerHTML = '<span class="prompt">$</span> <span class="caret"></span>';
        terminalBody.appendChild(caretLine);
        return;
      }
      const line = SCRIPT[i];
      i += 1;

      if (line.type === 'gap') {
        const div = document.createElement('div');
        div.className = 'line';
        div.innerHTML = '&nbsp;';
        terminalBody.appendChild(div);
        nextLine();
        return;
      }

      if (line.type === 'cmd') {
        const div = document.createElement('div');
        div.className = 'line';
        div.innerHTML = '<span class="prompt">$</span> <span class="cmd"></span><span class="caret"></span>';
        terminalBody.appendChild(div);
        const cmdSpan = div.querySelector('.cmd');
        const caret = div.querySelector('.caret');
        let c = 0;
        const chars = line.text.split('');
        const step = () => {
          if (c < chars.length) {
            cmdSpan.textContent += chars[c];
            c += 1;
            setTimeout(step, 16 + Math.random() * 22);
          } else {
            caret.remove();
            setTimeout(nextLine, 260);
          }
        };
        step();
        return;
      }

      // out: reveal word by word
      const div = document.createElement('div');
      div.className = `line ${line.cls}`;
      terminalBody.appendChild(div);
      const words = line.text.split(' ');
      let w = 0;
      const step = () => {
        if (w < words.length) {
          div.textContent += (w === 0 ? '' : ' ') + words[w];
          w += 1;
          setTimeout(step, 34);
        } else {
          setTimeout(nextLine, 200);
        }
      };
      step();
    }

    nextLine();
  }

  let terminalPlayed = false;
  if ('IntersectionObserver' in window) {
    const termIo = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting && !terminalPlayed) {
            terminalPlayed = true;
            if (reducedMotion()) {
              renderStatic();
            } else {
              typeSequence();
            }
            termIo.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.4 }
    );
    termIo.observe(terminalWindow);
  } else {
    renderStatic();
  }

  /* ------------------------------------------------------------ pipeline stagger */

  const pipeline = document.getElementById('pipeline');
  const stages = pipeline ? pipeline.querySelectorAll('.pipeline-stage') : [];
  let pipelinePlayed = false;

  if (pipeline && 'IntersectionObserver' in window) {
    const pipeIo = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting && !pipelinePlayed) {
            pipelinePlayed = true;
            stages.forEach((stage) => {
              const idx = Number(stage.dataset.i || 0);
              const delay = reducedMotion() ? 0 : idx * 130;
              setTimeout(() => stage.classList.add('is-active'), delay);
            });
            pipeIo.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.25 }
    );
    pipeIo.observe(pipeline);
  } else {
    stages.forEach((s) => s.classList.add('is-active'));
  }

  /* ------------------------------------------------------------ orbit graph */

  const canvas = document.getElementById('orbit-canvas');
  const stage = document.getElementById('hero-stage');
  const labelEl = document.getElementById('hero-node-label');

  if (canvas && stage) {
    const ctx = canvas.getContext('2d');

    const SYMBOL_NAMES = [
      'scanner.scan', 'indexer.write_node', 'resolve.bind_call', 'query.callers',
      'query.impact', 'context.build_prompt', 'review.blast_radius', 'audit.rank_findings',
      'viz.force_layout', 'llm.stream', 'extract.walk_tree', 'db.connect',
      'rules.check_pattern', 'semantic.embed_symbol', 'fixer.propose_fix', 'verify.run_gates',
      'cli.main', 'Console.print', 'Segment.split', 'make_user',
      'normalize', 'parse_and_extract', 'resolve_project', 'Table.add_row', 'Style.parse',
    ];

    const NODE_COUNT = SYMBOL_NAMES.length;
    let nodes = [];
    let edges = [];
    let width = 0;
    let height = 0;
    let dpr = Math.min(window.devicePixelRatio || 1, 2);

    function seededRandom(seed) {
      let s = seed;
      return () => {
        s = (s * 9301 + 49297) % 233280;
        return s / 233280;
      };
    }

    function buildGraph() {
      const rand = seededRandom(42);
      const R = 150;
      nodes = SYMBOL_NAMES.map((name, i) => {
        const u = rand();
        const v = rand();
        const theta = u * Math.PI * 2;
        const phi = Math.acos(2 * v - 1);
        const r = R * (0.7 + rand() * 0.3);
        return {
          name,
          id: i,
          x: r * Math.sin(phi) * Math.cos(theta),
          y: r * Math.sin(phi) * Math.sin(theta),
          z: r * Math.cos(phi),
        };
      });

      const pairKey = (a, b) => (a < b ? `${a}-${b}` : `${b}-${a}`);
      const seen = new Set();
      edges = [];

      function dist(a, b) {
        return Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
      }

      nodes.forEach((node, i) => {
        const distances = nodes
          .map((other, j) => ({ j, d: dist(node, other) }))
          .filter((e) => e.j !== i)
          .sort((a, b) => a.d - b.d)
          .slice(0, 2);
        distances.forEach(({ j }) => {
          const key = pairKey(i, j);
          if (!seen.has(key)) {
            seen.add(key);
            edges.push([i, j]);
          }
        });
      });

      // a handful of longer cross-cluster edges for visual richness
      for (let k = 0; k < 5; k += 1) {
        const a = Math.floor(rand() * NODE_COUNT);
        const b = Math.floor(rand() * NODE_COUNT);
        if (a === b) continue;
        const key = pairKey(a, b);
        if (!seen.has(key)) {
          seen.add(key);
          edges.push([a, b]);
        }
      }

      const degree = new Array(NODE_COUNT).fill(0);
      edges.forEach(([a, b]) => {
        degree[a] += 1;
        degree[b] += 1;
      });
      nodes.forEach((n, i) => { n.degree = degree[i]; });
    }

    buildGraph();

    let autoRotY = 0.4;
    let curTiltX = -0.18;
    let curTiltY = 0;
    let targetTiltX = -0.18;
    let targetTiltY = 0;
    let rotY = autoRotY;
    let rotX = curTiltX;
    let hoverId = null;
    let selectedId = null;
    let pointerActive = false;

    function resize() {
      const rect = stage.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // Resizing (including ResizeObserver's guaranteed async first callback)
      // clears the canvas bitmap. Without a running animation loop
      // (prefers-reduced-motion, or before the first frame fires) nothing
      // would repaint it, so redraw right here.
      draw();
    }

    if ('ResizeObserver' in window) {
      new ResizeObserver(resize).observe(stage);
    } else {
      window.addEventListener('resize', resize, { passive: true });
    }
    resize();

    function project(p, cx, cy, focal, camZ) {
      const cosY = Math.cos(rotY);
      const sinY = Math.sin(rotY);
      const x1 = p.x * cosY - p.z * sinY;
      const z1 = p.x * sinY + p.z * cosY;

      const cosX = Math.cos(rotX);
      const sinX = Math.sin(rotX);
      const y1 = p.y * cosX - z1 * sinX;
      const z2 = p.y * sinX + z1 * cosX;

      const depth = camZ + z2;
      const scale = focal / Math.max(depth, 1);
      return {
        sx: cx + x1 * scale,
        sy: cy + y1 * scale,
        scale,
        z: z2,
      };
    }

    function neighborsOf(id) {
      const set = new Set();
      edges.forEach(([a, b]) => {
        if (a === id) set.add(b);
        if (b === id) set.add(a);
      });
      return set;
    }

    function draw() {
      if (!width || !height) return;
      ctx.clearRect(0, 0, width, height);

      const cx = width / 2;
      const cy = height / 2;
      const focal = Math.min(width, height) * 0.9;
      const camZ = Math.min(width, height) * 0.62;

      const projected = nodes.map((n) => project(n, cx, cy, focal, camZ));
      const activeId = selectedId !== null ? selectedId : hoverId;
      const activeNeighbors = activeId !== null ? neighborsOf(activeId) : null;

      // edges
      edges.forEach(([a, b]) => {
        const pa = projected[a];
        const pb = projected[b];
        const isActiveEdge = activeId !== null && (a === activeId || b === activeId);
        ctx.beginPath();
        ctx.moveTo(pa.sx, pa.sy);
        ctx.lineTo(pb.sx, pb.sy);
        if (isActiveEdge) {
          ctx.strokeStyle = 'rgba(226, 161, 63, 0.85)';
          ctx.lineWidth = 1.4;
        } else {
          const avgZ = (pa.z + pb.z) / 2;
          const fade = Math.max(0.08, Math.min(0.28, 0.2 + avgZ / 900));
          ctx.strokeStyle = `rgba(237, 236, 230, ${fade})`;
          ctx.lineWidth = 0.9;
        }
        ctx.stroke();
      });

      // nodes, back to front
      const order = projected
        .map((p, i) => ({ p, i }))
        .sort((a, b) => a.p.z - b.p.z);

      order.forEach(({ p, i }) => {
        const node = nodes[i];
        const isActive = i === activeId;
        const isNeighbor = activeNeighbors ? activeNeighbors.has(i) : false;
        const dim = activeId !== null && !isActive && !isNeighbor;

        const baseR = 3.4 + node.degree * 0.5;
        const r = Math.max(2, baseR * (p.scale / (focal / camZ)));

        ctx.beginPath();
        ctx.arc(p.sx, p.sy, isActive ? r * 1.6 : r, 0, Math.PI * 2);

        if (isActive) {
          ctx.fillStyle = '#e2a13f';
        } else if (isNeighbor) {
          ctx.fillStyle = 'rgba(237, 236, 230, 0.85)';
        } else {
          ctx.fillStyle = dim ? 'rgba(155, 156, 164, 0.35)' : 'rgba(155, 156, 164, 0.75)';
        }
        ctx.fill();

        if (isActive) {
          ctx.beginPath();
          ctx.arc(p.sx, p.sy, r * 1.6 + 5, 0, Math.PI * 2);
          ctx.strokeStyle = 'rgba(226, 161, 63, 0.35)';
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }
      });
    }

    function findNodeAt(px, py) {
      const cx = width / 2;
      const cy = height / 2;
      const focal = Math.min(width, height) * 0.9;
      const camZ = Math.min(width, height) * 0.62;
      let best = null;
      let bestDist = 16;
      nodes.forEach((n, i) => {
        const p = project(n, cx, cy, focal, camZ);
        const d = Math.hypot(p.sx - px, p.sy - py);
        if (d < bestDist) {
          bestDist = d;
          best = i;
        }
      });
      return best;
    }

    function setLabel(id) {
      if (id === null) {
        if (selectedId === null) {
          labelEl.innerHTML = 'hover or tap a node to inspect it';
          labelEl.classList.add('is-visible');
        }
        return;
      }
      const n = nodes[id];
      labelEl.innerHTML = `<span class="k">${n.name}</span> · connects to ${n.degree} symbol${n.degree === 1 ? '' : 's'}`;
      labelEl.classList.add('is-visible');
    }

    setLabel(null);

    canvas.addEventListener('pointermove', (e) => {
      const rect = canvas.getBoundingClientRect();
      const px = e.clientX - rect.left;
      const py = e.clientY - rect.top;

      if (!reducedMotion()) {
        targetTiltY = ((px / width) - 0.5) * 0.7;
        targetTiltX = -0.18 - ((py / height) - 0.5) * 0.5;
      }

      const hit = findNodeAt(px, py);
      if (hit !== hoverId) {
        hoverId = hit;
        canvas.style.cursor = hit !== null ? 'pointer' : 'default';
        if (selectedId === null) setLabel(hit);
      }
    });

    canvas.addEventListener('pointerleave', () => {
      hoverId = null;
      targetTiltX = -0.18;
      targetTiltY = 0;
      if (selectedId === null) setLabel(null);
    });

    canvas.addEventListener('pointerdown', () => { pointerActive = true; });
    window.addEventListener('pointerup', () => { pointerActive = false; });

    canvas.addEventListener('click', (e) => {
      const rect = canvas.getBoundingClientRect();
      const px = e.clientX - rect.left;
      const py = e.clientY - rect.top;
      const hit = findNodeAt(px, py);
      selectedId = selectedId === hit ? null : hit;
      setLabel(selectedId !== null ? selectedId : hoverId);
      if (reducedMotion()) draw();
    });

    let lastTime = performance.now();
    const baseSpin = 0.00016;

    function loop(now) {
      const dt = now - lastTime;
      lastTime = now;

      if (!reducedMotion()) {
        autoRotY += baseSpin * dt;
        curTiltX += (targetTiltX - curTiltX) * 0.06;
        curTiltY += (targetTiltY - curTiltY) * 0.06;
        rotY = autoRotY + curTiltY;
        rotX = curTiltX;
        draw();
        requestAnimationFrame(loop);
      }
    }

    if (reducedMotion()) {
      draw();
    } else {
      requestAnimationFrame(loop);
    }

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && !reducedMotion()) {
        lastTime = performance.now();
        requestAnimationFrame(loop);
      }
    });
  }
})();
