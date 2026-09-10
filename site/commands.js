/* The interactive command reference.
 *
 * Everything rendered here comes from site/commands.json, which is generated
 * from the CLI by scripts/gen_commands.py. Nothing about a flag is written
 * twice, so the page cannot document an option that no longer exists.
 *
 * Progressive enhancement: the section is marked hidden in the markup and only
 * revealed once the data has loaded, so a failed fetch leaves no empty shell.
 */
(function () {
  "use strict";

  const root = document.getElementById("usage");
  if (!root) return;

  const listEl = document.getElementById("cmd-list");
  const detailEl = document.getElementById("cmd-detail");
  const filterEl = document.getElementById("cmd-filter");
  const countEl = document.getElementById("cmd-count");

  let COMMANDS = [];
  let PANELS = [];
  let current = null;

  fetch("commands.json")
    .then((r) => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    })
    .then((data) => {
      COMMANDS = data.commands || [];
      PANELS = data.panels || [];
      if (!COMMANDS.length) throw new Error("no commands");
      root.hidden = false;
      renderList("");
      selectFromHash();
    })
    .catch(() => {
      // The rest of the page is unaffected; this section simply stays away.
      root.remove();
    });

  /* ------------------------------------------------------------- helpers */

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function matches(cmd, q) {
    if (!q) return true;
    const hay = (cmd.name + " " + cmd.summary + " " + cmd.description + " " +
      cmd.options.map((o) => o.name + " " + o.help).join(" ")).toLowerCase();
    return hay.includes(q);
  }

  /* ---------------------------------------------------------------- list */

  function renderList(q) {
    listEl.innerHTML = "";
    const hits = COMMANDS.filter((c) => matches(c, q));

    countEl.textContent = q
      ? `${hits.length} of ${COMMANDS.length}`
      : `${COMMANDS.length} commands`;

    if (!hits.length) {
      const none = el("p", "cmd-none", "No command matches that.");
      listEl.append(none);
      return;
    }

    for (const panel of PANELS) {
      const inPanel = hits.filter((c) => c.panel === panel);
      if (!inPanel.length) continue;

      listEl.append(el("h3", "cmd-group", panel));
      const ul = el("ul", "cmd-items");
      for (const cmd of inPanel) {
        const li = document.createElement("li");
        const b = el("button", "cmd-item");
        b.type = "button";
        b.dataset.name = cmd.name;
        b.setAttribute("aria-pressed", String(current === cmd.name));
        b.append(el("code", "cmd-item-name", cmd.name));
        b.append(el("span", "cmd-item-sum", cmd.summary));
        b.addEventListener("click", () => select(cmd.name, true));
        li.append(b);
        ul.append(li);
      }
      listEl.append(ul);
    }
  }

  /* -------------------------------------------------------------- detail */

  function select(name, push) {
    const cmd = COMMANDS.find((c) => c.name === name);
    if (!cmd) return;
    current = name;

    for (const b of listEl.querySelectorAll(".cmd-item")) {
      b.setAttribute("aria-pressed", String(b.dataset.name === name));
    }
    renderDetail(cmd);

    if (push && location.hash !== "#cmd-" + name) {
      history.replaceState(null, "", "#cmd-" + name);
    }
  }

  function renderDetail(cmd) {
    detailEl.innerHTML = "";

    const head = el("div", "cmd-head");
    head.append(el("h3", "cmd-title", "codeorbit " + cmd.name));
    head.append(el("p", "cmd-summary", cmd.summary));
    detailEl.append(head);

    const syn = el("div", "cmd-synopsis");
    syn.append(el("span", "cmd-synopsis-label", "Usage"));
    syn.append(el("code", null, `codeorbit ${cmd.name} ${cmd.usage}`.trim()));
    detailEl.append(syn);

    if (cmd.description) {
      for (const para of cmd.description.split(/\n\s*\n/)) {
        detailEl.append(el("p", "cmd-desc", para.replace(/\s+/g, " ").trim()));
      }
    }

    if (cmd.examples.length) {
      detailEl.append(el("h4", "cmd-sub", "Examples"));
      for (const ex of cmd.examples) {
        detailEl.append(example(ex));
      }
    }

    if (cmd.arguments.length) {
      detailEl.append(el("h4", "cmd-sub", "Arguments"));
      detailEl.append(paramTable(cmd.arguments, false));
    }

    if (cmd.options.length) {
      detailEl.append(el("h4", "cmd-sub", "Options"));
      detailEl.append(paramTable(cmd.options, true));
    }
  }

  function example(ex) {
    const box = el("div", "cmd-example");
    const line = el("div", "cmd-example-line");
    line.append(el("code", null, ex.cmd));

    const copy = el("button", "cmd-copy", "Copy");
    copy.type = "button";
    copy.setAttribute("aria-label", "Copy: " + ex.cmd);
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(ex.cmd);
        copy.textContent = "Copied";
      } catch {
        // Clipboard is blocked in some contexts; say so rather than lying.
        copy.textContent = "Select it";
      }
      setTimeout(() => { copy.textContent = "Copy"; }, 1400);
    });
    line.append(copy);
    box.append(line);
    if (ex.note) box.append(el("p", "cmd-example-note", ex.note));
    return box;
  }

  function paramTable(rows, withDefault) {
    const wrap = el("div", "cmd-table-wrap");
    const table = el("table", "cmd-table");
    const tbody = document.createElement("tbody");

    for (const p of rows) {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      name.className = "cmd-table-name";
      name.append(el("code", null, p.name + (p.metavar ? " " + p.metavar : "")));
      if (p.required) name.append(el("span", "cmd-req", "required"));
      tr.append(name);

      const desc = document.createElement("td");
      desc.textContent = p.help || "";
      if (withDefault && p.default) {
        desc.append(el("span", "cmd-default", "default: " + p.default));
      }
      tr.append(desc);
      tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);
    return wrap;
  }

  /* ------------------------------------------------------------ wiring */

  filterEl.addEventListener("input", () => {
    renderList(filterEl.value.trim().toLowerCase());
  });

  filterEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      const first = listEl.querySelector(".cmd-item");
      if (first) { ev.preventDefault(); first.click(); }
    } else if (ev.key === "Escape") {
      filterEl.value = "";
      renderList("");
    }
  });

  function selectFromHash() {
    const m = /^#cmd-([a-z-]+)$/.exec(location.hash);
    const name = m && COMMANDS.some((c) => c.name === m[1]) ? m[1] : "index";
    select(name, false);
    if (m) root.scrollIntoView({ behavior: "auto", block: "start" });
  }

  window.addEventListener("hashchange", () => {
    if (/^#cmd-/.test(location.hash)) selectFromHash();
  });
})();
