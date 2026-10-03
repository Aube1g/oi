-- xli.ui — the floating window the agent talks into.
--
-- One scratch buffer in a floating window, with its own filetype so users can
-- attach their own syntax/conceal rules. Nothing here knows about the agent;
-- it only knows how to append styled lines and scroll.

local M = {}

local HIGHLIGHTS = {
  XliTitle = { fg = "#61afef", bold = true },
  XliUser = { fg = "#c678dd" },
  XliAssistant = { fg = "#98c379" },
  XliTool = { fg = "#56b6c2" },
  XliOk = { fg = "#98c379" },
  XliFail = { fg = "#e06c75" },
  XliWarn = { fg = "#e5c07b" },
  XliDim = { fg = "#5c6370" },
}

---@class xli.ui.Window
---@field buf integer
---@field win integer|nil
local Window = {}
Window.__index = Window

function M.define_highlights()
  for name, spec in pairs(HIGHLIGHTS) do
    -- clear=false keeps a user's own definition if they set one first.
    vim.api.nvim_set_hl(0, name, vim.tbl_extend("keep", spec, { default = true }))
  end
end

---Open (or reveal) the floating window.
---@param opts table|nil {width, height, border, title}
---@return xli.ui.Window
function M.open(opts)
  opts = opts or {}
  local width = opts.width or math.floor(vim.o.columns * 0.8)
  local height = opts.height or math.floor(vim.o.lines * 0.7)

  local buf = vim.api.nvim_create_buf(false, true)
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "hide"
  vim.bo[buf].swapfile = false
  vim.bo[buf].filetype = opts.filetype or "xli"
  vim.bo[buf].modifiable = true

  local win = vim.api.nvim_open_win(buf, true, {
    relative = "editor",
    width = width,
    height = height,
    col = math.floor((vim.o.columns - width) / 2),
    row = math.floor((vim.o.lines - height) / 2),
    style = "minimal",
    border = opts.border or "rounded",
    title = opts.title or " XLI ",
    title_pos = "center",
  })

  local self = setmetatable({ buf = buf, win = win }, Window)

  -- Leave with <Esc> or q, and clean the window up properly.
  local function close() self:close() end
  vim.keymap.set("n", "<Esc>", close, { buffer = buf, nowait = true, silent = true })
  vim.keymap.set("n", "q", close, { buffer = buf, nowait = true, silent = true })

  return self
end

function Window:is_open()
  return self.win ~= nil and vim.api.nvim_win_is_valid(self.win)
end

---Append lines, optionally highlighting the whole line.
---@param lines string|string[]
---@param group string|nil highlight group name
function Window:append(lines, group)
  if not vim.api.nvim_buf_is_valid(self.buf) then return end

  if type(lines) == "string" then lines = { lines } end
  local start_row = vim.api.nvim_buf_line_count(self.buf)

  -- A fresh buffer holds one empty line; overwrite it rather than grow by one.
  if start_row == 1 and vim.api.nvim_buf_get_lines(self.buf, 0, 1, false)[1] == "" then
    start_row = 0
  end

  vim.api.nvim_buf_set_lines(self.buf, start_row, -1, false, lines)

  if group then
    local first = start_row
    local last = vim.api.nvim_buf_line_count(self.buf)
    for row = first, last - 1 do
      vim.api.nvim_buf_add_highlight(self.buf, -1, group, row, 0, -1)
    end
  end

  if self:is_open() then
    vim.api.nvim_win_set_cursor(self.win, { vim.api.nvim_buf_line_count(self.buf), 0 })
  end
end

function Window:clear()
  if not vim.api.nvim_buf_is_valid(self.buf) then return end
  vim.api.nvim_buf_set_lines(self.buf, 0, -1, false, {})
end

function Window:close()
  if self.win and vim.api.nvim_win_is_valid(self.win) then
    vim.api.nvim_win_close(self.win, true)
  end
  self.win = nil
end

return M
