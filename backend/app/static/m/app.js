/* Jarvis on the phone and iPad (PLAN 4.19 phase 1): the Freeform thread over the PC's Tailscale
   address. The server decides this is remote (it came in on the remote port) and keeps the turn
   in the thread whatever we send; this page only needs its device token. */
"use strict";

const THREAD = "jarvis-thread";
const TOKEN_KEY = "jarvis.remote.token";
const STATUS_EVERY_MS = 5000;
const MARK = "icons/jarvis-mark.svg";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const icon = (name, cls = "i s") => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
};

// ---- token storage (survives in the installed app; a private tab simply re-pairs) ----
const store = {
  get() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } },
  set(v) { try { localStorage.setItem(TOKEN_KEY, v); } catch { /* private mode */ } },
  clear() { try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } },
};
let token = store.get();

class Unpaired extends Error {}

// This page's id on the live relay: our own turns come back there too, and are skipped.
const CLIENT_ID = `m-${Math.random().toString(36).slice(2, 10)}`;

async function api(path, { method = "GET", body, signal } = {}) {
  const res = await fetch(path, {
    method, signal, cache: "no-store",
    headers: { "X-Jarvis-Client": CLIENT_ID, ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) throw new Unpaired();
  return res;
}
async function getJson(path) {
  const res = await api(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

// ---- reachability banner ----
let offline = false;
function setOffline(on, title, sub) {
  offline = on;
  $("banner").hidden = !on;
  if (on) {
    $("banner-title").textContent = title || "Can't reach your PC";
    $("banner-sub").textContent = sub || "It may be asleep, or Jarvis is closed.";
  }
}

// ---- pairing ----
function deviceName() {
  const ua = navigator.userAgent;
  if (/iPad|Macintosh/.test(ua) && navigator.maxTouchPoints > 1) return "iPad";
  if (/iPhone/.test(ua)) return "iPhone";
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? "Android phone" : "Android tablet";
  return "Browser";
}

// iPadOS keeps a Home Screen app's storage apart from Safari's, so a device paired in Safari
// would be unpaired once installed: pair from inside the installed app.
const iosBrowserTab = /iPad|iPhone|Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1 && !navigator.standalone;

function showPairing(message) {
  $("app").hidden = true;
  $("pair").hidden = false;
  $("pair-ios").hidden = !iosBrowserTab;
  $("pair-error").hidden = !message;
  if (message) $("pair-error").textContent = message;
}

async function pairWith(code) {
  const btn = $("pair-form").querySelector("button");
  btn.disabled = true;
  try {
    const res = await fetch("/api/remote/pair", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name: deviceName() }),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail;
      showPairing(detail || "That code didn't work. Codes last 10 minutes.");
      return false;
    }
    token = (await res.json()).token;
    store.set(token);
    return true;
  } catch {
    showPairing("Can't reach your PC. It may be asleep, or Jarvis is closed.");
    return false;
  } finally {
    btn.disabled = false;
  }
}

$("pair-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("pair-code").value.trim();
  if (code && await pairWith(code)) start();
});

// ---- time formatting ----
const pad = (n) => String(n).padStart(2, "0");
const clock = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
function dayLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(now) - start(d)) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  const opts = { weekday: "long", day: "numeric", month: "long" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

// ---- a small, safe markdown: paragraphs, lists, headings, code, bold/italic, links ----
function inline(text) {
  const frag = document.createDocumentFragment();
  // Code, bold, italic, [text](url), then a bare URL (its trailing punctuation left out of the link).
  const rx = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"'`]*[^\s<>"'`.,;:!?)\]])/g;
  const link = (label, href) => { const a = el("a", null, label); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; };
  let last = 0, m;
  while ((m = rx.exec(text))) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    if (m[1]) frag.append(el("code", null, m[1].slice(1, -1)));
    else if (m[2]) frag.append(el("strong", null, m[2].slice(2, -2)));
    else if (m[3]) frag.append(el("em", null, m[3].slice(1, -1)));
    else if (m[4]) frag.append(link(m[4], m[5]));
    else frag.append(link(m[6], m[6]));
    last = rx.lastIndex;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}
function renderMarkdown(node, src) {
  node.replaceChildren();
  const lines = (src || "").replace(/\r/g, "").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const pre = el("pre"), code = el("code");
      const body = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++]);
      i++;
      code.textContent = body.join("\n");
      pre.append(code); node.append(pre);
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) { const n = el(`h${h[1].length}`); n.append(inline(h[2])); node.append(n); i++; continue; }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const list = el(ordered ? "ol" : "ul");
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        const li = el("li"); li.append(inline(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ""))); list.append(li); i++;
      }
      node.append(list);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^```|^#{1,4}\s|^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) para.push(lines[i++]);
    const p = el("p");
    para.forEach((t, k) => { if (k) p.append(el("br")); p.append(inline(t)); });
    node.append(p);
  }
}

// ---- transcript ----
const transcript = $("transcript");
const scroller = $("scroll");
let oldestId = null, hasMore = false, loadingOlder = false;
let newestId = 0;                  // the newest stored message shown here (turns from other devices append after it)
let episodes = new Map();          // id -> episode
let lastDay = null;                // day label of the newest rendered message (for appends)

function nearBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120; }
function toBottom() { scroller.scrollTop = scroller.scrollHeight; }

function toolLabel(name, args) {
  const a = args || {};
  const one = a.query || a.url || a.name || a.name_or_path || a.text || "";
  const verbs = { web_search: "Searched", research: "Looked up", fetch_url: "Read", recall: "Recalled" };
  if (name === "look_at_screen") return "Looked at the screen";
  if (verbs[name] && one) return `${verbs[name]}: ${String(one).slice(0, 80)}`;
  return name;
}

function stepRow(name, args, status, result) {
  const wrap = el("div");
  const row = el("button", "step touch-footnote");
  row.setAttribute("aria-expanded", "false");
  const ic = el("span"); const lbl = el("span", "lbl", toolLabel(name, args)); const word = el("span");
  row.append(ic, lbl, word, icon("chev", "i chev"));
  const detail = el("div", "step-detail"); detail.hidden = true;
  const set = (st, res) => {
    const [i, w, c] = st === "running" ? ["sync", "Running", "warn"]
      : st === "error" ? ["err", "Failed", "err"]
      : st === "approval" ? ["warn", "Needs approval", "warn"]
      : st === "denied" ? ["close", "Denied", "sub"] : ["check", "Done", "ok"];
    ic.replaceChildren(icon(i, `i s ${c}${st === "running" ? " spin" : ""}`));
    word.textContent = w; word.className = c;
    detail.replaceChildren();
    if (args && Object.keys(args).length) { detail.append(el("div", "touch-footnote sub", "Arguments"), el("div", "data", JSON.stringify(args, null, 1))); }
    if (res) { detail.append(el("div", "touch-footnote sub", "Result"), el("div", "data", String(res).slice(0, 4000))); }
  };
  set(status, result);
  row.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    row.setAttribute("aria-expanded", String(!detail.hidden));
  });
  wrap.append(row, detail);
  wrap.setStatus = set;
  return wrap;
}

function assistantTurn() {
  const turn = el("div", "asst");
  const img = el("img"); img.src = MARK; img.alt = "";
  const col = el("div", "col");
  const steps = el("div", "col"); steps.style.gap = "2px";
  const text = el("div", "text touch-message");
  col.append(steps, text);
  turn.append(img, col);
  return { node: turn, col, steps, text };
}

function markFor(ep, startedAt) {
  const title = ep && ep.title ? ep.title : "New topic";
  const m = el("div", "mark touch-footnote", `${title} · ${clock(ep ? ep.started_at : startedAt)}`);
  if (ep) m.id = `ep-${ep.id}`;
  return m;
}

/** A stored tool row's call arguments: the row carries its own call in ``tool_calls``. */
function storedArgs(r) {
  const fn = (r.tool_calls && r.tool_calls[0] && (r.tool_calls[0].function || r.tool_calls[0])) || {};
  let args = fn.arguments || {};
  if (typeof args === "string") { try { args = JSON.parse(args); } catch { args = {}; } }
  return args;
}

/** Turn stored rows into nodes, oldest first. Each tool that ran is stored as its own "tool" row
    (name, its call, its result) *before* the answer it led to; the steps go back on that answer,
    or on an answer-less turn when the turn ended without one (a confirmation) -- as the desktop
    reads them (ChatViewModel.MapHistory). */
function buildRows(rows, prevDay) {
  const out = [];
  let day = prevDay;
  let steps = [];                    // step rows waiting for the answer they belong to
  const flushSteps = () => {
    if (!steps.length) return;
    const turn = assistantTurn();
    turn.text.remove();
    steps.forEach((s) => turn.steps.append(s));
    out.push(turn.node);
    steps = [];
  };
  for (const r of rows) {
    if (r.role === "system" || r.is_summary) continue;
    if (r.role === "tool") {
      steps.push(stepRow(r.name || "tool", storedArgs(r), /^error/i.test(r.content || "") ? "error" : "success", r.content));
      continue;
    }
    if (r.role === "user") {
      flushSteps();
      const d = r.created_at ? dayLabel(r.created_at) : day;  // the day first, then the topic it opens
      if (d && d !== day) { out.push(el("div", "day touch-footnote", d)); day = d; }
    }
    if (r.episode_start) out.push(markFor(episodes.get(r.episode_start), r.created_at));
    if (r.role === "user") {
      out.push(el("div", "user touch-message", r.content));
      continue;
    }
    if (r.role === "assistant") {
      const turn = assistantTurn();
      steps.forEach((s) => turn.steps.append(s));
      steps = [];
      if (r.content && r.content.trim()) renderMarkdown(turn.text, r.content);
      else turn.text.remove();
      out.push(turn.node);
    }
  }
  flushSteps();
  return { nodes: out, day };
}

async function loadEpisodes() {
  try {
    const data = await getJson(`/api/thread/episodes?limit=200`);
    episodes = new Map(data.episodes.map((e) => [e.id, e]));
    renderSidebar(data.episodes);
  } catch (e) { if (e instanceof Unpaired) throw e; }
}

async function loadLatest() {
  const data = await getJson(`/api/thread/messages?limit=80`);
  const { nodes, day } = buildRows(data.messages, null);
  transcript.replaceChildren(...nodes);
  lastDay = day;
  oldestId = data.messages.length ? data.messages[0].id : null;
  newestId = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
  hasMore = data.has_more;
  requestAnimationFrame(toBottom);
}

/** Turns another device (the desktop, the HUD) added since we last looked: appended, never
    while a turn of ours streams. After our own turn we only adopt the head (it's on screen). */
let catchingUp = false;
async function catchUp({ adoptOnly = false } = {}) {
  // A turn being relayed live is drawn already; its rows are adopted at its turn_end.
  if (catchingUp || (!adoptOnly && (streaming || liveTurns.size))) return;
  catchingUp = true;
  try {
    const head = (await getJson("/api/thread/head")).last_message_id;
    if (adoptOnly || head <= newestId) { newestId = Math.max(newestId, head); return; }
    const follow = nearBottom();
    for (let guard = 0; guard < 10; guard++) {
      const data = await getJson(`/api/thread/messages?after_id=${newestId}&limit=200`);
      if (!data.messages.length) break;
      if (data.messages.some((m) => m.episode_start && !episodes.has(m.episode_start))) await loadEpisodes();
      const armed = transcript.querySelector(".mark.armed");  // stays last: it marks our next message
      const { nodes, day } = buildRows(data.messages, lastDay);
      transcript.append(...nodes);
      if (armed) transcript.append(armed);
      lastDay = day;
      newestId = data.messages[data.messages.length - 1].id;
      if (!data.has_more) break;
    }
    if (follow) requestAnimationFrame(toBottom);
  } catch (e) {
    if (e instanceof Unpaired) showPairing("This device was removed on your PC. Pair it again.");
  } finally {
    catchingUp = false;
  }
}

async function loadOlder() {
  if (!hasMore || loadingOlder || oldestId == null) return;
  loadingOlder = true;
  $("older").hidden = false;
  try {
    const data = await getJson(`/api/thread/messages?before_id=${oldestId}&limit=80`);
    const { nodes } = buildRows(data.messages, null);
    const before = scroller.scrollHeight;
    // A day divider at the top of the old page duplicates the first one of the loaded page.
    const firstDay = transcript.querySelector(".day");
    const lastNewDay = [...nodes].reverse().find((n) => n.classList && n.classList.contains("day"));
    if (firstDay && lastNewDay && firstDay.textContent === lastNewDay.textContent && transcript.firstChild === firstDay) firstDay.remove();
    transcript.prepend(...nodes);
    scroller.scrollTop += scroller.scrollHeight - before;   // what was on screen stays put
    oldestId = data.messages.length ? data.messages[0].id : oldestId;
    hasMore = data.has_more;
  } catch (e) { if (e instanceof Unpaired) return showPairing(); }
  finally { loadingOlder = false; $("older").hidden = true; }
}
scroller.addEventListener("scroll", () => { if (scroller.scrollTop < 200) loadOlder(); }, { passive: true });

// ---- sidebar ----
function renderSidebar(list) {
  const box = $("episodes");
  box.replaceChildren();
  let day = null;
  for (const ep of list) {   // newest first
    const d = dayLabel(ep.started_at);
    if (d !== day) { box.append(el("div", "gh touch-footnote", d)); day = d; }
    const row = el("button", "srow touch-body");
    row.append(icon("clock"), el("span", null, `${ep.title || "New topic"} · ${clock(ep.started_at)}`));
    row.addEventListener("click", () => jumpTo(ep.id));
    box.append(row);
  }
}
async function jumpTo(id) {
  closeSidebar();
  for (let tries = 0; tries < 20 && !$(`ep-${id}`) && hasMore; tries++) await loadOlder();
  const mark = $(`ep-${id}`);
  if (mark) mark.scrollIntoView({ block: "start", behavior: "smooth" });
}
function openSidebar() { $("sidebar").classList.add("open"); $("scrim").hidden = false; }
function closeSidebar() { $("sidebar").classList.remove("open"); $("scrim").hidden = true; }
$("side-open").addEventListener("click", openSidebar);
$("side-close").addEventListener("click", closeSidebar);
$("scrim").addEventListener("click", closeSidebar);
$("row-thread").addEventListener("click", () => { closeSidebar(); toBottom(); });

// ---- sending a turn ----
let streaming = null;              // AbortController while a turn streams
let newTopic = false;
let lastPrompt = "";
let confirmTimeout = 90;

const input = $("input");
function autosize() { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 160)}px`; }
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  // A hardware keyboard (iPad): Enter sends, Shift+Enter breaks the line. On-screen keyboards
  // send with the button.
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer: fine)").matches) {
    e.preventDefault(); $("composer").requestSubmit();
  }
});
$("new-topic").addEventListener("click", () => {
  newTopic = !newTopic;
  $("new-topic").setAttribute("aria-pressed", String(newTopic));
  const armed = transcript.querySelector(".mark.armed");
  if (newTopic && !armed) { const m = el("div", "mark touch-footnote armed", "New topic · starts with your next message"); transcript.append(m); toBottom(); }
  if (!newTopic && armed) armed.remove();
});

function setStreaming(on) {
  const btn = $("send");
  btn.replaceChildren(icon(on ? "stop" : "send", "i"));
  btn.setAttribute("aria-label", on ? "Stop" : "Send");
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  if (streaming) { streaming.abort(); return; }
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; autosize();
  const d = dayLabel(Date.now() / 1000);
  if (d !== lastDay) { transcript.append(el("div", "day touch-footnote", d)); lastDay = d; }
  const armed = transcript.querySelector(".mark.armed");
  if (armed) { armed.classList.remove("armed"); armed.textContent = `New topic · ${clock(Date.now() / 1000)}`; }
  transcript.append(el("div", "user touch-message", text));
  lastPrompt = text;
  runTurn(text, null);
});

/** Reads an SSE response body (fetch: EventSource can't POST or send a token) frame by frame. */
async function readSse(res, onFrame) {
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += dec.decode(value, { stream: true });
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, cut); buf = buf.slice(cut + 2);
      let ev = "message", data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) ev = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      let d;
      try { d = JSON.parse(data); } catch { continue; }
      onFrame(ev, d);
    }
  }
}

/** Draws one assistant turn from its stream events -- ours, or one relayed live from another
    device (``own`` false: its approval card can only be answered where it was asked). */
function turnView(turn, own) {
  let text = "";
  const running = [];              // step rows waiting for their tool_end, in order
  const view = { ended: false, get text() { return text; } };
  const paint = () => { const follow = nearBottom(); renderMarkdown(turn.text, text); if (follow) toBottom(); };
  turn.text.classList.add("caret");
  view.handle = (ev, d) => {
    switch (ev) {
      case "reasoning":   // the model thinks before it answers: say so instead of a bare caret
        if (!text && !turn.thinking) {
          turn.thinking = el("div", "touch-footnote meta", "Thinking…");
          turn.col.insertBefore(turn.thinking, turn.text);
        }
        break;
      case "token": case "text_delta":
        if (turn.thinking) { turn.thinking.remove(); turn.thinking = null; }
        text += d.delta || d.content || ""; paint(); break;
      case "tool_start": { const row = stepRow(d.tool, d.args, "running"); running.push(row); turn.steps.append(row); paint(); break; }
      case "tool_end": {
        const row = running.shift() || (() => { const r = stepRow(d.tool, d.args, "running"); turn.steps.append(r); return r; })();
        row.setStatus(d.status === "success" ? "success" : "error", d.result || d.summary);
        break;
      }
      case "episode":
        if (d.opened) { transcript.insertBefore(markFor(null, Date.now() / 1000), turn.node.previousSibling || turn.node); loadEpisodes(); }
        break;
      case "confirmation_required":
        view.ended = true;
        text = d.response || text; paint();
        if (own) showApproval(turn, d.pending_confirmations || []);
        else showForeignApproval(turn, d.pending_confirmations || []);
        break;
      case "done":
        view.ended = true;
        if (d.response) { text = d.response; paint(); }
        if (d.model && d.model !== "none") turn.col.append(el("div", "data meta", d.model));
        break;
      case "error":
        view.ended = true;
        turn.col.append(el("div", "touch-footnote failed", `Something went wrong: ${d.error || "the turn failed"}.`));
        break;
    }
  };
  view.finish = () => {
    if (turn.thinking) { turn.thinking.remove(); turn.thinking = null; }
    turn.text.classList.remove("caret");
    if (!text && view.ended) turn.text.remove();
    if (!view.ended) loadEpisodes();
  };
  return view;
}

/** One /chat/stream call of our own. */
async function runTurn(message, approvedIds) {
  const turn = assistantTurn();
  transcript.append(turn.node);
  toBottom();
  const view = turnView(turn, true);
  const ctrl = new AbortController();
  streaming = ctrl; setStreaming(true);
  const body = { message, session_id: THREAD, chat_mode: "FREEFORM", new_topic: newTopic && !approvedIds };
  if (approvedIds) body.approved_action_ids = approvedIds;
  if (newTopic && !approvedIds) { newTopic = false; $("new-topic").setAttribute("aria-pressed", "false"); }
  try {
    const res = await api("/chat/stream", { method: "POST", body, signal: ctrl.signal });
    if (res.status === 429) {
      turn.text.classList.add("failed");
      turn.text.textContent = "Your PC is busy with something heavy right now, so Jarvis is holding off. Try again in a moment.";
      view.ended = true;
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    setOffline(false);
    await readSse(res, view.handle);
  } catch (e) {
    if (e instanceof Unpaired) { showPairing("This device was removed on your PC. Pair it again."); return; }
    if (e.name === "AbortError") { turn.col.append(el("div", "touch-footnote meta", "Stopped")); return; }
    setOffline(true);
    turn.col.append(el("div", "touch-footnote failed", "The reply didn't come through. Your PC may have gone to sleep."));
  } finally {
    view.finish();
    streaming = null; setStreaming(false);
    catchUp({ adoptOnly: true });   // our own turn is already on screen
  }
}

// ---- other devices' turns, live (GET /api/thread/live) ----
const liveTurns = new Map();       // turn id -> turnView, for turns another device is running now

function onLiveFrame(ev, d) {
  if (ev === "ready" || d._origin === CLIENT_ID) return;
  if (ev === "turn_start") {
    if (!d.resumed) {  // an approval's resume re-sends the prompt: the bubble is already there
      const d0 = dayLabel(Date.now() / 1000);
      if (d0 !== lastDay) { transcript.append(el("div", "day touch-footnote", d0)); lastDay = d0; }
      const armed = transcript.querySelector(".mark.armed");
      const bubble = el("div", "user touch-message", d.message);
      if (armed) transcript.insertBefore(bubble, armed); else transcript.append(bubble);
    }
    const turn = assistantTurn();
    const armed = transcript.querySelector(".mark.armed");
    if (armed) transcript.insertBefore(turn.node, armed); else transcript.append(turn.node);
    if (nearBottom()) toBottom();
    liveTurns.set(d._turn, turnView(turn, false));
    return;
  }
  const view = liveTurns.get(d._turn);
  if (!view) return;               // joined mid-turn: the stored thread catches it up after
  if (ev === "turn_end") {
    view.finish();
    liveTurns.delete(d._turn);
    if (!liveTurns.size) catchUp({ adoptOnly: true });   // what was relayed is what was stored
    return;
  }
  view.handle(ev, d);
}

let liveCtrl = null;
async function connectLive() {
  if (liveCtrl || !token) return;
  liveCtrl = new AbortController();
  try {
    const res = await api("/api/thread/live", { signal: liveCtrl.signal });
    if (res.ok) await readSse(res, onLiveFrame);
  } catch (e) {
    if (e instanceof Unpaired) return showPairing("This device was removed on your PC. Pair it again.");
  } finally {
    liveCtrl = null;
    for (const view of liveTurns.values()) view.finish();   // cut off mid-turn: the head check fills it in
    liveTurns.clear();
  }
  setTimeout(() => { if (!document.hidden) connectLive(); }, 3000);
}

function showForeignApproval(turn, pending) {
  if (!pending.length) return;
  const card = el("div", "card is-done");
  for (const p of pending) {
    const hd = el("div", "hd");
    hd.append(icon("warn", "i warn"), el("span", "touch-headline", approvalTitle(p)));
    card.append(hd);
    if (p.reason) card.append(el("div", "touch-footnote sub", p.reason));
    turn.steps.append(stepRow(p.tool, p.args, "approval"));
  }
  card.append(el("div", "exp touch-footnote", "Waiting for an answer on the device that asked."));
  turn.col.append(card);
  if (nearBottom()) toBottom();
}

function approvalTitle(p) {
  const a = p.args || {};
  switch (p.tool) {
    case "launch_app": return `Open ${a.name_or_path || a.name || "an app"}?`;
    case "focus_app": return `Switch to ${a.name || "an app"}?`;
    case "type_text": return "Type on your PC?";
    case "press_keys": return `Press ${a.keys || "keys"}?`;
    case "kill_process": return `Close ${a.pid_or_name || "a program"}?`;
    case "write_file": case "patch_file": return `Change ${String(a.file_path || a.path || "a file").split(/[\\/]/).pop()}?`;
    case "execute_command": return "Run a command?";
    default: return `Allow ${p.tool}?`;
  }
}

function showApproval(turn, pending) {
  if (!pending.length) return;
  const ids = pending.map((p) => p.action_id);
  const card = el("div", "card");
  for (const p of pending) {
    const hd = el("div", "hd");
    hd.append(icon("warn", "i warn"), el("span", "touch-headline", approvalTitle(p)));
    card.append(hd);
    if (p.reason) card.append(el("div", "touch-footnote sub", p.reason));
    const row = stepRow(p.tool, p.args, "approval");
    turn.steps.append(row);
    p._row = row;
  }
  const btns = el("div", "btns touch-body");
  const deny = el("button", "btn deny", "Deny"), allow = el("button", "btn allow", "Allow");
  btns.append(deny, allow);
  const exp = el("div", "exp data");
  card.append(btns, exp);
  turn.col.append(card);
  toBottom();

  const until = Date.now() + confirmTimeout * 1000;
  const tick = () => {
    const left = Math.max(0, Math.round((until - Date.now()) / 1000));
    if (left > 0) { exp.textContent = `Expires in ${left} s`; return; }
    clearInterval(timer);
    btns.remove(); card.classList.add("is-done");
    exp.textContent = "Expired · ask again";
  };
  const timer = setInterval(tick, 1000); tick();
  const finish = (word) => { clearInterval(timer); btns.remove(); card.classList.add("is-done"); exp.textContent = word; };

  allow.addEventListener("click", () => {
    if (streaming) return;
    finish("Allowed");
    pending.forEach((p) => p._row.setStatus("running"));
    runTurn(lastPrompt || "yes", ids);
  });
  deny.addEventListener("click", async () => {
    finish("Denied");
    pending.forEach((p) => p._row.setStatus("denied"));
    for (const id of ids) { try { await api(`/api/confirmations/${id}/deny`, { method: "POST" }); } catch { /* expired already */ } }
  });
}

// ---- status pill and sheet ----
let status = null;
function gb(mb) { return mb == null ? "—" : (mb / 1024).toFixed(1); }
function paintStatus() {
  const s = status;
  const pill = $("pill"), name = $("pill-name"), vram = $("pill-vram");
  pill.classList.remove("is-warn", "is-err", "is-info");
  let ic = "check", word = "Running", cls = "ok";
  if (!s) { ic = "err"; word = "Offline"; cls = "err"; name.textContent = "Offline"; vram.textContent = ""; pill.classList.add("is-err"); }
  else if (s.game_mode) { ic = "game"; word = "Game mode"; cls = "info"; name.textContent = "Game mode"; vram.textContent = ""; pill.classList.add("is-info"); }
  else if (s.model_unloaded || !s.model) { ic = "info"; word = s.model_unloaded ? "Unloaded" : "Not loaded"; cls = "sub"; name.textContent = s.model_unloaded ? "Model unloaded" : "No model loaded"; vram.textContent = ""; }
  else {
    if (s.launch_adjustment) { ic = "warn"; word = "Reduced"; cls = "warn"; pill.classList.add("is-warn"); }
    // The card's VRAM in use, as the desktop pill shows it (model_vram_mb is fixed at load).
    name.textContent = s.model; vram.textContent = s.vram_total_mb ? `${gb(s.vram_used_mb)} GB` : "";
  }
  $("pill-icon").replaceChildren(...icon(ic, `i s ${cls}`).childNodes);
  $("pill-icon").setAttribute("class", `i s ${cls}`);

  $("st-icon").setAttribute("class", `i ${cls}`);
  $("st-icon").replaceChildren(...icon(ic).childNodes);
  $("st-model").textContent = s ? (s.model_file || s.model || "No model loaded") : "Your PC is offline";
  $("st-word").textContent = word; $("st-word").className = `touch-footnote ${cls}`;
  $("st-detail").textContent = s ? [s.slot, s.ctx_size ? `${Math.round(s.ctx_size / 1024)}k context` : null, s.launch_adjustment].filter(Boolean).join(" · ") : "";
  const vPct = s && s.vram_total_mb ? (100 * s.vram_used_mb) / s.vram_total_mb : 0;
  $("st-vram").textContent = s && s.vram_total_mb ? `${gb(s.vram_used_mb)} / ${gb(s.vram_total_mb)} GB` : "—";
  meter($("st-vram-bar"), vPct, 88, s ? s.vram_threshold : 92);
  const g = s ? s.gpu_util_percent : null;
  $("st-gpu").textContent = g == null ? "—" : `${Math.round(g)}%`;
  $("st-gpu-label").textContent = s && s.gpu_temp_c ? `GPU · ${Math.round(s.gpu_temp_c)} °C` : "GPU";
  meter($("st-gpu-bar"), g || 0, 90, Math.max(90, s ? s.gpu_threshold : 97));

  const gov = $("tg-gov"), game = $("tg-game");
  gov.setAttribute("aria-checked", String(!!(s && s.governor_auto)));
  game.setAttribute("aria-checked", String(!!(s && s.game_mode)));
  gov.disabled = game.disabled = !s;
  $("st-gov-cap").textContent = !s ? "—" : s.game_mode ? "Paused while Game mode is on"
    : s.governor_auto ? "Auto · yields VRAM to other apps" : "Off · ignores load, never unloads";
  $("st-game-cap").textContent = !s ? "—" : s.game_mode ? "On · the model stays unloaded" : "Off · turn on before a game starts";
}
// meter-high past the soft limit (StatusPopover: VRAM 88%, GPU 90%), meter-critical at the
// governor's eviction line.
function meter(bar, pct, soft, evict) {
  bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  bar.style.background = pct >= evict ? "var(--meter-critical)" : pct >= soft ? "var(--meter-high)" : "var(--meter-normal)";
}
async function pollStatus() {
  if (document.hidden) return;
  try {
    status = await getJson("/api/remote/status");
    if (offline) setOffline(false);
    if (!streaming) catchUp();
  } catch (e) {
    if (e instanceof Unpaired) return showPairing("This device was removed on your PC. Pair it again.");
    status = null;
    setOffline(true);
  }
  paintStatus();
}
function openSheet() { $("sheet").hidden = false; $("sheet-scrim").hidden = false; pollStatus(); }
function closeSheet() { $("sheet").hidden = true; $("sheet-scrim").hidden = true; }
$("pill").addEventListener("click", () => ($("sheet").hidden ? openSheet() : closeSheet()));
$("sheet-scrim").addEventListener("click", closeSheet);
// Swipe the sheet down to close it.
(() => {
  let y0 = null;
  const sheet = $("sheet");
  sheet.addEventListener("touchstart", (e) => { y0 = e.touches[0].clientY; }, { passive: true });
  sheet.addEventListener("touchmove", (e) => {
    if (y0 == null) return;
    const dy = Math.max(0, e.touches[0].clientY - y0);
    sheet.style.transform = dy ? `translateY(${dy}px)` : "";
  }, { passive: true });
  sheet.addEventListener("touchend", (e) => {
    const dy = y0 == null ? 0 : e.changedTouches[0].clientY - y0;
    y0 = null; sheet.style.transform = "";
    if (dy > 80) closeSheet();
  });
})();

async function toggle(btn, path) {
  const want = btn.getAttribute("aria-checked") !== "true";
  btn.disabled = true;
  btn.setAttribute("aria-checked", String(want));
  try { await api(path, { method: "POST", body: { enabled: want } }); }
  catch (e) { if (e instanceof Unpaired) return showPairing(); }
  finally { btn.disabled = false; pollStatus(); }
}
// ---- notifications (PLAN 4.19 phase 2): web push while you're away from the PC ----
const pushSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
let notifyOn = false;              // what the server has for this device

function b64urlToBytes(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function paintNotify(note) {
  const tg = $("tg-notify"), cap = $("st-notify-cap");
  tg.setAttribute("aria-checked", String(notifyOn));
  $("notify-test").hidden = !notifyOn;
  if (!pushSupported) {
    tg.disabled = true;
    cap.textContent = iosBrowserTab ? "Add Jarvis to the Home Screen to get notifications" : "Not supported in this browser";
  } else if (Notification.permission === "denied") {
    tg.disabled = true;
    cap.textContent = "Blocked: allow notifications for Jarvis in this device's settings";
  } else {
    tg.disabled = false;
    cap.textContent = note || (notifyOn ? "On · reminders and alerts while you're away from your PC" : "Off · nothing arrives while you're away");
  }
}

async function setNotify(want) {
  const tg = $("tg-notify");
  tg.disabled = true;
  try {
    const reg = await navigator.serviceWorker.ready;
    if (want) {
      // Must follow the tap directly: iOS only asks from a user gesture.
      if (await Notification.requestPermission() !== "granted") { notifyOn = false; return paintNotify(); }
      const key = (await getJson("/api/remote/push/key")).public_key;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64urlToBytes(key) });
      const res = await api("/api/remote/push", { method: "POST", body: sub.toJSON() });
      notifyOn = res.ok;
      paintNotify(res.ok ? null : "Couldn't turn them on. Try again.");
    } else {
      const sub = await reg.pushManager.getSubscription();
      if (sub) await sub.unsubscribe().catch(() => {});
      await api("/api/remote/push", { method: "DELETE" });
      notifyOn = false;
      paintNotify();
    }
  } catch (e) {
    if (e instanceof Unpaired) return showPairing();
    paintNotify("Couldn't change notifications: " + (e.message || e));
  } finally {
    tg.disabled = !pushSupported;
  }
}
$("tg-notify").addEventListener("click", () => setNotify(!notifyOn));
$("notify-test").addEventListener("click", async () => {
  const btn = $("notify-test");
  btn.disabled = true;
  try {
    const res = await api("/api/remote/push/test", { method: "POST" });
    paintNotify(res.ok ? "Test sent · it should arrive in a few seconds" : (await res.json().catch(() => ({}))).detail || "The test didn't go through.");
  } catch { paintNotify("The test didn't go through."); }
  finally { btn.disabled = false; }
});

$("tg-gov").addEventListener("click", (e) => toggle(e.currentTarget, "/governor/auto"));
$("tg-game").addEventListener("click", (e) => toggle(e.currentTarget, "/api/game-mode"));

// ---- start ----
let statusTimer = null;
async function start() {
  if (!token) return showPairing();
  try {
    const me = await getJson("/api/remote/me");
    if (!me.device) return showPairing();
    notifyOn = !!me.device.notifications;
  } catch (e) {
    if (e instanceof Unpaired) { store.clear(); token = ""; return showPairing(); }
    // Not reachable: show the app with the banner; it retries on its own.
  }
  $("pair").hidden = true;
  $("app").hidden = false;
  paintNotify();
  try {
    await loadEpisodes();
    await loadLatest();
    const conf = await getJson("/api/confirmations?session_id=jarvis-thread");
    confirmTimeout = conf.timeout_seconds || confirmTimeout;
    setOffline(false);
  } catch (e) {
    if (e instanceof Unpaired) return showPairing();
    setOffline(true);
  }
  pollStatus();
  connectLive();
  clearInterval(statusTimer);
  statusTimer = setInterval(pollStatus, STATUS_EVERY_MS);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && token && !$("app").hidden && !streaming) { pollStatus(); connectLive(); if (offline) start(); }
});

// A QR from desktop Settings opens /m/#pair=<code>: pair at once, then drop the code from the URL.
async function pairFromHash() {
  const m = /[#&]pair=([0-9A-Za-z]+)/.exec(location.hash);
  if (!m) return true;
  history.replaceState(null, "", location.pathname);
  return pairWith(m[1]);
}
window.addEventListener("hashchange", async () => { if (/pair=/.test(location.hash) && await pairFromHash()) start(); });

(async () => {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  if (await pairFromHash()) start();
})();
