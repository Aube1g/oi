-- xli — plugin entry point.
--
-- Neovim sources every file in plugin/ at startup. This one only defines the
-- highlight groups and the commands (as lazy shims); `require('xli').setup()`
-- is what actually wires anything up, so a user who never calls it pays
-- nothing but a table of command definitions.

if vim.g.loaded_xli then return end
vim.g.loaded_xli = 1

-- Needs Neovim 0.10 for vim.uv; older builds have vim.loop and still work,
-- but 0.8 and earlier lack the JSON-RPC pieces this relies on.
if vim.fn.has("nvim-0.8") == 0 then
  vim.notify("[xli] нужен Neovim 0.8 или новее", vim.log.levels.ERROR)
  return
end

require("xli.ui").define_highlights()

-- Commands exist immediately so they can be referenced in a user's config
-- before setup() has run; each one calls setup() with defaults if needed.
local function lazy()
  if not vim.g.__xli_commands_registered then
    require("xli").setup({})
  end
  return require("xli")
end

local COMMANDS = {
  { "Xli", "run", { nargs = "+", desc = "XLI: задача агенту" } },
  { "XliAgain", "again", { desc = "XLI: повторить прошлую задачу" } },
  { "XliTools", "tools", { desc = "XLI: каталог инструментов" } },
  { "XliGraph", "graph", { nargs = "?", desc = "XLI: граф зависимостей проекта" } },
  { "XliStatus", "status", { desc = "XLI: связь и состояние ядра" } },
  { "XliSessions", "sessions", { desc = "XLI: сохранённые сессии" } },
  { "XliKernel", "kernel_build", { desc = "XLI: собрать Cython-ядро" } },
  { "XliClose", "close", { desc = "XLI: закрыть окно" } },
  { "XliSelection", "run_visual", { range = true, desc = "XLI: отправить выделение" } },
  { "XliDiagnostics", "run_diagnostics", { desc = "XLI: отправить диагностику строки" } },
}

for _, spec in ipairs(COMMANDS) do
  local name, method, opts = spec[1], spec[2], spec[3] or {}
  opts.desc = opts.desc or ("XLI: " .. name)
  vim.api.nvim_create_user_command(name, function(args)
    local agent = lazy()
    local fn = agent[method]
    if type(fn) ~= "function" then return end
    if method == "run" then
      fn(table.concat(args.fargs, " "))
    elseif method == "graph" then
      fn(args.args)
    else
      fn()
    end
  end, opts)
end
