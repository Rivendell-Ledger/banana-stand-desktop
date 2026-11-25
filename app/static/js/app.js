/* Banana Stand — small progressive enhancements. No framework, no build step. */
(() => {
  "use strict";

  // The till line: width comes from a data attribute so CSP can stay strict.
  document.querySelectorAll(".till[data-pct]").forEach((el) => {
    const pct = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
    const fill = el.querySelector(".till-fill");
    if (fill) {
      requestAnimationFrame(() => {
        fill.style.width = pct + "%";
      });
    }
  });

  // Honest placeholders: explain what is not built yet, inline, no alert().
  document.querySelectorAll("[data-soon]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const note = document.getElementById(btn.dataset.soon);
      if (!note) return;
      note.textContent = btn.dataset.note || "Arrives in the next build.";
      note.hidden = false;
    });
  });

  // Prevent accidental double submits. The page reloads on any server response.
  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      // The clicked button, not merely the first one: forms can have several.
      const btn = event.submitter || form.querySelector('button[type="submit"]');
      if (!btn || btn.disabled) return;
      btn.dataset.label = btn.textContent;
      btn.disabled = true;
      btn.textContent = btn.dataset.busy || "Working…";
    });
  });
})();


/* ---------------------------------------------------------------- transactions */
(() => {
  "use strict";
  const meta = document.querySelector('meta[name="csrf"]');
  const CSRF = meta ? meta.content : "";

  async function post(url, data) {
    const res = await fetch(url, {
      method: "POST",
      body: new URLSearchParams(data),
      headers: { "X-Requested-With": "fetch" },
    });
    return res.json().catch(() => ({ ok: false, error: "Server error" }));
  }

  // ---- click-to-edit cells -------------------------------------------------
  let categories = null;
  async function loadCategories() {
    if (categories) return categories;
    try {
      const res = await fetch("/api/categories", { headers: { "X-Requested-With": "fetch" } });
      const data = await res.json();
      categories = data.categories || [];
    } catch (err) {
      categories = [];
    }
    return categories;
  }

  async function beginEdit(btn) {
    if (btn.dataset.busy) return;
    const field = btn.dataset.edit;
    const row = btn.closest(".txn");
    const id = row && row.dataset.id;
    if (!id) return;
    btn.dataset.busy = "1";

    let input;
    if (field === "category_id") {
      const list = await loadCategories();
      input = document.createElement("select");
      input.className = "cell-input";
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "Uncategorized";
      input.appendChild(none);
      list.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = String(c.id);
        opt.textContent = c.name;
        input.appendChild(opt);
      });
      input.value = btn.dataset.value || "";
    } else {
      input = document.createElement("input");
      input.className = "cell-input";
      input.value = btn.dataset.value || "";
      input.inputMode = field === "amount_cents" ? "decimal" : "text";
      input.autocomplete = "off";
    }

    btn.hidden = true;
    btn.after(input);
    input.focus();
    if (input.select) input.select();

    let done = false;
    const close = (commit) => {
      if (done) return;
      done = true;
      const value = input.value;
      if (!commit || value === (btn.dataset.value || "")) {
        input.remove();
        btn.hidden = false;
        delete btn.dataset.busy;
        return;
      }
      save(btn, id, field, value, input);
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); close(true); }
      else if (e.key === "Escape") { e.preventDefault(); close(false); }
    });
    input.addEventListener("blur", () => close(true));
    if (field === "category_id") input.addEventListener("change", () => close(true));
  }

  async function save(btn, id, field, value, input) {
    input.classList.add("cell-saving");
    const data = await post(`/transactions/${id}/field`, { csrf: CSRF, field, value });
    if (data.ok) {
      btn.dataset.value = field === "category_id" ? (input.value || "") : value;
      btn.textContent = data.display;
      if (field === "amount_cents") {
        const cell = btn.closest(".txn-amount");
        if (cell) {
          cell.classList.toggle("pos", data.kind === "pos");
          cell.classList.toggle("neg", data.kind === "neg");
        }
      }
    } else {
      input.classList.add("cell-error");
      btn.title = data.error || "Could not save.";
      await new Promise((r) => setTimeout(r, 900));
    }
    input.remove();
    btn.hidden = false;
    delete btn.dataset.busy;
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-edit]");
    if (btn && btn.tagName === "BUTTON") beginEdit(btn);
  });

  // ---- split totals --------------------------------------------------------
  document.querySelectorAll(".split-form[data-total]").forEach((form) => {
    const total = Number(form.dataset.total) || 0;
    const check = form.querySelector("#split-check");
    const inputs = Array.from(form.querySelectorAll('input[name="split_amount"]'));
    if (!check) return;
    const asMoney = (cents) =>
      (cents < 0 ? "−" : "") + "$" + (Math.abs(cents) / 100).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    const update = () => {
      let sum = 0;
      let any = false;
      inputs.forEach((input) => {
        const raw = (input.value || "").replace(/[$\s,()]/g, "").replace("−", "-");
        if (!raw) return;
        const n = Number(raw);
        if (!Number.isNaN(n)) { sum += Math.round(n * 100); any = true; }
      });
      if (!any) { check.textContent = ""; check.className = "split-check"; return; }
      const diff = sum - total;
      check.textContent = diff === 0
        ? `Lines total ${asMoney(sum)} — that adds up.`
        : `Lines total ${asMoney(sum)} · ${asMoney(diff)} off`;
      check.className = "split-check " + (diff === 0 ? "ok" : "bad");
    };
    inputs.forEach((input) => input.addEventListener("input", update));
    update();
  });
})();


/* ---------------------------------------------------------------- rules editor */
(() => {
  "use strict";
  const action = document.querySelector("[data-action-toggle]");
  if (!action) return;
  const fields = Array.from(document.querySelectorAll("[data-action-for]"));
  const sync = () => {
    fields.forEach((field) => {
      const on = field.dataset.actionFor === action.value;
      field.hidden = !on;
      field.querySelectorAll("input, select").forEach((el) => { el.disabled = !on; });
    });
  };
  action.addEventListener("change", sync);
  sync();
})();

/* ---------------------------------------------------------------- category bars */
(() => {
  "use strict";
  const bars = Array.from(document.querySelectorAll(".cat-bar-fill[data-uses]"));
  if (!bars.length) return;
  const max = Math.max(1, ...bars.map((b) => Number(b.dataset.uses) || 0));
  bars.forEach((bar) => {
    const pct = Math.round(((Number(bar.dataset.uses) || 0) / max) * 100);
    requestAnimationFrame(() => { bar.style.width = pct + "%"; });
  });
})();


/* ---------------------------------------------------------------- budget bars */
(() => {
  "use strict";
  document.querySelectorAll(".mini-fill[data-pct]").forEach((el) => {
    const pct = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
    requestAnimationFrame(() => { el.style.width = pct + "%"; });
  });
})();


/* ---------------------------------------------------------------- allocation bar */
(() => {
  "use strict";
  document.querySelectorAll(".alloc-seg[data-pct]").forEach((el) => {
    const pct = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
    el.style.width = pct + "%";
  });
})();


/* ---------------------------------------------------------------- report + goal bars */
(() => {
  "use strict";
  document.querySelectorAll(".report-fill[data-pct], .goal-fill[data-pct]").forEach((el) => {
    const pct = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
    el.style.width = pct + "%";
  });
  document.querySelectorAll(".trend-bar[data-pct]").forEach((el) => {
    const pct = Math.max(2, Math.min(100, Number(el.dataset.pct) || 0));
    el.style.height = pct + "%";
  });
  document.querySelectorAll(".trend-gridline[data-pct]").forEach((el) => {
    const pct = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
    el.style.bottom = pct + "%";
  });
})();


/* ---------------------------------------------------------------- CSP-safe behaviours */
(() => {
  "use strict";
  // <select data-autosubmit> replaces onchange="this.form.submit()", which strict CSP blocks.
  document.addEventListener("change", (event) => {
    const el = event.target.closest("[data-autosubmit]");
    if (el && el.form) el.form.requestSubmit();
  });

  // <button data-confirm="..."> replaces onclick="return confirm(...)", which strict CSP blocks.
  document.addEventListener("click", (event) => {
    const el = event.target.closest("[data-confirm]");
    if (!el) return;
    if (!window.confirm(el.dataset.confirm || "Are you sure?")) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
})();


/* ---------------------------------------------------------------- tab swipes */
/* Turn the page with a sideways drag. Modelled on the Ledger's swipe: a plain
   threshold on touchstart/touchend instead of dragging a transformed copy of a long
   page (that repaint is what felt sticky), tabs form a RING so either direction always
   works, and the click under the finger is swallowed afterwards. */
(() => {
  "use strict";

  const bar = document.querySelector(".tabbar");
  const main = document.querySelector("main.page");
  if (!bar || !main) return;

  const links = Array.from(bar.querySelectorAll("a[href]"));
  if (links.length < 2) return;

  const here = links.findIndex((a) => a.classList.contains("active"));
  if (here < 0) return;

  // The ring: the last tab steps on to the first and the first steps back to the last,
  // so a swipe is never refused at either end.
  const step = (dir) => {
    const next = links[((here + dir) % links.length + links.length) % links.length];
    if (next) window.location.href = next.href;
  };

  // Warm the two neighbours so the turn lands instantly.
  [here - 1, here + 1].forEach((i) => {
    const link = links[((i % links.length) + links.length) % links.length];
    if (!link) return;
    const tag = document.createElement("link");
    tag.rel = "prefetch";
    tag.href = link.href;
    document.head.appendChild(tag);
  });

  /* Sideways drags that already mean something are left alone: the tab strip scrolls,
     a select or a text box owns the finger, and a wide CSV can be panned. */
  const HARD = ".tabbar, .topbar, .page-tabs, select, textarea, .chart-scroll, .curve-scroll,"
    + " [data-chart], [data-scrub], [data-no-swipe]";
  const SCROLL = ".csv-scroll";
  const MIN = 55;      // px of travel before it counts as a swipe, not a sloppy tap
  const SLOPE = 1.6;   // ...and it must be this much more sideways than up-and-down
  const MAX_MS = 900;  // a slow drag is the reader aiming, not turning the page
  let start = null;
  let ate = 0;

  document.addEventListener("touchstart", (event) => {
    start = null;
    if (event.touches.length !== 1) return;
    const t = event.touches[0];
    const from = event.target;
    const hard = from && from.closest ? from.closest(HARD) : null;
    const sc = (hard || !from || !from.closest) ? null : from.closest(SCROLL);
    const room = sc ? sc.scrollWidth - sc.clientWidth : 0;
    start = {
      x: t.clientX,
      y: t.clientY,
      at: Date.now(),
      blocked: !!hard,
      scroll: sc,
      // Where the strip sat when the finger landed, not where the drag left it.
      atStart: sc ? sc.scrollLeft <= 1 : false,
      atEnd: sc ? sc.scrollLeft >= room - 1 : false,
    };
  }, { passive: true });

  document.addEventListener("touchend", (event) => {
    const s = start;
    start = null;
    const t = event.changedTouches && event.changedTouches[0];
    if (!s || s.blocked || !t) return;

    const dx = t.clientX - s.x;
    const dy = t.clientY - s.y;
    if (Math.abs(dx) < MIN || Math.abs(dx) < Math.abs(dy) * SLOPE) return;
    if (Date.now() - s.at > MAX_MS) return;

    // The drag was a swipe, not a press: nothing under the finger may act on it.
    ate = Date.now();
    if (s.scroll) {
      if (dx < 0 ? !s.atEnd : !s.atStart) return;
    }
    step(dx < 0 ? 1 : -1);
  }, { passive: true });

  document.addEventListener("click", (event) => {
    if (Date.now() - ate < 450 && (main.contains(event.target) || bar.contains(event.target))) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
})();

// ---------------------------------------------------------------- account form
// A Bitcoin account is entered in BTC, not dollars: the Stand prices it live.
(() => {
  const form = document.querySelector("[data-account-form]");
  if (!form) return;
  const select = form.querySelector("[data-account-type]");
  const label = form.querySelector("[data-balance-label]");
  const input = form.querySelector("[data-balance-input]");
  const hint = form.querySelector("[data-balance-hint]");
  if (!select || !label || !input) return;

  const budgetBox = form.querySelector("input[name=include_budget]");
  let budgetAutoUnchecked = false;

  const apply = () => {
    const option = select.selectedOptions && select.selectedOptions[0];
    const isBtc = !!(option && option.hasAttribute("data-btc"));
    // A house is net worth, not spending money — don't let it inflate safe to spend.
    if (budgetBox) {
      if (option && option.value === "real_estate" && budgetBox.checked) {
        budgetBox.checked = false;
        budgetAutoUnchecked = true;
      } else if (budgetAutoUnchecked && option && option.value !== "real_estate") {
        budgetBox.checked = true;
        budgetAutoUnchecked = false;
      }
    }
    label.textContent = isBtc ? "Bitcoin amount (BTC)" : "Current balance";
    input.placeholder = isBtc ? "0.00000000" : "0.00";
    input.setAttribute("inputmode", isBtc ? "decimal" : "decimal");
    if (hint) {
      hint.textContent = isBtc
        ? "Enter units of BTC. The dollar value is calculated from the live price."
        : "For cards and loans, enter the amount owed.";
    }
  };

  select.addEventListener("change", apply);
  apply();
})();

/* ---------------------------------------------------------------- person blocks
   Keep the collapsed summary honest while the fields inside are being edited. */
(function () {
  const words = { annual: "a year", monthly: "a month", semimonthly: "twice a month",
                  biweekly: "every two weeks", weekly: "a week" };
  document.querySelectorAll("[data-person]").forEach((block) => {
    const n = block.dataset.person;
    const title = block.querySelector("[data-person-title]");
    const meta = block.querySelector("[data-person-meta]");
    if (!title || !meta) return;
    const name = document.getElementById(`p${n}-name`);
    const retire = document.getElementById(`p${n}-retire`);
    const gross = document.getElementById(`gross${n}`);
    const freq = document.getElementById(`freq${n}`);
    const paint = () => {
      const who = (name?.value || "").trim();
      title.textContent = `Person ${n}` + (who ? ` — ${who}` : "");
      const bits = [`retires at ${(retire?.value || "65").trim() || "65"}`];
      const amount = parseFloat((gross?.value || "").replace(/[^\d.]/g, ""));
      if (amount > 0) {
        const per = words[freq?.value || "annual"] || "a year";
        bits.push(`$${amount.toLocaleString(undefined, { maximumFractionDigits: 0 })} ${per}`);
      }
      meta.textContent = bits.join(" · ");
    };
    [name, retire, gross, freq].forEach((el) => el && el.addEventListener("input", paint));
    freq && freq.addEventListener("change", paint);
  });

  // Keep the section summaries current too.
  const dollars = (el) => {
    const n = parseFloat((el?.value || "").replace(/[^\d.]/g, ""));
    return Number.isFinite(n) ? n : 0;
  };
  const spend = document.querySelector('[data-sec-meta="spending"]');
  const spendField = document.getElementById("spending");
  if (spend && spendField) {
    spendField.addEventListener("input", () => {
      const n = dollars(spendField);
      spend.textContent = `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })} a year today`;
    });
  }
  const assume = document.querySelector('[data-sec-meta="assumptions"]');
  if (assume) {
    const paint = () => {
      const bits = [`${dollars(document.getElementById("return"))}% return`];
      const crypto = document.getElementById("crypto");
      if (crypto && crypto.value.trim()) bits.push(`crypto ${dollars(crypto)}%`);
      bits.push(`${dollars(document.getElementById("inflation"))}% inflation`);
      assume.textContent = bits.join(" · ");
    };
    ["return", "crypto", "inflation"].forEach((id) => {
      document.getElementById(id)?.addEventListener("input", paint);
    });
  }
})();

/* ---------------------------------------------------------------- page tabs
   In-page tabs (Retirement): one panel at a time, remembered in the URL hash. */
(function () {
  const bar = document.querySelector("[data-tabs]");
  if (!bar) return;
  const tabs = Array.from(bar.querySelectorAll("[data-tab]"));
  const panels = tabs.map((t) => document.getElementById(t.dataset.tab)).filter(Boolean);
  if (!tabs.length || !panels.length) return;

  const show = (name, remember) => {
    tabs.forEach((t) => {
      const on = t.dataset.tab === name;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
    });
    panels.forEach((p) => { p.hidden = p.id !== name; });
    if (remember) history.replaceState(null, "", "#" + name);
  };

  const known = tabs.map((t) => t.dataset.tab);
  const fromHash = (location.hash || "").replace("#", "");
  if (known.includes(fromHash)) show(fromHash, false);

  bar.addEventListener("click", (ev) => {
    const tab = ev.target.closest("[data-tab]");
    if (tab) show(tab.dataset.tab, true);
  });
  // Back/forward and in-page #links change the hash without reloading.
  window.addEventListener("hashchange", () => {
    const name = (location.hash || "").replace("#", "");
    if (known.includes(name)) show(name, false);
  });

  bar.addEventListener("keydown", (ev) => {
    const i = known.indexOf(document.activeElement?.dataset?.tab || "");
    if (i < 0) return;
    if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
    ev.preventDefault();
    const next = (i + (ev.key === "ArrowRight" ? 1 : -1) + known.length) % known.length;
    bar.querySelector(`[data-tab="${known[next]}"]`).focus();
    show(known[next], true);
  });
})();

/* ---------------------------------------------------------------- timed spending rows
   Retirement's adjustments: add a row, or blank one out to drop it on save. */
(function () {
  const body = document.getElementById("adj-rows");
  if (!body) return;
  const add = document.querySelector("[data-adj-add]");
  const tpl = document.getElementById("adj-template");
  let next = Number(body.dataset.next || 1);
  if (add && tpl) {
    add.addEventListener("click", () => {
      const frag = tpl.content.cloneNode(true);
      frag.querySelectorAll("[name]").forEach((el) => {
        el.name = el.name.replace("__IDX__", String(next));
      });
      next += 1;
      body.appendChild(frag);
    });
  }
  body.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".adj-del");
    if (!btn) return;
    const row = btn.closest("tr");
    row.querySelectorAll("input").forEach((el) => {
      if (el.type === "checkbox") el.checked = false; else el.value = "";
    });
    row.hidden = true;
  });
})();

/* ---------------------------------------------------------------- privacy eye
   Hides the money on the Home cards for when someone is looking over a shoulder. */
(function () {
  const root = document.documentElement;
  const buttons = Array.from(document.querySelectorAll("[data-privacy]"));
  if (!buttons.length) return;
  // A native tooltip would whisper the amount the blur is hiding.
  const stashed = [];
  const veilTitles = (hidden) => {
    if (hidden) {
      document.querySelectorAll("[title]").forEach((el) => {
        const text = el.getAttribute("title");
        if (text && text.indexOf("$") >= 0) { stashed.push([el, text]); el.removeAttribute("title"); }
      });
    } else {
      stashed.splice(0).forEach(([el, text]) => el.setAttribute("title", text));
    }
  };
  const paint = (hidden) => {
    root.classList.toggle("is-private", hidden);
    veilTitles(hidden);
    buttons.forEach((b) => {
      b.setAttribute("aria-pressed", hidden ? "true" : "false");
      b.title = hidden ? "Show amounts" : "Hide amounts";
      b.setAttribute("aria-label", hidden ? "Privacy mode on" : "Privacy mode");
    });
  };
  paint(root.classList.contains("is-private"));
  buttons.forEach((b) => b.addEventListener("click", () => {
    const hidden = !root.classList.contains("is-private");
    paint(hidden);
    document.cookie = "bs_privacy=" + (hidden ? "1" : "0") +
      "; path=/; max-age=31536000; samesite=lax";
  }));
})();

/* ---------------------------------------------------------------- chart scrubber
   Drag across a chart to read the snapshot under your finger. */
(function () {
  const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  const day = (iso) =>
    new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });

  document.querySelectorAll("[data-chart]").forEach((card) => {
    let data;
    try { data = JSON.parse(card.dataset.series || "{}"); } catch (err) { return; }
    if (!data || !Array.isArray(data.labels) || data.labels.length < 2) return;
    const byKey = new Map((data.series || []).map((s) => [s.key, s]));
    const last = data.labels.length - 1;
    const plot = card.querySelector(".chart-plot");
    const cursor = card.querySelector("[data-cursor]");
    const readout = card.querySelector("[data-readout]");
    const keys = (card.dataset.keys || "").split(",").filter(Boolean);
    const showPct = card.dataset.unit === "pct";
    if (!plot || !cursor || !readout) return;

    const show = (clientX) => {
      const rect = plot.getBoundingClientRect();
      if (!rect.width) return;
      const t = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const i = Math.round(t * last);
      cursor.hidden = false;
      cursor.style.left = (i / last) * 100 + "%";
      const parts = keys
        .map((k) => {
          const s = byKey.get(k);
          if (!s) return "";
          if (showPct && s.values[0]) {
            const pct = ((s.values[i] - s.values[0]) / s.values[0]) * 100;
            return s.name + " " + (pct >= 0 ? "+" : "") + pct.toFixed(1) + "%";
          }
          return s.name + " " + usd.format(s.values[i] / 100);
        })
        .filter(Boolean);
      readout.textContent = day(data.labels[i]) + " · " + parts.join(" · ");
    };

    let dragging = false;
    plot.addEventListener("pointerdown", (e) => {
      dragging = true;
      try { plot.setPointerCapture(e.pointerId); } catch (err) { /* older browsers */ }
      show(e.clientX);
      e.preventDefault();
    });
    plot.addEventListener("pointermove", (e) => { if (dragging) show(e.clientX); });
    plot.addEventListener("pointerup", () => { dragging = false; });
    plot.addEventListener("pointercancel", () => { dragging = false; });
  });
})();

/* ---------------------------------------------------------------- holdings auto-save
   The Save bar is gone; a manual holding writes itself when a field is committed. */
(function () {
  const form = document.querySelector("[data-holdings-form]");
  if (!form) return;
  form.addEventListener("change", (event) => {
    const field = event.target;
    if (!field.matches("input[name^='qty_'], input[name^='price_']")) return;
    if (!field.value.trim()) return;
    form.requestSubmit();
  });
})();

/* ---------------------------------------------------------------- panel pills
   Filters and New Txn live in the header as small pills and open their panels. */
(function () {
  document.querySelectorAll("[data-toggle-panel]").forEach((btn) => {
    const panel = document.getElementById(btn.dataset.togglePanel);
    if (!panel) return;
    const isDetails = panel.tagName === "DETAILS";
    const setState = (open) => {
      if (isDetails) panel.open = open;
      else panel.hidden = !open;
      btn.classList.toggle("is-active", open);
      btn.setAttribute("aria-expanded", String(open));
    };
    setState(isDetails ? panel.open : !panel.hidden);
    btn.addEventListener("click", () => {
      const open = isDetails ? panel.open : !panel.hidden;
      setState(!open);
      if (!open) {
        const first = panel.querySelector("input:not([type=hidden]), select, textarea");
        if (first) first.focus();
      }
    });
  });
})();

/* ---------------------------------------------------------------- spend bars */
(function () {
  document.querySelectorAll(".spend-bar[data-pct]").forEach((el) => {
    const pct = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
    el.style.height = pct ? Math.max(2, pct) + "%" : "0";
  });
})();


/* ---------------------------------------------------------------- tap-to-read charts */
(function () {
  "use strict";

  function attach(plot) {
    if (!plot || plot.dataset.tapReady) return;
    plot.dataset.tapReady = "1";
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    plot.appendChild(tip);

    function show(col) {
      const label = col.dataset.label || "";
      const value = col.dataset.value || "";
      if (!label && !value) return;
      tip.textContent = value ? label + " · " + value : label;
      const pr = plot.getBoundingClientRect();
      const bar = col.querySelector(".spend-bar, .trend-bar, .flow-bar") || col;
      const br = bar.getBoundingClientRect();
      const half = tip.offsetWidth / 2;
      let x = br.left - pr.left + br.width / 2;
      x = pr.width <= tip.offsetWidth ? pr.width / 2 : Math.max(half, Math.min(pr.width - half, x));
      tip.style.left = x + "px";
      tip.style.top = Math.max(0, br.top - pr.top) + "px";
      plot.classList.add("tip-on");
    }

    function hide() {
      plot.classList.remove("tip-on");
    }

    plot.addEventListener("click", (ev) => {
      const col = ev.target.closest(".spend-col, .trend-col, .flow-col");
      if (col) show(col);
      else hide();
    });
    plot.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const col = ev.target.closest(".spend-col, .trend-col, .flow-col");
      if (!col) return;
      ev.preventDefault();
      show(col);
    });
    document.addEventListener("click", (ev) => {
      if (!plot.contains(ev.target)) hide();
    });
  }

  document.querySelectorAll(".spend-plot, .trend, .flow-plot").forEach(attach);
})();


/* ---------------------------------------------------------------- cash-flow bars */
(function () {
  document.querySelectorAll(".flow-bar[data-pct]").forEach((el) => {
    const pct = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
    el.style.height = pct ? Math.max(1.5, pct) + "%" : "0";
  });
})();


/* ---------------------------------------------------------------- bills calendar */
(function () {
  "use strict";
  const cal = document.querySelector("[data-cal]");
  const detail = document.querySelector("[data-cal-detail]");
  if (!cal || !detail) return;

  const csrf = cal.dataset.csrf || "";
  const days = Array.from(cal.querySelectorAll("[data-cal-day]"));

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function postForm(action, label) {
    const form = document.createElement("form");
    form.method = "post";
    form.action = action;
    const token = document.createElement("input");
    token.type = "hidden";
    token.name = "csrf";
    token.value = csrf;
    form.appendChild(token);
    const btn = node("button", "btn btn-ghost btn-sm", label);
    btn.type = "submit";
    form.appendChild(btn);
    return form;
  }

  function select(btn) {
    days.forEach((day) => {
      const on = day === btn;
      day.classList.toggle("is-selected", on);
      day.setAttribute("aria-pressed", on ? "true" : "false");
    });

    let items = [];
    try { items = JSON.parse(btn.dataset.calItems || "[]"); } catch (err) { items = []; }

    const date = new Date(btn.dataset.calDay + "T12:00:00");
    detail.replaceChildren();
    detail.appendChild(node("p", "cal-detail-date", date.toLocaleDateString(undefined, {
      weekday: "long", month: "long", day: "numeric",
    })));

    if (!items.length) {
      detail.appendChild(node("p", "sub", "Nothing scheduled this day."));
      return;
    }

    const list = node("ul", "cal-items");
    items.forEach((item) => {
      const isIncome = item.kind === "income";
      const li = node("li", "cal-item");
      li.appendChild(node("span", "cal-dot " + (isIncome ? "income" : "bill")));

      const main = node("div", "cal-item-main");
      main.appendChild(node("p", "cal-item-name", item.name));
      const meta = [item.cadence];
      if (item.varies) meta.push("varies");
      if (item.status === "suggested") meta.push("suggested");
      main.appendChild(node("p", "cal-item-meta", meta.join(" · ")));
      li.appendChild(main);

      const amount = node("span", "cal-item-amt" + (isIncome ? " pos" : ""));
      amount.textContent = (isIncome ? "+" : "") + (item.varies ? "≈ " : "") + item.amount;
      li.appendChild(amount);

      if (item.status === "suggested" && item.first) {
        const actions = node("div", "cal-item-actions");
        const confirm = postForm("/bills/" + item.id + "/confirm", "Confirm");
        confirm.querySelector("button").className = "btn btn-primary btn-sm";
        actions.appendChild(confirm);
        actions.appendChild(postForm("/bills/" + item.id + "/ignore", "Ignore"));
        li.appendChild(actions);
      } else if (item.status === "confirmed") {
        const actions = node("div", "cal-item-actions");
        const edit = node("a", "btn btn-ghost btn-sm", "Edit");
        edit.href = "/bills/" + item.id;
        actions.appendChild(edit);
        li.appendChild(actions);
      }
      list.appendChild(li);
    });
    detail.appendChild(list);
  }

  days.forEach((day) => day.addEventListener("click", () => select(day)));
  const today = days.find((day) => day.classList.contains("is-today"));
  if (today) select(today);
})();


/* ---------------------------------------------------------------- pending review */
(function () {
  "use strict";
  const forms = document.querySelectorAll("form[data-inline-review]");
  if (!forms.length) return;

  forms.forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("button[type='submit']");
      if (button) button.disabled = true;
      try {
        const res = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { "X-Requested-With": "fetch" },
        });
        if (!res.ok) throw new Error("review failed");
      } catch (err) {
        form.submit();
        return;
      }

      const row = form.closest(".pending-row");
      if (row) row.remove();

      const list = document.querySelector(".pending-list");
      const card = document.getElementById("pending-review");
      const left = list ? list.querySelectorAll(".pending-row").length : 0;
      const chip = card ? (card.querySelector("[data-pending-chip]") || card.querySelector(".chip")) : null;
      if (chip) chip.textContent = left + " waiting";
      // The bulk pill only makes sense while there is more than one row left.
      const bulk = card ? card.querySelector("form[data-inline-review-all]") : null;
      if (bulk && left <= 1) bulk.remove();
      if (card && left === 0) card.remove();
    });
  });

  const bulk = document.querySelector("form[data-inline-review-all]");
  if (bulk) {
    bulk.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = bulk.querySelector("button");
      if (button) { button.disabled = true; button.textContent = "Clearing…"; }
      try {
        const res = await fetch(bulk.action, {
          method: "POST",
          body: new FormData(bulk),
          headers: { "X-Requested-With": "fetch" },
        });
        if (!res.ok) throw new Error("bulk review failed");
      } catch (err) {
        bulk.submit();   // let the server take over rather than dropping the tap
        return;
      }
      const card = document.getElementById("pending-review");
      if (card) card.remove();
    });
  }
})();


/* ---------------------------------------------------------------- budget edits
   Enter commits the budget form in place: no Save bar, no reload, no lost focus. */
(function () {
  "use strict";
  const form = document.querySelector("form[data-budget-form]");
  if (!form) return;

  const cents = (value) => Math.round(parseFloat(String(value).replace(/[^0-9.\-]/g, "")) * 100) || 0;
  const money = (value) => {
    const sign = value < 0 ? "\u2212" : "";
    return sign + "$" + (Math.abs(value) / 100).toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  };
  const budgeted = document.querySelector("[data-budget-budgeted]");
  const spent = document.querySelector("[data-budget-spent]");
  const left = document.querySelector("[data-budget-left]");
  const display = document.querySelector("[data-income-display]");
  const incomeInput = document.querySelector("[data-income-input]");
  let editingIncome = false;

  async function save(field) {
    let data;
    try {
      const res = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
      });
      if (!res.ok) throw new Error("save failed");
      data = await res.json();
    } catch (err) {
      form.submit();
      return false;
    }
    if (budgeted) budgeted.textContent = money(data.budgeted_cents);
    if (spent) spent.textContent = money(data.spent_cents);
    if (left) left.textContent = data.left_to_budget_cents == null ? "\u2014" : money(data.left_to_budget_cents);
    if (display) display.textContent = money(data.expected_income_cents);
    if (field) {
      field.classList.add("is-saved");
      window.setTimeout(() => field.classList.remove("is-saved"), 1200);
    }
    return true;
  }

  function openIncome() {
    if (!display || !incomeInput) return;
    editingIncome = true;
    display.hidden = true;
    incomeInput.hidden = false;
    incomeInput.focus();
    incomeInput.select();
  }

  function closeIncome() {
    if (!display || !incomeInput) return;
    editingIncome = false;
    incomeInput.hidden = true;
    display.hidden = false;
  }

  if (display && incomeInput) {
    display.addEventListener("click", openIncome);
    incomeInput.addEventListener("blur", async () => {
      if (!editingIncome) return;
      if (await save(incomeInput)) closeIncome();
    });
    incomeInput.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      incomeInput.value = incomeInput.defaultValue;
      closeIncome();
    });
  }

  form.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter" || event.target.tagName !== "INPUT") return;
    event.preventDefault();
    const target = event.target;
    if (target === incomeInput && cents(target.value) === cents(target.defaultValue)) {
      closeIncome();
      return;
    }
    if (await save(target)) {
      if (target === incomeInput) {
        target.defaultValue = target.value;
        closeIncome();
      } else {
        target.defaultValue = target.value;
      }
    }
  });
})();


/* ---------------------------------------------------------------- Other wedge
   The grey "Other" slice opens the list row that spells out what it holds. */
(function () {
  "use strict";
  const wedges = document.querySelectorAll("[data-other-wedge]");
  if (!wedges.length) return;
  wedges.forEach((wedge) => {
    wedge.addEventListener("click", () => {
      const details = document.getElementById("other-details");
      if (!details) return;
      details.open = true;
      details.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  });
})();

/* Confirm a suggested bill/income the moment its box is ticked. */
(() => {
  "use strict";
  document.querySelectorAll("form[data-auto-submit] input[type='checkbox']").forEach((box) => {
    box.addEventListener("change", () => box.form.submit());
  });
})();

/* Keep the current tab visible when the bar has to scroll. */
(() => {
  "use strict";
  const bar = document.querySelector(".tabbar");
  const active = bar && bar.querySelector("a.active");
  if (bar && active && bar.scrollWidth > bar.clientWidth) {
    active.scrollIntoView({ inline: "center", block: "nearest" });
  }
})();


/* ---------------------------------------------------------------- chart panning */
/* Click and drag a wide chart like the Ledger's charts: the finger owns the plot, so the
   window follows it 1:1 and the tab swipe never sees the gesture. */
(() => {
  "use strict";
  document.querySelectorAll(".chart-scroll, .curve-scroll").forEach((box) => {
    if (box.querySelector("[data-scrub]")) return;  // this one is scrubbed, not panned
    let drag = null;
    box.addEventListener("pointerdown", (event) => {
      if (event.button != null && event.button !== 0) return;
      if (box.scrollWidth <= box.clientWidth + 1) return;
      drag = { x: event.clientX, left: box.scrollLeft, id: event.pointerId };
      box.classList.add("is-dragging");
      try { box.setPointerCapture(event.pointerId); } catch (err) { /* moves still bubble */ }
      event.preventDefault();
    });
    box.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      box.scrollLeft = drag.left - (event.clientX - drag.x);
    });
    const end = (event) => {
      if (!drag || (event && event.pointerId !== drag.id)) return;
      drag = null;
      box.classList.remove("is-dragging");
    };
    box.addEventListener("pointerup", end);
    box.addEventListener("pointercancel", end);
    box.addEventListener("lostpointercapture", end);
  });
})();


/* ---------------------------------------------------------------- chart scrubbing */
/* Press and drag across the investments chart the way the Ledger's inflation chart
   behaves: a rule and a dot per series follow the finger, and the readout names the
   day. Nothing is recomputed — the dots sit on the points that were drawn. */
(() => {
  "use strict";
  document.querySelectorAll("svg[data-scrub]").forEach((svg) => {
    const dates = JSON.parse(svg.dataset.dates || "[]");
    const labels = JSON.parse(svg.dataset.labels || "[]");
    const series = JSON.parse(svg.dataset.series || "[]");
    // A projection has years, not sessions, and money, not an index.
    const asMoney = svg.dataset.format === "money";
    const show = (v) => asMoney
      ? (Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(2)}M`
        : `$${Math.round(v).toLocaleString("en-US")}`)
      : v.toFixed(1);
    const geom = JSON.parse(svg.dataset.geom || "{}");
    const card = svg.closest(".card");
    const dayOut = card && card.querySelector("[data-chart-day]");
    const valueOut = card
      ? Array.from(card.querySelectorAll("[data-chart-value]"))
      : [];
    const pick = svg.querySelector(".perf-pick");
    const rule = pick && pick.querySelector(".perf-rule");
    const dots = pick ? Array.from(pick.querySelectorAll(".perf-pdot")) : [];
    const n = dates.length || labels.length;
    if (!pick || !rule || n < 2 || !series.length) return;

    const inner = geom.w - 2 * geom.pad;
    const xFor = (i) => geom.pad + i * inner / (n - 1);
    const yFor = (v) => geom.h - geom.pad - (v - geom.lo) * (geom.h - 2 * geom.pad) / (geom.hi - geom.lo);

    const paint = (clientX) => {
      const box = svg.getBoundingClientRect();
      if (!box.width) return;
      const vx = (clientX - box.left) * (geom.w / box.width);
      const i = Math.max(0, Math.min(n - 1, Math.round((vx - geom.pad) / (inner / (n - 1)))));
      const x = xFor(i);
      pick.style.opacity = 1;
      rule.setAttribute("x1", x.toFixed(1));
      rule.setAttribute("x2", x.toFixed(1));
      series.forEach((s, k) => {
        const dot = dots[k];
        if (!dot) return;
        // A line that stops early (this month, a failed path) has no point to sit on.
        if (s.values[i] == null) { dot.style.display = "none"; return; }
        dot.style.display = "";
        dot.setAttribute("cx", x.toFixed(1));
        dot.setAttribute("cy", yFor(s.values[i]).toFixed(1));
      });
      // The legend carries the reading: the day on the left, each line's value beside its key.
      if (dayOut) {
        if (labels[i]) {
          dayOut.textContent = labels[i];
        } else {
          const d = new Date(dates[i] + "T12:00:00");
          dayOut.textContent = isNaN(d) ? dates[i]
            : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
        }
      }
      valueOut.forEach((el) => {
        const key = el.dataset.chartValue;
        const ser = series.find((s) => s.key === key);
        if (ser) el.textContent = ser.values[i] == null ? "—" : show(ser.values[i]);
      });
    };

    let dragging = false;
    svg.addEventListener("pointerdown", (event) => {
      if (event.button != null && event.button !== 0) return;
      dragging = true;
      try { svg.setPointerCapture(event.pointerId); } catch (err) { /* moves still bubble */ }
      paint(event.clientX);
      event.preventDefault();
    });
    svg.addEventListener("pointermove", (event) => { if (dragging) paint(event.clientX); });
    const end = () => { dragging = false; };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    svg.addEventListener("lostpointercapture", end);
  });

  // Password reveal buttons: [data-reveal] names the input to unmask.
  // mousedown is cancelled so the button never takes focus or moves the caret.
  document.querySelectorAll("[data-reveal]").forEach((btn) => {
    btn.addEventListener("mousedown", (event) => event.preventDefault());

    btn.addEventListener("click", (event) => {
      event.preventDefault();
      const input = document.getElementById(btn.dataset.reveal);
      if (!input) return;
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      btn.setAttribute("aria-pressed", reveal ? "true" : "false");
      const label = reveal ? "Hide password" : "Show password";
      btn.setAttribute("aria-label", label);
      btn.title = label;
      input.focus({ preventScroll: true });
      const end = input.value.length;
      try {
        input.setSelectionRange(end, end);
      } catch (err) {
        /* some browsers refuse setSelectionRange on type=password; focus is enough */
      }
    });
  });

  // "Forgot it? Show hint" on the sign-in page.
  document.querySelectorAll("[data-hint-for]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const source = document.getElementById(btn.dataset.hintFor);
      const output = document.getElementById(btn.dataset.hintTarget);
      const token = document.querySelector('input[name="csrf"]');
      if (!output) return;
      try {
        const body = new URLSearchParams({
          csrf: token ? token.value : "",
          handle: source ? source.value : "",
        });
        const res = await fetch("/login/hint", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body,
        });
        const data = await res.json();
        output.hidden = false;
        output.textContent = data.hint
          ? "Hint: " + data.hint
          : "No hint is set for that name. Try the name you used at setup.";
      } catch (err) {
        output.hidden = false;
        output.textContent = "Could not read the hint. Try again.";
      }
    });
  });


  // Settings forms save in place: no reload, so nothing jumps and only the
  // section that saved reports it.
  document.querySelectorAll("form[data-save]").forEach((form) => {
    const status = form.querySelector("[data-save-status]");
    form.addEventListener("submit", async (event) => {
      if (!window.fetch || !status) return; // fall back to a normal POST
      event.preventDefault();

      const button = event.submitter || form.querySelector('button[type="submit"]');
      const body = new FormData(form);
      if (event.submitter && event.submitter.name) {
        body.append(event.submitter.name, event.submitter.value);
      }
      // The global submit guard has already swapped the label for "Working…";
      // it stashed the real one in dataset.label on its way through.
      const label = button ? (button.dataset.label || button.textContent) : "";
      if (button) {
        button.disabled = true;
        button.textContent = button.dataset.busy || "Saving…";
      }
      status.className = "save-status";
      status.textContent = "";

      try {
        const res = await fetch(form.action, {
          method: "POST",
          body,
          headers: { "X-Requested-With": "fetch" },
          credentials: "same-origin",
        });
        const data = await res.json().catch(() => ({ ok: res.ok }));
        if (data.ok) {
          status.textContent = data.message || "Saved.";
          status.className = "save-status ok";
        } else {
          status.textContent = data.error || "Could not save.";
          status.className = "save-status err";
        }
      } catch (err) {
        status.textContent = "Could not save. Check your connection.";
        status.className = "save-status err";
      } finally {
        if (button) {
          button.disabled = false;
          button.textContent = label;
          delete button.dataset.label;
        }
      }
    });
  });


})();
