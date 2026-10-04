-- xli — Neovim plugin for the XLI coding agent.
--
-- The plugin is a thin client: the agent itself runs in the XLI kernel
-- (`xli serve --unix`), and this talks to it over JSON-RPC. That split is
-- deliberate — Neovim reloads, crashes and updates; a half-finished edit from
-- the agent should not die with the editor.
--
--   require('xli').setup()
--   :Xli почини падающие тесты в ./api
--   :XliTools   :XliGraph   :XliStatus   :XliKernel   :XliDoctor
--
-- Everything the user reads is Russian, like the rest of XLI: the interface is
-- the product, and mixing languages in a chat window looks unfinished.

local rpc = require("xli.rpc")
local ui = require("xli.ui")

local M = {}

---@class xli.Config
---@field socket string|nil path to the kernel socket
---@field start_kernel boolean spawn `xli serve --unix` on demand
---@field auto_open boolean open the window on the first task
---@field notify boolean use vim.notify for errors
---@field spinner boolean animate the status line while the agent works
M.defaults = {
  socket = nil, -- nil -> $XLI_RUNTIME_DIR/kernel.sock or ~/.xli/run/kernel.sock
  start_kernel = true,
  auto_open = true,
  notify = true,
  spinner = true,
}

local SPINNER = { "⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷" }

-- Mirrors xli/tui/palette.py: the glyph is how you recognise a call at a
-- glance without reading the rest of the line.
local GLYPH = {
  read = "◇", write = "◆", edit = "✎", apply_patch = "✎", bash = "❯",
  grep = "⌕", glob = "✦", ls = "▤", outline = "❖", repo_map = "▦",
  dep_graph = "⛓", chart = "▁", delegate = "◆", git = "⁝", todo = "☐",
  think = "∴", web_search = "⌾", fetch_page = "⇩", fetch_pages = "⇩",
  mcp_call = "⚙", mcp_list = "⚙", json_query = "{}", search_status = "⌕",
}

local state = {
  config = vim.deepcopy(M.defaults),
  client = nil,
  window = nil,
  kernel_job = nil,
  busy = false,
  last_task = nil,
  started_at = nil,
  frame = 0,
  timer = nil,
  step = { index = 0, max = 0 },
  stats = { tools = 0, errors = 0, steps = 0 },
}

-- -------------------------------------------------------------------- helpers
local function ru_number(value, digits)
  local text = string.format("%." .. (digits or 1) .. "f", value or 0)
  return (text:gsub("%.", ","))
end

local function ru_duration(seconds)
  seconds = seconds or 0
  if seconds < 60 then return ru_number(seconds) .. " с" end
  local minutes = math.floor(seconds / 60)
  return string.format("%d мин %02d с", minutes, math.floor(seconds % 60))
end

local function plural(count, one, few, many)
  local n = math.abs(math.floor(count or 0)) % 100
  if n >= 11 and n <= 14 then return many end
  n = n % 10
  if n == 1 then return one end
  if n >= 2 and n <= 4 then return few end
  return many
end

local function glyph(name)
  return GLYPH[name or ""] or "◆"
end

-- A tool call reads as a sentence, not as JSON. This is the same shape the
-- CLI's summary layer produces, cut down to the arguments that identify the
-- call: path, query, command.
local PRIORITY = { "path", "file", "file_path", "target", "query", "pattern",
  "command", "task", "name", "module", "symbol", "url" }

local function call_line(name, args)
  local parts = {}
  for _, key in ipairs(PRIORITY) do
    local value = args and args[key]
    if type(value) == "string" and value ~= "" then
      table.insert(parts, value)
      break
    end
  end
  if #parts == 0 and type(args) == "table" then
    for key, value in pairs(args) do
      if type(value) == "string" and value ~= "" then
        table.insert(parts, key .. "=" .. value)
        break
      end
    end
  end
  local suffix = parts[1] and ("  " .. parts[1]) or ""
  if #suffix > 90 then suffix = suffix:sub(1, 87) .. "…" end
  return glyph(name) .. " " .. (name or "?") .. suffix
end

-- --------------------------------------------------------------------- setup
---Configure the plugin. Safe to call more than once.
---@param opts table|nil
function M.setup(opts)
  state.config = vim.tbl_deep_extend("force", M.defaults, opts or {})
  ui.define_highlights()
  M._register_commands()
end

function M._socket_path()
  if state.config.socket then return state.config.socket end
  if vim.env.XLI_SOCKET then return vim.env.XLI_SOCKET end

  local runtime = vim.env.XLI_RUNTIME_DIR
  if not runtime or runtime == "" then
    runtime = (vim.env.HOME or vim.fn.expand("~")) .. "/.xli/run"
  end
  return runtime .. "/kernel.sock"
end

-- -------------------------------------------------------------------- kernel
---Start the kernel if it is not already listening. Idempotent.
---@param callback fun(err: string|nil)
function M.ensure_kernel(callback)
  local path = M._socket_path()

  -- A socket file can outlive the process that made it; connecting is the only
  -- reliable liveness check.
  if state.client and state.client:is_connected() then
    callback(nil)
    return
  end

  state.client = rpc.new()
  M._wire_notifications(state.client)

  state.client:connect(path, function(err)
    if not err then
      callback(nil)
      return
    end

    if not state.config.start_kernel then
      callback("ядро не отвечает на " .. path .. "\nзапустите: xli serve --unix " .. path)
      return
    end

    M._spawn_kernel(path, function(spawn_err)
      if spawn_err then
        callback(spawn_err)
        return
      end
      -- Retry the handshake now that something is listening.
      state.client = rpc.new()
      M._wire_notifications(state.client)
      state.client:connect(path, callback)
    end)
  end)
end

function M._spawn_kernel(path, callback)
  local attempts = 0

  local job = vim.fn.jobstart({ "xli", "serve", "--unix", path }, {
    detach = true,
    on_exit = function() state.kernel_job = nil end,
  })

  if job <= 0 then
    callback("не удалось запустить `xli serve` — xli есть в PATH?")
    return
  end

  state.kernel_job = job

  -- Poll for the socket rather than sleeping a fixed amount: a cold start and
  -- a warm one differ by an order of magnitude.
  local function poll()
    attempts = attempts + 1
    if vim.fn.filereadable(path) == 1 or vim.loop.fs_stat(path) then
      callback(nil)
      return
    end
    if attempts > 100 then
      callback("ядро не поднялось: " .. path .. " не появился")
      return
    end
    vim.defer_fn(poll, 50)
  end

  poll()
end

function M.shutdown()
  M._stop_spinner()
  if state.kernel_job then
    vim.fn.jobstop(state.kernel_job)
    state.kernel_job = nil
  end
  if state.client then
    state.client:close()
    state.client = nil
  end
end

-- -------------------------------------------------------------------- spinner
function M._status_text()
  if not state.busy then
    if state.stats.tools == 0 and state.stats.steps == 0 then return nil end
    return string.format("● готово: %d %s, %d %s",
      state.stats.steps, plural(state.stats.steps, "шаг", "шага", "шагов"),
      state.stats.tools, plural(state.stats.tools, "инструмент", "инструмента", "инструментов"))
  end

  local seconds = state.started_at and (vim.loop.hrtime() - state.started_at) / 1e9 or 0
  local parts = { SPINNER[state.frame % #SPINNER + 1] .. " работаю" }
  if state.step.max > 0 then
    table.insert(parts, string.format("шаг %d/%d", state.step.index, state.step.max))
  end
  table.insert(parts, string.format("%d %s",
    state.stats.tools, plural(state.stats.tools, "инструмент", "инструмента", "инструментов")))
  if state.stats.errors > 0 then
    table.insert(parts, string.format("ошибок %d", state.stats.errors))
  end
  table.insert(parts, ru_duration(seconds))
  return "⣿ " .. table.concat(parts, " · ")
end

function M._paint_status()
  local text = M._status_text()
  local win = state.window
  if win and win:is_open() then
    win:set_status(text)
    if state.busy then
      local seconds = state.started_at and (vim.loop.hrtime() - state.started_at) / 1e9 or 0
      win:set_title(string.format(" ⣽ XLI · работаю %s ", ru_duration(seconds)))
    end
  end
  vim.g.xli_status = text or ""
end

function M._start_spinner()
  if not state.config.spinner or state.timer then return end
  state.frame = 0
  local function tick()
    state.frame = state.frame + 1
    M._paint_status()
    if state.busy then
      state.timer = vim.defer_fn(tick, 110)
    else
      state.timer = nil
    end
  end
  state.timer = vim.defer_fn(tick, 110)
end

function M._stop_spinner()
  state.timer = nil
end

---A one-line summary for a statusline plugin (lualine, heirline, …).
function M.statusline()
  if state.busy then
    return string.format("◆ XLI ⣽ %d/%d", state.step.index, state.stats.tools)
  end
  return "◆ XLI"
end

-- ------------------------------------------------------------- notifications
function M._wire_notifications(client)
  client:on("agent.assistant", function(params)
    local text = params.text or ""
    if text:gsub("%s", "") == "" then return end
    -- The model writes markdown; the window shows a document, not asterisks.
    M._push(ui.md_lines(text))
  end)

  client:on("agent.tool_call", function(params)
    state.stats.tools = state.stats.tools + 1
    M._push({ { call_line(params.name, params.args), "XliTool" } })
    M._paint_status()
  end)

  client:on("agent.tool_result", function(params)
    local summary = params.summary or params.error or ""
    if #summary > 200 then summary = summary:sub(1, 197) .. "…" end
    if params.ok then
      M._push({ { "  ✓ " .. summary, "XliDim" } })
    else
      state.stats.errors = state.stats.errors + 1
      M._push({ { "  ✗ " .. summary, "XliFail" } })
    end
    M._paint_status()
  end)

  client:on("agent.step", function(params)
    state.stats.steps = params.index or state.stats.steps
    state.step.index = params.index or state.step.index
    state.step.max = params.max_steps or state.step.max
    M._paint_status()
  end)

  client:on("agent.repair", function(params)
    M._push({ { "  ⟳ поправка: " .. (params.detail or ""), "XliWarn" } })
  end)

  client:on("agent.warning", function(params)
    M._push({ { "  ! " .. (params.message or ""), "XliWarn" } })
  end)

  client:on("agent.error", function(params)
    state.stats.errors = state.stats.errors + 1
    M._push({ { "  ✗ " .. (params.message or ""), "XliFail" } })
    M._paint_status()
  end)

  -- The kernel announces itself as `agent.agent` with a phase, not as
  -- `agent.end`: that event never existed, and listening for it meant the
  -- spinner kept turning until the RPC reply arrived.
  client:on("agent.agent", function(params)
    if params.phase == "end" then
      state.busy = false
      M._stop_spinner()
      M._paint_status()
    elseif params.phase == "start" then
      state.busy = true
    end
  end)

  client:on("agent.step_done", function(params)
    local seconds = tonumber(params.seconds) or 0
    M._push({ { string.format("  · шаг %s · %s",
      tostring(params.index or "?"), ru_duration(seconds)), "XliDim" } })
    M._paint_status()
  end)
end

-- ---------------------------------------------------------------------- window
function M._window()
  if state.window and state.window:is_open() then return state.window end
  state.window = ui.open({ title = " ◆ XLI " })
  return state.window
end

function M._push(entries)
  -- Buffer writes must happen on the main loop; RPC callbacks do not.
  vim.schedule(function()
    M._window():append(entries)
  end)
end

function M._say(text, group)
  if text == nil or text == "" then return end
  local lines = {}
  for _, line in ipairs(vim.split(tostring(text), "\n", { plain = true })) do
    table.insert(lines, { line, group })
  end
  M._push(lines)
end

function M._error(message)
  if state.config.notify then
    vim.notify("[xli] " .. tostring(message), vim.log.levels.ERROR)
  else
    M._say("[xli] " .. tostring(message), "XliFail")
  end
end

-- -------------------------------------------------------------------- actions
---Run a task. Called by :Xli <task>.
---@param task string
function M.run(task)
  if not task or task == "" then
    M._error("укажите задачу: :Xli <что сделать>")
    return
  end

  if state.config.auto_open then M._window() end

  if state.busy then
    M._error("агент уже работает — дождитесь ответа")
    return
  end

  state.busy = true
  state.last_task = task
  state.started_at = vim.loop.hrtime()
  state.step = { index = 0, max = 0 }
  state.stats = { tools = 0, errors = 0, steps = 0 }

  M._say("▸ вы  " .. task, "XliUser")
  M._push({ { "", nil } })
  M._start_spinner()
  M._paint_status()

  M.ensure_kernel(function(err)
    if err then
      state.busy = false
      M._stop_spinner()
      M._paint_status()
      M._error(err)
      return
    end

    state.client:request("agent.run", { task = task }, function(run_err, result)
      state.busy = false
      M._stop_spinner()
      if run_err then
        M._paint_status()
        M._error(run_err)
        return
      end

      local seconds = state.started_at and (vim.loop.hrtime() - state.started_at) / 1e9 or 0
      local reason = result and result.stopped_reason or "?"
      local words = {
        done = "готово", max_steps = "кончились шаги",
        error = "ошибка", cancelled = "отменено",
      }
      local tail = string.format("  %s %s · %s · %s",
        (result and result.ok) and "✓" or "✗",
        words[reason] or reason,
        ru_duration(seconds),
        string.format("%d %s, %d %s",
          (result and result.steps) or 0,
          plural((result and result.steps) or 0, "шаг", "шага", "шагов"),
          (result and result.tool_calls) or 0,
          plural((result and result.tool_calls) or 0, "вызов", "вызова", "вызовов")))
      M._push({ { tail, (result and result.ok) and "XliOk" or "XliFail" } })

      if result and result.summary and result.summary ~= "" then
        M._push(ui.md_lines(result.summary))
      end

      local title = string.format(" ◆ XLI · %s ",
        (result and result.ok) and "готово" or "ошибка")
      vim.schedule(function()
        if state.window and state.window:is_open() then state.window:set_title(title) end
      end)
      M._paint_status()
    end)
  end)
end

---Repeat the previous task.
function M.again()
  if not state.last_task then
    M._error("ещё не было задач")
    return
  end
  M.run(state.last_task)
end

---Run a task built from the current visual selection.
function M.run_visual()
  local start_pos = vim.fn.getpos("'<")
  local end_pos = vim.fn.getpos("'>")
  local lines = vim.api.nvim_buf_get_lines(0, start_pos[2] - 1, end_pos[2], false)
  if #lines == 0 then
    M._error("сначала выделите текст")
    return
  end
  local file = vim.fn.expand("%:p")
  M.run(string.format(
    "Посмотри на код из %s (строки %d–%d) и улучши его:\n%s",
    file, start_pos[2], end_pos[2], table.concat(lines, "\n")
  ))
end

---Send the diagnostic under the cursor to the agent.
function M.run_diagnostics()
  local diags = vim.diagnostic.get(0, { lnum = vim.fn.line(".") - 1 })
  if #diags == 0 then
    M._error("на этой строке нет диагностики")
    return
  end
  local parts = {}
  for _, diag in ipairs(diags) do
    table.insert(parts, string.format("строка %d: %s", diag.lnum + 1, diag.message))
  end
  M.run("Исправь проблемы в " .. vim.fn.expand("%:p") .. ":\n" .. table.concat(parts, "\n"))
end

---Call one tool through the kernel and print what it returned.
---@param name string
---@param args table|nil
---@param title string
function M.call_tool(name, args, title)
  M._window()
  if title then M._say("◆ " .. title, "XliAccent") end
  M.ensure_kernel(function(err)
    if err then M._error(err) return end
    state.client:request("tools.run", { name = name, args = args or {} }, function(call_err, result)
      if call_err then M._error(call_err) return end
      if not result or not result.ok then
        M._say("✗ " .. tostring(result and (result.error or result.summary) or "пусто"), "XliFail")
        return
      end
      local data = result.data or {}
      local text = type(data) == "table" and data.text or data
      if type(text) == "string" and text ~= "" then
        M._push(ui.md_lines(text))
      end
      if result.summary and result.summary ~= "" then
        M._say("  " .. result.summary, "XliDim")
      end
    end)
  end)
end

---Show the tool catalogue in the floating window.
function M.tools()
  M._window()
  M.ensure_kernel(function(err)
    if err then M._error(err) return end
    state.client:request("agent.tools", {}, function(call_err, result)
      if call_err then M._error(call_err) return end
      local specs = result.tools or {}
      M._say(string.format("◆ инструменты: %d", #specs), "XliAccent")
      for _, spec in ipairs(specs) do
        M._push({ { string.format("  %s %-12s %s",
          glyph(spec.name), spec.name, spec.description or ""), "XliTool" } })
      end
    end)
  end)
end

---Show the dependency graph, optionally focused on one module.
---@param module string|nil
function M.graph(module)
  local focus = (module and module ~= "") and module or nil
  M.call_tool("dep_graph", { path = vim.fn.getcwd(), focus = focus or "" },
    focus and ("граф: " .. focus) or "граф зависимостей проекта")
end

---Compile the Cython kernel and report what happened.
function M.kernel_build()
  M._window()
  M._say("◆ сборка ядра…", "XliAccent")
  M._start_spinner()
  state.busy = true
  state.started_at = vim.loop.hrtime()
  M.ensure_kernel(function(err)
    if err then
      state.busy = false
      M._stop_spinner()
      M._error(err)
      return
    end
    state.client:request("kernel.build", {}, function(call_err, result)
      state.busy = false
      M._stop_spinner()
      M._paint_status()
      if call_err then M._error(call_err) return end
      local built = result.built or {}
      if result.ok then
        M._say(string.format("✓ собрано: %d %s", #built,
          plural(#built, "модуль", "модуля", "модулей")), "XliOk")
      else
        for _, failure in ipairs(result.failed or {}) do
          M._say(string.format("✗ %s: %s", failure.module, failure.error), "XliFail")
        end
      end
      if #built > 0 then M._say("  " .. table.concat(built, ", "), "XliDim") end
    end)
  end)
end

---Show kernel and connection status.
function M.status()
  M._window()
  local path = M._socket_path()
  local connected = state.client and state.client:is_connected() or false
  M._push({ { string.format("◆ XLI · %s", connected and "связь есть" or "нет связи"),
    connected and "XliOk" or "XliFail" } })
  M._say(string.format("  сокет %s", path), "XliDim")
  M._say(string.format("  инструментов %d · ошибок %d · шагов %d",
    state.stats.tools, state.stats.errors, state.stats.steps), "XliDim")

  M.ensure_kernel(function(err)
    if err then M._error(err) return end
    state.client:request("kernel.status", {}, function(call_err, result)
      if call_err then M._error(call_err) return end
      M._say(string.format("  ядро: собрано %d из %d",
        result.compiled or 0, result.total or 0), "XliTitle")
    end)
    state.client:request("doctor", {}, function(call_err, result)
      if call_err or not result then return end
      M._say("  окружение:", "XliTitle")
      for _, check in ipairs(result.checks or {}) do
        local mark = check.ok and "✓" or "✗"
        M._push({ { string.format("    %s %-18s %s", mark, check.name, check.detail or ""),
          check.ok and "XliDim" or "XliWarn" } })
      end
    end)
  end)
end

---List saved sessions.
function M.sessions()
  M._window()
  M.ensure_kernel(function(err)
    if err then M._error(err) return end
    state.client:request("session.list", {}, function(call_err, result)
      if call_err then M._error(call_err) return end
      local sessions = result.sessions or {}
      M._say(string.format("◆ сессии: %d", #sessions), "XliAccent")
      if #sessions == 0 then
        M._say("  пока пусто", "XliDim")
        return
      end
      for index, session in ipairs(sessions) do
        if index > 20 then
          M._say(string.format("  … и ещё %d", #sessions - 20), "XliDim")
          break
        end
        local id = tostring(session.id or session.session_id or "?")
        M._push({ { string.format("  %s %s", id:sub(1, 12),
          tostring(session.task or session.summary or "")), "XliDim" } })
      end
    end)
  end)
end

---Close the floating window.
function M.close()
  if state.window then state.window:close() end
end

-- ------------------------------------------------------------------- commands
function M._register_commands()
  if vim.g.__xli_commands_registered then return end
  vim.g.__xli_commands_registered = true

  local function command(name, fn, opts)
    opts = opts or {}
    opts.desc = "XLI: " .. (opts.desc or name)
    vim.api.nvim_create_user_command(name, fn, opts)
  end

  command("Xli", function(opts) M.run(table.concat(opts.fargs, " ")) end,
    { nargs = "+", desc = "задача агенту" })

  command("XliAgain", function() M.again() end, { desc = "повторить прошлую задачу" })
  command("XliTools", function() M.tools() end, { desc = "каталог инструментов" })
  command("XliGraph", function(opts) M.graph(opts.args) end,
    { nargs = "?", desc = "граф зависимостей проекта" })
  command("XliStatus", function() M.status() end, { desc = "связь и состояние ядра" })
  command("XliSessions", function() M.sessions() end, { desc = "сохранённые сессии" })
  command("XliKernel", function() M.kernel_build() end, { desc = "собрать Cython-ядро" })
  command("XliClose", function() M.close() end, { desc = "закрыть окно" })
  command("XliSelection", function() M.run_visual() end,
    { range = true, desc = "отправить выделение" })
  command("XliDiagnostics", function() M.run_diagnostics() end,
    { desc = "отправить диагностику строки" })
end

return M
