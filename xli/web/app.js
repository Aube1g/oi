/* XLI web — a client of the same kernel the CLI and the TUI talk to.
 *
 * Two channels, matching how the transport is built:
 *
 *   POST /rpc     request and reply — `hello`, `tools.list`, `agent.run`
 *   GET  /events  Server-Sent Events — tool calls, assistant text, steps
 *
 * No frameworks, no build step: the page is three files, and the only network
 * calls it makes are to the process that served it.
 */

"use strict";

const SPINNER = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"];
const GLYPH = {
  read: "◇", write: "◆", edit: "✎", apply_patch: "✎", bash: "❯", grep: "⌕",
  glob: "✦", ls: "▤", outline: "❖", repo_map: "▦", dep_graph: "⛓", chart: "▁",
  delegate: "◆", git: "⁝", todo: "☐", think: "∴", web_search: "⌾",
  fetch_page: "⇩", fetch_pages: "⇩", mcp_call: "⚙", mcp_list: "⚙",
  search_status: "⌕", json_query: "{}",
};

const ARG_KEYS = ["path", "file", "file_path", "target", "query", "pattern",
  "command", "task", "name", "module", "symbol", "url", "kind"];

/* The same six colours xli/ui/agents.py hands the terminal, in the same order,
 * and the same hash — a sub-agent must be the colour in the browser that it is
 * in the TUI, or the two front ends teach the user two different mappings. */
const AGENT_COLORS = ["#9D4EDD", "#7EE7B0", "#FFC46E", "#8CBEFF", "#E0AAFF", "#FF7A95"];
const AGENT_NAMED = {
  accent: "#9D4EDD", good: "#7EE7B0", warn: "#FFC46E",
  bad: "#FF7A95", blue: "#8CBEFF", magenta: "#E0AAFF",
};

function agentColor(name, hint) {
  const named = AGENT_NAMED[String(hint || "").trim().toLowerCase()];
  if (named) return named;
  const text = String(name || "");
  if (!text) return "";
  let digest = 0;
  for (let i = 0; i < text.length; i += 1) {
    digest = (digest * 131 + text.charCodeAt(i)) % 100003;   // as in agents.py
  }
  return AGENT_COLORS[digest % AGENT_COLORS.length];
}

function agentOf(params) {
  const id = String((params && params.agent_id) || "main");
  const name = String((params && params.agent_name) || "");
  if (id === "main" || !name) return { id: "main", name: "XLI", colour: "" };
  return { id, name, colour: agentColor(name, params && params.agent_hint) };
}

const state = {
  busy: false,
  startedAt: 0,
  timer: null,
  frame: 0,
  steps: 0,
  maxSteps: 0,
  tools: 0,
  errors: 0,
  lastTask: "",
  toolRow: null,
  assistantRow: null,
  assistantAgent: "main",
  events: 0,
  agents: [],
};

/* ---------------------------------------------------------------- helpers */
const $ = (id) => document.getElementById(id);

function ru(value, digits = 1) {
  return Number(value || 0).toFixed(digits).replace(".", ",");
}

function plural(count, one, few, many) {
  const n = Math.abs(Math.floor(count || 0)) % 100;
  if (n >= 11 && n <= 14) return many;
  const d = n % 10;
  if (d === 1) return one;
  if (d >= 2 && d <= 4) return few;
  return many;
}

function elapsed() {
  return (performance.now() - state.startedAt) / 1000;
}

function duration(seconds) {
  if (seconds < 60) return `${ru(seconds)} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${String(Math.floor(seconds % 60)).padStart(2, "0")} с`;
}

function glyph(name) {
  return GLYPH[name] || "◆";
}

function argSummary(args) {
  if (!args || typeof args !== "object") return "";
  for (const key of ARG_KEYS) {
    const value = args[key];
    if (typeof value === "string" && value.length) return value;
  }
  for (const [key, value] of Object.entries(args)) {
    if (typeof value === "string" && value.length) return `${key}=${value}`;
  }
  return "";
}

/* ------------------------------------------------------- markdown, in JS */
/* Deliberately small: what a model actually writes into a chat. Fenced code
 * keeps its bytes, a link keeps its label (a URL doubles the width of every
 * citation), and `==метка==` becomes a highlight rather than two equals signs. */
function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function inlineSpans(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/==([^=]+)==/g, "<mark>$1</mark>");
  out = out.replace(/~~([^~]+)~~/g, "<s>$1</s>");
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  return out;
}

function renderMarkdown(text) {
  const lines = String(text || "").split("\n");
  const html = [];
  let fence = null;
  let list = null;
  let table = false;

  const closeList = () => {
    if (list) { html.push(`</${list}>`); list = null; }
  };
  const closeTable = () => {
    if (table) { html.push("</table>"); table = false; }
  };
  const closeBlocks = () => { closeList(); closeTable(); };
  // One flat list with a per-item indent, rather than nested <ul> trees: a
  // model's markdown is rarely well-formed enough for the tree to survive, and
  // a sub-item that renders one level in is what the reader wanted anyway.
  const indent = (depth) => (depth > 0 ? ` style="margin-left:${depth * 1.2}em"` : "");

  for (const raw of lines) {
    const fenceMatch = raw.match(/^\s*```\s*(\S*)/);
    if (fenceMatch) {
      if (fence === null) {
        closeBlocks();
        fence = fenceMatch[1] || "code";
        html.push(`<pre data-lang="${escapeHtml(fence)}"><code>`);
      } else {
        html.push("</code></pre>");
        fence = null;
      }
      continue;
    }
    if (fence !== null) { html.push(escapeHtml(raw) + "\n"); continue; }

    const heading = raw.match(/^(#{1,3})\s+(.*)$/);
    if (heading) { closeBlocks(); html.push(`<h${heading[1].length}>${inlineSpans(heading[2])}</h${heading[1].length}>`); continue; }

    if (/^\s*([-*_])\1{2,}\s*$/.test(raw)) { closeBlocks(); html.push("<hr>"); continue; }

    const task = raw.match(/^\s*[-*+]\s+\[( |x|X)\]\s+(.*)$/);
    if (task) {
      if (list !== "ul task") { closeBlocks(); html.push('<ul class="task">'); list = "ul task"; }
      html.push(`<li>${task[1] === " " ? "☐" : "☑"} ${inlineSpans(task[2])}</li>`);
      continue;
    }

    const bullet = raw.match(/^(\s*)[-*+]\s+(.*)$/);
    if (bullet) {
      if (list !== "ul") { closeBlocks(); html.push("<ul>"); list = "ul"; }
      const depth = Math.floor(bullet[1].length / 2);
      html.push(`<li${indent(depth)}>${inlineSpans(bullet[2])}</li>`);
      continue;
    }

    const ordered = raw.match(/^(\s*)\d+[.)]\s+(.*)$/);
    if (ordered) {
      if (list !== "ol") { closeBlocks(); html.push("<ol>"); list = "ol"; }
      const depth = Math.floor(ordered[1].length / 2);
      html.push(`<li${indent(depth)}>${inlineSpans(ordered[2])}</li>`);
      continue;
    }

    const quote = raw.match(/^>\s?(.*)$/);
    if (quote) { closeBlocks(); html.push(`<blockquote>${inlineSpans(quote[1])}</blockquote>`); continue; }

    if (/^\s*\|.*\|\s*$/.test(raw)) {
      closeList();
      const cells = raw.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      if (cells.every((cell) => /^:?-{2,}:?$/.test(cell))) continue;
      const tag = table ? "td" : "th";
      if (!table) { html.push("<table>"); table = true; }
      html.push(`<tr>${cells.map((cell) => `<${tag}>${inlineSpans(cell)}</${tag}>`).join("")}</tr>`);
      continue;
    }

    if (raw.trim() === "") { closeBlocks(); continue; }

    closeBlocks();
    html.push(`<p>${inlineSpans(raw)}</p>`);
  }

  closeBlocks();
  if (fence !== null) html.push("</code></pre>");
  return html.join("\n");
}

/* ---------------------------------------------------------------- render */
function stage() { return document.querySelector(".stage"); }

function startSession() {
  stage().classList.add("started");
  $("hero").style.display = "none";
}

function addUser(text) {
  startSession();
  const node = $("t-user").content.firstElementChild.cloneNode(true);
  node.querySelector(".content").textContent = text;
  $("transcript").appendChild(node);
  scroll();
  return node;
}

function addAssistant(agent) {
  const who = agent || { id: "main", name: "XLI", colour: "" };
  const node = $("t-assistant").content.firstElementChild.cloneNode(true);
  const name = node.querySelector(".who-name");
  name.textContent = who.name;
  if (who.colour) {
    node.querySelector(".avatar").style.color = who.colour;
    name.style.color = who.colour;
    node.classList.add("delegate");
  }
  node.querySelector(".content").innerHTML = "";
  $("transcript").appendChild(node);
  state.assistantRow = node.querySelector(".content");
  state.assistantAgent = who.id;
  scroll();
  return node;
}

function appendAssistant(text, params) {
  const agent = agentOf(params);
  // A sub-agent speaking must not be welded onto the main agent's sentence:
  // the whole point of a distinct colour is that the voice changed.
  if (!state.assistantRow || state.assistantAgent !== agent.id) {
    state.assistantRow = null;
    addAssistant(agent);
  }
  state.assistantRow.innerHTML += renderMarkdown(text);
  scroll();
}

function addTool(name, args) {
  const node = $("t-tool").content.firstElementChild.cloneNode(true);
  node.querySelector(".glyph").textContent = glyph(name);
  node.querySelector(".tool-name").textContent = name || "?";
  node.querySelector(".tool-args").textContent = argSummary(args);
  node.querySelector(".tool-body").textContent = JSON.stringify(args || {}, null, 2);
  $("transcript").appendChild(node);
  state.toolRow = node;
  state.assistantRow = null;
  scroll();
  return node;
}

function finishTool(payload) {
  const node = state.toolRow;
  if (!node) return;
  const mark = payload.ok ? "✓" : "✗";
  const result = node.querySelector(".tool-result");
  result.textContent = `${mark} ${payload.summary || ""}`.slice(0, 120);
  result.classList.add(payload.ok ? "ok" : "fail");
  if (!payload.ok) node.classList.add("fail");
  const body = node.querySelector(".tool-body");
  if (payload.summary) {
    body.textContent = `${body.textContent}\n\n${payload.summary}`;
    body.hidden = false;
  }
}

function addDelegate(name, task, hint) {
  const colour = agentColor(name, hint);
  const node = document.createElement("div");
  node.className = "delegate-row";
  node.innerHTML = `<span class="glyph" style="color:${colour}">◆</span>`
    + `<span class="label">делегирую</span>`
    + `<span class="who" style="color:${colour}">${escapeHtml(name)}</span>`
    + (task ? `<span class="what">${escapeHtml(task)}</span>` : "");
  $("transcript").appendChild(node);
  state.toolRow = null;
  state.assistantRow = null;
  scroll();
  return node;
}

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function addNote(text, kind) {
  const node = document.createElement("div");
  node.className = `note ${kind || ""}`;
  node.textContent = text;
  $("transcript").appendChild(node);
  scroll();
}

function endTurn(result) {
  const seconds = result && result.seconds ? result.seconds : elapsed();
  const ok = !!(result && result.ok);
  const words = { done: "готово", max_steps: "кончились шаги", error: "ошибка", cancelled: "отменено" };
  const node = document.createElement("div");
  node.className = `turn-end ${ok ? "ok" : "bad"}`;
  node.textContent = `  ${ok ? "✓" : "✗"} ${words[result && result.stopped_reason] || "готово"} · `
    + `${duration(seconds)} · ${result ? result.steps : 0} ${plural(result ? result.steps : 0, "шаг", "шага", "шагов")} · `
    + `${result ? result.tool_calls : 0} ${plural(result ? result.tool_calls : 0, "вызов", "вызова", "вызовов")}`;
  $("transcript").appendChild(node);
  state.assistantRow = null;
  state.toolRow = null;
  scroll();
}

function scroll() {
  window.requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }));
}

/* ----------------------------------------------------------------- chips */
function flash(element) {
  element.classList.remove("flash");
  void element.offsetWidth;
  element.classList.add("flash");
}

function setStats() {
  $("st-step").textContent = state.maxSteps ? `${state.steps}/${state.maxSteps}` : "—";
  $("st-tools").textContent = String(state.tools);
  $("st-errors").textContent = String(state.errors);
  $("st-time").textContent = duration(state.busy ? elapsed() : (state.totalSeconds || 0));
  $("sb-steps").textContent = `${state.steps}/${state.maxSteps || 0}`;
  $("sb-tools").textContent = String(state.tools);
  $("sb-errors").textContent = String(state.errors);
  $("sb-time").textContent = ru(state.busy ? elapsed() : (state.totalSeconds || 0));
  const roster = $("st-agents");
  if (roster) roster.textContent = state.agents.length
    ? state.agents.map((a) => a.name).join(", ")
    : "—";
  const ratio = state.maxSteps ? Math.min(1, state.steps / state.maxSteps) : (state.busy ? 0.08 : 0);
  $("meter").style.width = `${Math.round(ratio * 100)}%`;
}

function setActivity(text, kind) {
  const bar = $("sb-activity");
  bar.className = `seg ${kind || ""}`;
  $("sb-activity-text").textContent = text;
  $("activity").hidden = kind !== "busy";
  if (kind === "busy") $("activity-text").textContent = text;
}

function tick() {
  if (!state.busy) return;
  state.frame += 1;
  $("spinner").textContent = SPINNER[state.frame % SPINNER.length];
  $("sb-activity").querySelector("i").title = `${state.frame}`;
  setStats();
  state.timer = window.setTimeout(tick, 110);
}

function startWork() {
  state.busy = true;
  state.startedAt = performance.now();
  state.steps = 0;
  state.maxSteps = 0;
  state.tools = 0;
  state.errors = 0;
  $("send").disabled = true;
  setActivity("работаю", "busy");
  tick();
}

function stopWork() {
  state.busy = false;
  state.totalSeconds = elapsed();
  window.clearTimeout(state.timer);
  $("send").disabled = false;
  setStats();
}

/* ------------------------------------------------------------ transport */
let nextId = 1;

async function rpc(method, params) {
  const id = nextId++;
  const response = await fetch("/rpc", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id, method, params: params || {} }),
  });
  if (!response.ok) throw new Error(`ядро ответило ${response.status}`);
  const frame = await response.json();
  if (frame && frame.error) throw new Error(frame.error.message || "ошибка ядра");
  return frame ? frame.result : null;
}

function connectEvents() {
  const stream = new EventSource("/events");
  const seg = $("sb-events");

  stream.onopen = () => { seg.textContent = "живой"; };
  stream.onerror = () => { seg.textContent = "переподключаюсь"; };
  stream.onmessage = (event) => {
    state.events += 1;
    seg.textContent = `живой (${state.events})`;
    let frame;
    try { frame = JSON.parse(event.data); } catch { return; }
    handleNotification(frame.method, frame.params || {});
  };
  return stream;
}

function handleNotification(method, params) {
  switch (method) {
    case "agent.assistant":
      if (params.text && params.text.trim()) appendAssistant(params.text, params);
      break;
    case "agent.tool_call":
      state.tools += 1;
      if (params.name === "delegate") {
        addDelegate((params.args || {}).agent_name || "?", (params.args || {}).task || "",
          params.agent_hint);
        break;
      }
      addTool(params.name, params.args);
      setActivity(`работаю · ${params.name || ""}`, "busy");
      setStats();
      break;
    case "agent.tool_result":
      finishTool(params);
      if (!params.ok) state.errors += 1;
      setStats();
      break;
    case "agent.step":
      state.steps = params.index || state.steps;
      state.maxSteps = params.max_steps || state.maxSteps;
      setStats();
      break;
    case "agent.step_done":
      break;
    case "agent.repair":
      addNote(`  ⟳ поправка: ${params.detail || ""}`, "warn");
      break;
    case "agent.warning":
      addNote(`  ! ${params.message || ""}`, "warn");
      break;
    case "agent.error":
      state.errors += 1;
      addNote(`  ✗ ${params.message || ""}`, "bad");
      setStats();
      break;
    case "agent.agent": {
      const agent = agentOf(params);
      if (agent.id === "main") {
        if (params.phase === "end") setActivity("заканчиваю", "busy");
        break;
      }
      if (params.phase === "start") {
        if (!state.agents.some((a) => a.id === agent.id)) state.agents.push(agent);
        addNote(`  ◆ ${agent.name} подключился — ${params.task || ""}`.trim(), "agent");
      } else {
        const steps = `${params.steps || 0} ${plural(params.steps || 0, "шаг", "шага", "шагов")}`;
        const reason = params.stopped_reason === "done" ? "готово" : (params.stopped_reason || "");
        addNote(`  ◇ ${agent.name} закончил · ${steps} · ${duration(params.seconds || 0)} · ${reason}`,
          "agent");
      }
      setStats();
      break;
    }
    default:
      break;
  }
}

/* ---------------------------------------------------------------- actions */
async function send(task) {
  const text = (task || $("input").value || "").trim();
  if (!text || state.busy) return;

  state.lastTask = text;
  state.agents = [];
  $("input").value = "";
  autosize();
  addUser(text);
  startWork();

  try {
    const result = await rpc("agent.run", { task: text });
    stopWork();
    if (result && result.summary) appendAssistant(result.summary, { agent_id: "main" });
    endTurn(result || {});
    setActivity(result && result.ok ? "готов" : "ошибка", result && result.ok ? "ok" : "bad");
  } catch (error) {
    stopWork();
    addNote(`  ✗ ${error.message}`, "bad");
    endTurn({ ok: false, stopped_reason: "error", seconds: elapsed(), steps: state.steps, tool_calls: state.tools });
    setActivity("ошибка", "bad");
  }
}

/* ------------------------------------------------------------------- boot */
async function boot() {
  const pill = $("pill-kernel");
  try {
    const hello = await rpc("hello", {
      protocol_version: 1,
      client: "web",
      capabilities: ["tools", "agent", "config", "kernel"],
    });
    pill.classList.add("on");
    $("pill-kernel-text").textContent = `ядро ${hello.implementation || "xli"}`;
    $("sb-kernel").textContent = `ядро: ${hello.implementation || "xli"} · протокол ${hello.protocol_version}`;
  } catch (error) {
    pill.classList.add("off");
    $("pill-kernel-text").textContent = "нет связи";
    addNote(`  ✗ ядро не отвечает: ${error.message}`, "bad");
  }

  try {
    const catalogue = await rpc("tools.list", {});
    const tools = (catalogue && catalogue.tools) || [];
    $("pill-tools-text").textContent = `${tools.length} ${plural(tools.length, "инструмент", "инструмента", "инструментов")}`;
    const list = $("tool-list");
    list.innerHTML = "";
    for (const tool of tools) {
      const item = document.createElement("li");
      item.innerHTML = `<b>${glyph(tool.name)}</b> ${escapeHtml(tool.name)}`;
      item.title = tool.description || "";
      list.appendChild(item);
    }
  } catch { /* the pill stays as it is; the transcript already says why */ }

  try {
    const payload = await rpc("mcp.servers", {});
    const all = (payload && payload.servers) || {};
    const count = Array.isArray(all) ? all.length : Object.keys(all).length;
    $("pill-mcp-text").textContent = `${count} MCP`;
  } catch {
    $("pill-mcp-text").textContent = "MCP: —";
  }

  connectEvents();
  setStats();
  setActivity("готов", "ok");
  $("input").focus();
}

/* -------------------------------------------------------------- DOM wiring */
function autosize() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, window.innerHeight * 0.4)}px`;
}

function initTheme() {
  const saved = localStorage.getItem("xli-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  $("theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("xli-theme", next);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  $("input").addEventListener("input", autosize);
  $("input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(); }
    if (event.key === "Escape") { $("input").value = ""; autosize(); }
    if (event.key === "ArrowUp" && !$("input").value && state.lastTask) { $("input").value = state.lastTask; autosize(); }
  });
  $("send").addEventListener("click", () => send());
  for (const chip of document.querySelectorAll(".chip")) {
    chip.addEventListener("click", () => send(chip.dataset.task));
  }
  boot();
});
