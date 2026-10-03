-- xli — Neovim plugin for the XLI coding agent.
--
-- The plugin is a thin client: the agent itself runs in the XLI kernel
-- (`xli serve --unix`), and this talks to it over JSON-RPC. That split is
-- deliberate — Neovim reloads, crashes and updates; a half-finished edit from
-- the agent should not die with the editor.
--
--   require('xli').setup()
--   :Xli fix the failing tests in ./api
--   :XliTools        :XliStatus       :XliKernel

local rpc = require("xli.rpc")
local ui = require("xli.ui")

local M = {}

---@class xli.Config
---@field socket string|nil path to the kernel socket
---@field start_kernel boolean spawn `xli serve --unix` on demand
---@field auto_open boolean open the window on the first task
---@field notify boolean use vim.notify for errors
M.defaults = {
  socket = nil, -- nil -> $XLI_RUNTIME_DIR/kernel.sock or ~/.xli/run/kernel.sock
  start_kernel = true,
  auto_open = true,
  notify = true,
}

local state = {
  config = vim.deepcopy(M.defaults),
  client = nil,
  window = nil,
  kernel_job = nil,
  busy = false,
  stats = { tools = 0, errors = 0, steps = 0 },
}

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
      callback(err .. "\nstart it with: xli serve --unix " .. path)
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
    callback("could not start `xli serve` — is xli on your PATH?")
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
      callback("timed out waiting for the kernel to listen on " .. path)
      return
    end
    vim.defer_fn(poll, 50)
  end

  poll()
end

function M.shutdown()
  if state.kernel_job then
    vim.fn.jobstop(state.kernel_job)
    state.kernel_job = nil
  end
  if state.client then
    state.client:close()
    state.client = nil
  end
end

-- ------------------------------------------------------------- notifications
function M._wire_notifications(client)
  client:on("agent.assistant", function(params)
    M._say(params.text or "", "XliAssistant")
  end)

  client:on("agent.tool_call", function(params)
    state.stats.tools = state.stats.tools + 1
    local args = vim.json.encode(params.args or {})
    if #args > 120 then args = args:sub(1, 117) .. "..." end
    M._say("  -> " .. (params.name or "") .. " " .. args, "XliTool")
  end)

  client:on("agent.tool_result", function(params)
    if params.ok then
      M._say("     [ok] " .. (params.summary or ""), "XliOk")
    else
      state.stats.errors = state.stats.errors + 1
      M._say("     [FAIL] " .. (params.summary or params.error or ""), "XliFail")
    end
  end)

  client:on("agent.step", function(params)
    state.stats.steps = params.index or state.stats.steps
    M._say(string.format("-- step %s/%s", tostring(params.index), tostring(params.max_steps)), "XliDim")
  end)

  client:on("agent.repair", function(params)
    M._say("  ! repaired: " .. (params.detail or ""), "XliWarn")
  end)

  client:on("agent.warning", function(params)
    M._say("  ! " .. (params.message or ""), "XliWarn")
  end)

  client:on("agent.error", function(params)
    M._say("  x " .. (params.message or ""), "XliFail")
  end)

  client:on("agent.end", function()
    state.busy = false
  end)
end

-- ---------------------------------------------------------------------- window
function M._window()
  if state.window and state.window:is_open() then return state.window end
  state.window = ui.open()
  return state.window
end

function M._say(text, group)
  if text == nil or text == "" then return end
  -- Buffer writes must happen on the main loop; RPC callbacks do not.
  vim.schedule(function()
    local win = M._window()
    for _, line in ipairs(vim.split(tostring(text), "\n", { plain = true })) do
      win:append(line, group)
    end
  end)
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
    M._error("usage: :Xli <task>")
    return
  end

  if state.config.auto_open then M._window() end

  if state.busy then
    M._error("the agent is already working — wait for it to finish")
    return
  end

  state.busy = true
  M._say("you  " .. task, "XliUser")

  M.ensure_kernel(function(err)
    if err then
      state.busy = false
      M._error(err)
      return
    end

    state.client:request("agent.run", { task = task }, function(run_err, result)
      state.busy = false
      if run_err then
        M._error(run_err)
        return
      end
      local label = result and result.stopped_reason or "?"
      local summary = result and result.summary or ""
      M._say(string.format("[%s] %s", label, summary), result and result.ok and "XliOk" or "XliFail")
    end)
  end)
end

---Run a task built from the current visual selection.
function M.run_visual()
  local start_pos = vim.fn.getpos("'<")
  local end_pos = vim.fn.getpos("'>")
  local lines = vim.api.nvim_buf_get_lines(0, start_pos[2] - 1, end_pos[2], false)
  if #lines == 0 then
    M._error("select some text first")
    return
  end
  local file = vim.fn.expand("%:p")
  M.run(string.format(
    "Look at this code from %s (lines %d-%d) and improve it:\n%s",
    file, start_pos[2], end_pos[2], table.concat(lines, "\n")
  ))
end

---Send the diagnostic under the cursor to the agent.
function M.run_diagnostics()
  local diags = vim.diagnostic.get(0, { lnum = vim.fn.line(".") - 1 })
  if #diags == 0 then
    M._error("no diagnostics on this line")
    return
  end
  local parts = {}
  for _, diag in ipairs(diags) do
    table.insert(parts, string.format("line %d: %s", diag.lnum + 1, diag.message))
  end
  M.run("Fix these problems in " .. vim.fn.expand("%:p") .. ":\n" .. table.concat(parts, "\n"))
end

---Show the tool catalogue in the floating window.
function M.tools()
  M._window()
  M.ensure_kernel(function(err)
    if err then M._error(err) return end
    state.client:request("agent.tools", {}, function(call_err, result)
      if call_err then M._error(call_err) return end
      for _, spec in ipairs(result.tools or {}) do
        M._say(string.format("%-10s %s", spec.name, spec.description or ""), "XliTool")
      end
    end)
  end)
end

---Show kernel and connection status.
function M.status()
  M._window()
  local path = M._socket_path()
  local connected = state.client and state.client:is_connected() or false
  M._say(string.format("socket    %s", path), "XliDim")
  M._say(string.format("connected %s", tostring(connected)), connected and "XliOk" or "XliFail")
  M._say(string.format("tools %d · errors %d · steps %d",
    state.stats.tools, state.stats.errors, state.stats.steps), "XliDim")

  M.ensure_kernel(function(err)
    if err then M._error(err) return end
    state.client:request("kernel.status", {}, function(call_err, result)
      if call_err then M._error(call_err) return end
      M._say(string.format("kernel    %d/%d compiled", result.compiled or 0, result.total or 0), "XliTitle")
    end)
    state.client:request("doctor", {}, function(call_err, result)
      if call_err or not result then return end
      for _, check in ipairs(result.checks or {}) do
        M._say(string.format("  [%s] %-22s %s",
          check.ok and "ok" or "--", check.name, check.detail or ""),
          check.ok and "XliDim" or "XliWarn")
      end
    end)
  end)
end

---Compile the Cython kernel and report what happened.
function M.kernel_build()
  M._window()
  M._say("building the kernel...", "XliDim")
  M.ensure_kernel(function(err)
    if err then M._error(err) return end
    state.client:request("kernel.build", {}, function(call_err, result)
      if call_err then M._error(call_err) return end
      if result.ok then
        M._say("built: " .. table.concat(result.built or {}, ", "), "XliOk")
      else
        for _, failure in ipairs(result.failed or {}) do
          M._say(string.format("FAILED %s: %s", failure.module, failure.error), "XliFail")
        end
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

  vim.api.nvim_create_user_command("Xli", function(opts)
    M.run(table.concat(opts.fargs, " "))
  end, { nargs = "+", desc = "Run an XLI task" })

  vim.api.nvim_create_user_command("XliTools", function() M.tools() end,
    { desc = "List XLI tools" })

  vim.api.nvim_create_user_command("XliStatus", function() M.status() end,
    { desc = "XLI connection and kernel status" })

  vim.api.nvim_create_user_command("XliKernel", function() M.kernel_build() end,
    { desc = "Compile the XLI Cython kernel" })

  vim.api.nvim_create_user_command("XliClose", function() M.close() end,
    { desc = "Close the XLI window" })

  vim.api.nvim_create_user_command("XliSelection", function() M.run_visual() end,
    { range = true, desc = "Send the selection to XLI" })

  vim.api.nvim_create_user_command("XliDiagnostics", function() M.run_diagnostics() end,
    { desc = "Send this line's diagnostics to XLI" })
end

return M
