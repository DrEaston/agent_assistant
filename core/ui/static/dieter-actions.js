// Dieter's chat actions: an answer can carry "do": [{type: "navigate", url, title}, {type: "proposal", card},
// {type: "decided", id, status}, {type: "reload"}]. Shared by every app's chat. (The chat itself shows proposal
// cards and records decided ones; run() carries out navigate and reload.)
//
//   DieterActions.run(actions, {state, keepChatOn: ["/thanksgiving"], delay: 700})
//     Carries out the actions. Before leaving the page it saves `state` (the chat's conversation) for the next page,
//     but only when that page has the same chat (its path starts with one of keepChatOn). Returns "left", "here" or "".
//   DieterActions.resume()  -> the saved state on the new page (once, within 5 minutes), or null.
//   DieterActions.highlight(id)  -> scroll to an element and flash it (also done on load for a #hash).
//   DieterActions.proposalCard(card) -> HTML for a change waiting for the person's OK (core/connector/proposals.py).
//     Its Save / Discard buttons work anywhere on the page; they fire "dieter:proposal" with {card, status}.
//     Elements with data-proposal-card='{card json}' are filled in on load.
window.DieterActions = (() => {
  const KEY = "dieter-chat-resume";

  function highlight(id) {
    const el = id && document.getElementById(id);
    if (!el) return false;
    const details = el.closest("details");
    if (details) details.open = true;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    const before = el.style.boxShadow;
    el.style.transition = "box-shadow .3s";
    el.style.boxShadow = "0 0 0 3px #f59e0b";
    setTimeout(() => { el.style.boxShadow = before; }, 2600);
    return true;
  }

  function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function safeUrl(u) { return typeof u === "string" && u.startsWith("/") && !u.startsWith("//") ? u : ""; }

  const STYLE = `.dieter-proposal { border: 2px solid #d97706; border-radius: 12px; background: #fffbeb; color: #1f2937;
      padding: .7rem .8rem; margin: .4rem 0; font-size: .95rem; }
    .dieter-proposal h4 { margin: 0 0 .3rem; font-size: 1rem; }
    .dieter-proposal .p-reason { margin: 0 0 .4rem; }
    .dieter-proposal .p-label { font-weight: 700; margin-top: .35rem; font-size: .85rem; text-transform: uppercase; letter-spacing: .04em; color: #6b7280; }
    .dieter-proposal .p-line { font-family: ui-monospace, Consolas, monospace; font-size: .85rem; white-space: pre-wrap; padding: 0 .3rem; }
    .dieter-proposal .p-removed { color: #991b1b; background: #fee2e2; text-decoration: line-through; }
    .dieter-proposal .p-added { color: #166534; background: #dcfce7; }
    .dieter-proposal .p-gap { color: #6b7280; font-style: italic; }
    .dieter-proposal ul { margin: .35rem 0 0; padding-left: 1.1rem; }
    .dieter-proposal .p-note { color: #6b7280; font-size: .85rem; margin-top: .35rem; }
    .dieter-proposal .p-actions { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .6rem; align-items: center; }
    .dieter-proposal .p-save { background: #15803d; border-color: #15803d; color: #fff; font-weight: 700; }
    .dieter-proposal .p-status { font-weight: 700; }`;
  function addStyle() {
    if (document.getElementById("dieter-actions-style")) return;
    const style = document.createElement("style");
    style.id = "dieter-actions-style"; style.textContent = STYLE;
    document.head.appendChild(style);
  }

  addStyle();  // also for cards brought back with a restored conversation

  function proposalCard(card) {
    addStyle();
    const diffs = (card.diffs || []).map((d) => `<div class="p-label">${esc(d.label)}</div>` + (d.lines || []).map((l) =>
      `<div class="p-line p-${esc(l.op)}">${l.op === "removed" ? "− " : l.op === "added" ? "+ " : ""}${esc(l.text)}</div>`).join("")).join("");
    const effects = (card.effects || []).length
      ? `<div class="p-label">Shopping list</div><ul>${card.effects.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : "";
    const pending = card.status === "pending";
    const status = { saved: "Saved ✓", discarded: "Discarded", expired: "Expired. Ask again." }[card.status] || "";
    return `<div class="dieter-proposal" data-proposal-id="${esc(card.id)}" data-save-url="${esc(safeUrl(card.save_url))}"
        data-discard-url="${esc(safeUrl(card.discard_url))}" data-open-url="${esc(safeUrl(card.open_url))}">
      <h4>${esc(card.title)}</h4>${card.reason ? `<p class="p-reason">${esc(card.reason)}</p>` : ""}
      ${diffs}${effects}${card.note ? `<div class="p-note">${esc(card.note)}</div>` : ""}
      <div class="p-actions">${pending ? `<button type="button" class="p-save" data-proposal-save>Save change</button>
        <button type="button" data-proposal-discard>Discard</button><span class="p-note">Nothing is saved until you tap Save.</span>` : ""}
        <span class="p-status">${status}</span></div></div>`;
  }

  async function decide(box, which) {
    const url = box.dataset[which === "save" ? "saveUrl" : "discardUrl"];
    if (!url) return;
    box.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    let data = {};
    try {
      const r = await fetch(url, { method: "POST" });
      data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || "That didn't work. Try again.");
    } catch (error) {
      box.querySelector(".p-status").textContent = error.message;
      box.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      return;
    }
    const status = which === "save" ? "saved" : "discarded";
    box.querySelector(".p-actions").innerHTML = `<span class="p-status">${status === "saved" ? "Saved ✓" : "Discarded"}</span>`;
    document.dispatchEvent(new CustomEvent("dieter:proposal", { detail: { card: data.card || {}, status, box } }));
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-proposal-save], [data-proposal-discard]");
    const box = button && button.closest(".dieter-proposal");
    if (box) decide(box, button.hasAttribute("data-proposal-save") ? "save" : "discard");
  });

  function run(actions, options = {}) {
    for (const action of actions || []) {
      if (action.type === "reload") {
        try { if (options.state) sessionStorage.setItem(KEY, JSON.stringify({ ...options.state, at: Date.now() })); } catch (e) {}
        setTimeout(() => location.reload(), options.delay ?? 700);
        return "left";
      }
      if (action.type !== "navigate" || typeof action.url !== "string") continue;
      if (!action.url.startsWith("/") || action.url.startsWith("//")) continue;  // only pages on this site
      const [path, hash] = action.url.split("#");
      if (path === location.pathname) {
        if (hash) { history.replaceState(null, "", "#" + hash); highlight(hash); }
        return "here";
      }
      const keep = (options.keepChatOn || []).some((prefix) => path.startsWith(prefix));
      try {
        if (keep && options.state) sessionStorage.setItem(KEY, JSON.stringify({ ...options.state, at: Date.now() }));
        else sessionStorage.removeItem(KEY);
      } catch (e) {}
      setTimeout(() => location.assign(action.url), options.delay ?? 700);  // a moment to read the reply
      return "left";
    }
    return "";
  }

  function resume() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(KEY) || "null");
      sessionStorage.removeItem(KEY);
      if (saved && Date.now() - saved.at < 5 * 60 * 1000) return saved;
    } catch (e) {}
    return null;
  }

  window.addEventListener("load", () => {
    document.querySelectorAll("[data-proposal-card]").forEach((slot) => {
      try { slot.outerHTML = proposalCard(JSON.parse(slot.dataset.proposalCard)); } catch (e) {}
    });
    if (location.hash) highlight(decodeURIComponent(location.hash.slice(1)));
  });
  return { run, resume, highlight, proposalCard };
})();
