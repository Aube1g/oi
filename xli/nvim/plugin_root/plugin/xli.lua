-- xli — plugin entry point.
--
-- Neovim sources every file in plugin/ at startup. This one only defines the
-- highlight groups and a lazy-loading shim; `require('xli').setup()` is what
-- actually wires anything up, so a user who never calls it pays nothing.

if vim.g.loaded_xli then return end
vim.g.loaded_xli = 1

-- Needs Neovim 0.10 for vim.uv; older builds have vim.loop and still work,
-- but 0.8 and earlier lack the JSON-RPC pieces this relies on.
if vim.fn.has("nvim-0.8") == 0 then
  vim.notify("[xli] requires Neovim 0.8 or newer", vim.log.levels.ERROR)
  return
end

require("xli.ui").define_highlights()

-- Commands exist immediately so they can be referenced in a user's config
-- before setup() has run; each one calls setup() with defaults if needed.
local function lazy_setup()
  if not vim.g.__xli_commands_registered then
    require("xli").setup({})
  end
end

vim.api.nvim_create_user_command("Xli", function(opts)
  lazy_setup()
  require("xli").run(table.concat(opts.fargs, " "))
end, { nargs = "+", desc = "Run an XLI task" })

for _, spec in ipairs({
  { "XliTools", "tools" },
  { "XliStatus", "status" },
  { "XliKernel", "kernel_build" },
  { "XliClose", "close" },
  { "XliSelection", "run_visual" },
  { "XliDiagnostics", "run_diagnostics" },
}) do
  vim.api.nvim_create_user_command(spec[1], function()
    lazy_setup()
    require("xli")[spec[2]]()
  end, { desc = "XLI: " .. spec[1] })
end
