// Dieter's chat actions: an answer can carry "do": [{type: "navigate", url, title}]. Shared by every app's chat.
//
//   DieterActions.run(actions, {state, keepChatOn: ["/thanksgiving"], delay: 700})
//     Carries out the actions. Before leaving the page it saves `state` (the chat's conversation) for the next page,
//     but only when that page has the same chat (its path starts with one of keepChatOn). Returns "left", "here" or "".
//   DieterActions.resume()  -> the saved state on the new page (once, within 5 minutes), or null.
//   DieterActions.highlight(id)  -> scroll to an element and flash it (also done on load for a #hash).
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

  function run(actions, options = {}) {
    for (const action of actions || []) {
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

  window.addEventListener("load", () => { if (location.hash) highlight(decodeURIComponent(location.hash.slice(1))); });
  return { run, resume, highlight };
})();
