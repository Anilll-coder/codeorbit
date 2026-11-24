/* The coverage section's numbers and chips.
 *
 * Everything here comes from site/languages.json, generated from the language
 * registry by scripts/gen_commands.py. The numbers are the claim this section
 * makes, so writing them by hand twice - once in the tool, once on the page -
 * is how a site ends up advertising 137 languages on a build that reads three.
 *
 * Progressive enhancement, like the command reference: the markup carries a
 * static fallback for every number and a single explanatory chip, so a failed
 * fetch degrades to a page that is still correct rather than an empty shell.
 * A test asserts the fallbacks match what the generator produces.
 */
(function () {
  "use strict";

  const chipHost = document.getElementById("lang-chips");
  if (!chipHost) return;

  const keyButtons = Array.from(document.querySelectorAll("[data-tier-filter]"));

  fetch("languages.json")
    .then((r) => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    })
    .then(render)
    .catch(() => {
      /* Leave the markup's fallback in place. It tells the reader to run
         `codeorbit langs`, which is the authoritative answer anyway. */
    });

  function render(data) {
    setNumbers(data);
    renderChips(data.languages || []);
    wireFilter();
  }

  /* --------------------------------------------------------------- numbers */

  function setNumbers(data) {
    text("[data-lang-count]", data.count);
    text("[data-suffix-count]", data.suffixes);

    const tiers = data.tiers || {};
    text('[data-tier-count="builtin"]', (tiers.builtin || []).length);
    text('[data-tier-count="tabled"]', (tiers.tabled || []).length);
    text('[data-tier-count="inferred"]', tiers.inferred);
  }

  function text(selector, value) {
    if (value === undefined || value === null) return;
    document.querySelectorAll(selector).forEach((el) => {
      el.textContent = String(value);
    });
  }

  /* ----------------------------------------------------------------- chips */

  function renderChips(languages) {
    chipHost.textContent = "";
    /* One document fragment rather than 137 reflows. */
    const frag = document.createDocumentFragment();

    /* Tier order, then alphabetical: the languages with a hand-written or
       tabled extractor are the ones a reader is scanning for, and burying
       them at "p" and "r" in a 137-item alphabetical list hides the point. */
    const rank = { builtin: 0, tabled: 1, inferred: 2 };
    const sorted = languages.slice().sort((a, b) => {
      const d = (rank[a.tier] ?? 3) - (rank[b.tier] ?? 3);
      return d || a.id.localeCompare(b.id);
    });

    for (const lang of sorted) {
      const chip = document.createElement("span");
      chip.className = "lang-chip";
      chip.dataset.tier = lang.tier;
      chip.textContent = lang.id;
      if (lang.suffixes && lang.suffixes.length) {
        chip.title = lang.suffixes.join(" ");
      }
      frag.appendChild(chip);
    }

    chipHost.appendChild(frag);
  }

  /* ---------------------------------------------------------------- filter */

  function wireFilter() {
    keyButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const want = btn.dataset.tierFilter;
        keyButtons.forEach((b) => b.classList.toggle("is-on", b === btn));

        /* Dimming rather than hiding, so the proportions stay visible: the
           proportions between the tiers stay visible. */
        chipHost.querySelectorAll(".lang-chip").forEach((chip) => {
          chip.classList.toggle(
            "is-dim",
            want !== "all" && chip.dataset.tier !== want
          );
        });
      });
    });
  }
})();
