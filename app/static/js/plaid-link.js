/* Plaid Link — open, exchange, hand back to the server. */
(() => {
  "use strict";
  const meta = document.querySelector('meta[name="csrf"]');
  const CSRF = meta ? meta.content : "";
  const statusEl = document.getElementById("plaid-status");

  function say(message, kind) {
    if (!statusEl) return;
    statusEl.textContent = message;
    statusEl.className = "alert " + (kind || "alert-ok");
    statusEl.hidden = false;
  }

  async function linkToken(mode, itemId) {
    const res = await fetch("/plaid/link-token", {
      method: "POST",
      body: new URLSearchParams({ csrf: CSRF, mode: mode, item_id: itemId || "" }),
      headers: { "X-Requested-With": "fetch" },
    });
    return res.json().catch(() => ({ ok: false, error: "Server error." }));
  }

  /* Plaid injects its own iframe and its own backdrop decision. Pin the frame to the
     viewport, dim what is behind it, lock the page so a scrolled body cannot drag the
     dialog out from under the finger, and hand it focus. */
  const veil = (() => {
    let el = null;
    return (show) => {
      if (show) {
        document.documentElement.classList.add("plaid-open");
        if (!el) {
          el = document.createElement("div");
          el.className = "plaid-veil";
          el.setAttribute("aria-hidden", "true");
          document.body.appendChild(el);
        }
        requestAnimationFrame(() => el && el.classList.add("on"));
      } else {
        document.documentElement.classList.remove("plaid-open");
        if (el) {
          const gone = el;
          gone.classList.remove("on");
          setTimeout(() => gone.remove(), 200);
          el = null;
        }
      }
    };
  })();

  function dressFrames() {
    let found = false;
    document.querySelectorAll('iframe[id^="plaid-link-iframe"]').forEach((frame) => {
      found = true;
      frame.classList.add("plaid-frame");
      frame.setAttribute("title", "Plaid");
      try { frame.focus(); } catch (err) { /* cross-origin: focus the frame itself */ }
    });
    return found;
  }

  function watchForFrame() {
    if (dressFrames()) return;
    const observer = new MutationObserver(() => {
      if (dressFrames()) observer.disconnect();
    });
    observer.observe(document.body, { childList: true });
    setTimeout(() => {
      observer.disconnect();
      // Nothing arrived: give the page back rather than leave it dimmed and locked.
      if (!document.querySelector('iframe[id^="plaid-link-iframe"]')) {
        veil(false);
        say("Plaid did not open. Try again.", "alert-error");
      }
    }, 8000);
  }

  let busy = false;

  async function open(mode, itemId, btn) {
    if (busy) return;
    busy = true;
    const label = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "Opening…"; }
    const release = (wait) => setTimeout(() => {
      busy = false;
      if (btn) { btn.disabled = false; btn.textContent = label; }
    }, wait || 0);
    if (typeof Plaid === "undefined") {
      say("Plaid Link did not load. Check the connection and reload.", "alert-error");
      release();
      return;
    }
    say("Opening Plaid…", "alert-ok");
    const data = await linkToken(mode, itemId);
    if (!data.ok) {
      say(data.error || "Could not start Plaid.", "alert-error");
      // A rate limit is Plaid's abuse guard, not a broken connection: back off a while.
      release(data.rate_limited ? 30000 : 0);
      return;
    }
    veil(true);
    window.scrollTo(0, 0);
    const handler = Plaid.create({
      token: data.link_token,
      onSuccess: async (publicToken, metadata) => {
        veil(false);
        release(1500);
        if (mode === "update") {
          // Reconnect: the Item is already ours. Plaid may hand back a public token
          // after re-initializing it — exchange that against this same row — or none.
          say("Reconnected. Syncing…", "alert-ok");
          if (publicToken) {
            const body = new URLSearchParams({
              csrf: CSRF, public_token: publicToken, item_id: itemId || "", mode: "update",
              institution_id: (metadata.institution && metadata.institution.institution_id) || "",
              institution_name: (metadata.institution && metadata.institution.name) || "",
            });
            const res = await fetch("/plaid/exchange", { method: "POST", body });
            window.location = res.url || "/settings";
            return;
          }
          const body = new URLSearchParams({ csrf: CSRF, item_id: itemId || "" });
          const res = await fetch("/plaid/reconnected", { method: "POST", body });
          window.location = res.url || "/settings?reconnected=1";
          return;
        }
        if (mode === "consent") {
          // Update mode for a new product gives no public token; the server syncs
          // holdings against the existing Item.
          say("Importing holdings…", "alert-ok");
          const body = new URLSearchParams({ csrf: CSRF, item_id: itemId || "" });
          const res = await fetch("/plaid/consented", { method: "POST", body });
          window.location = res.url || "/portfolio";
          return;
        }
        say("Linking…", "alert-ok");
        const body = new URLSearchParams({
          csrf: CSRF,
          public_token: publicToken,
          institution_id: (metadata.institution && metadata.institution.institution_id) || "",
          institution_name: (metadata.institution && metadata.institution.name) || "",
        });
        const res = await fetch("/plaid/exchange", { method: "POST", body });
        window.location = res.url || "/settings";
      },
      onExit: (err) => {
        veil(false);
        release(err ? 5000 : 0);
        if (!err) return;
        const code = err.error_code || "";
        const msg = err.display_message || err.error_message || "cancelled";
        if (code.indexOf("RATE_LIMIT") >= 0) {
          say("Plaid is limiting attempts on this bank. Wait a few minutes before trying again.",
              "alert-error");
          return;
        }
        say("Link closed: " + msg, "alert-error");
      },
    });
    handler.open();
    watchForFrame();
  }

  document.querySelectorAll("[data-plaid-link]").forEach((btn) => {
    btn.addEventListener("click", () =>
      open(btn.dataset.plaidMode || "new", btn.dataset.plaidItem || "", btn));
  });
})();
