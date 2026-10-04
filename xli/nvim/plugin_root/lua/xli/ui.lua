-- xli.ui — the floating window the agent talks into.
--
-- One scratch buffer in a floating window, with its own filetype so users can
-- attach their own syntax/conceal rules. Nothing here knows about the agent;
-- it only knows how to append styled lines, keep a live status line, and turn
-- the markdown the model writes into something that reads like a document
-- rather than like source.
--
-- The palette is XLI's: violet first, because cyan reads as "link" in almost
-- every colourscheme.

local M = {}

local HIGHLIGHTS = {
  XliTitle = { fg = "#c4a7ff", bold = true },
  XliAccent = { fg = "#a679ff", bold = true },
  XliUser = { fg = "#d7b3ff" },
  XliAssistant = { fg = "#e6e0ff" },
  XliTool = { fg = "#8ab4ff" },
  XliOk = { fg = "#9ee39e" },
  XliFail = { fg = "#ff8a8a" },
  XliWarn = { fg = "#ffd08a" },
  XliDim = { fg = "#8a86a8" },
  XliCode = { fg = "#b8e6c8" },
  XliH1 = { fg = "#c4a7ff", bold = true },
  XliH2 = { fg = "#b18cff", bold = true },
  XliH3 = { fg = "#d7d0ff" },
  XliBold = { bold = true },
  XliItalic = { italic = true },
  XliStrike = { strikethrough = true },
  XliMark = { bg = "#453a6e", fg = "#f0eaff" },
  XliRule = { fg = "#6f66a0" },
  XliBox = { fg = "#6f66a0" },
}

---@class xli.ui.Window
---@field buf integer
---@field win integer|nil
---@field ns integer namespace for our highlights
---@field entries table[] every line we have painted, {text, group}
---@field status table|nil the live line, replaced in place
local Window = {}
Window.__index = Window

function M.define_highlights()
  for name, spec in pairs(HIGHLIGHTS) do
    -- default = true keeps a user's own definition if they set one first.
    vim.api.nvim_set_hl(0, name, vim.tbl_extend("keep", spec, { default = true }))
  end
end

local function trim(text)
  return (text:gsub("^%s+", ""):gsub("%s+$", ""))
end

-- Inline markdown. Neovim highlights a range, not a span, so one line gets one
-- group: we strip the markers (a literal `**` in a chat window is noise) and
-- pick the most emphatic thing that was in the line. Priority runs from the
-- most specific marker to the least.
local function inline(line)
  local group = nil

  if line:find("`") then
    line = line:gsub("`([^`]*)`", "%1")
    group = "XliCode"
  end
  if line:find("==") then
    line = line:gsub("==(.-)==", "%1")
    group = group or "XliMark"
  end
  if line:find("%*%*") or line:find("__") then
    line = line:gsub("%*%*(.-)%*%*", "%1"):gsub("__(.-)__", "%1")
    group = group or "XliBold"
  end
  if line:find("~~") then
    line = line:gsub("~~(.-)~~", "%1")
    group = group or "XliStrike"
  end
  if line:find("%*%S") then
    line = line:gsub("%*([^%*]+)%*", "%1"):gsub("_([^_]+)_", "%1")
    group = group or "XliItalic"
  end

  -- A link keeps its label. The URL doubles the width of every citation and is
  -- already one `gx` away, which is the same trade the CLI makes.
  line = line:gsub("%[([^%]]*)%]%b()", "%1")

  return line, group
end

local function indent_of(line)
  return #(line:match("^(%s*)") or "")
end

---Turn markdown into `{text, group}` lines.
---@param text string
---@return table[]
function M.md_lines(text)
  local out = {}
  local in_code = false

  for raw in (text .. "\n"):gmatch("(.-)\n") do
    local line = raw

    local fence = line:match("^%s*```%s*(%S*)")
    if fence ~= nil then
      if in_code then
        table.insert(out, { "╰─", "XliBox" })
        in_code = false
      else
        table.insert(out, { "╭─" .. (fence ~= "" and (" " .. fence) or ""), "XliBox" })
        in_code = true
      end

    elseif in_code then
      table.insert(out, { "│ " .. line, "XliCode" })

    elseif line:match("^%s*$") then
      table.insert(out, { "", nil })

    elseif line:match("^%s*[-*_][-*_][-*_]+%s*$") then
      table.insert(out, { string.rep("─", 44), "XliRule" })

    else
      local hashes, heading = line:match("^(#+)%s+(.*)$")
      local indent, rest = line:match("^(%s*)[-*+]%s+(.*)$")
      local number_indent, digits, ordered = line:match("^(%s*)(%d+)[.)]%s+(.*)$")
      local quote = line:match("^>%s?(.*)$")

      if hashes then
        local level = math.min(#hashes, 3)
        table.insert(out, { inline(heading) or heading, "XliH" .. level })

      elseif indent then
        local body, group = inline(rest)
        table.insert(out, {
          string.rep(" ", #indent + 2) .. "• " .. body,
          group or "XliAssistant",
        })

      elseif digits then
        local body, group = inline(ordered)
        table.insert(out, {
          string.rep(" ", #number_indent) .. digits .. ". " .. body,
          group or "XliAssistant",
        })

      elseif quote then
        table.insert(out, { "▎ " .. (inline(quote)), "XliDim" })

      else
        local body, group = inline(line)
        table.insert(out, { body, group or "XliAssistant" })
      end
    end
  end

  -- Markdown always ends with a newline; drop the empty tail it produced.
  while #out > 0 and out[#out][1] == "" do
    table.remove(out)
  end

  return out
end

---Open (or reveal) the floating window.
---@param opts table|nil {width, height, border, title}
---@return xli.ui.Window
function M.open(opts)
  opts = opts or {}
  local width = opts.width or math.max(40, math.floor(vim.o.columns * 0.84))
  local height = opts.height or math.max(10, math.floor(vim.o.lines * 0.72))

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
    title = opts.title or " ◆ XLI ",
    title_pos = "center",
  })
  vim.wo[win].wrap = true

  local self = setmetatable({
    buf = buf,
    win = win,
    ns = vim.api.nvim_create_namespace("xli"),
    entries = {},
    status = nil,
  }, Window)

  -- Leave with <Esc> or q; Y copies the whole transcript, because the first
  -- thing anyone does with a good answer is paste it somewhere.
  vim.keymap.set("n", "<Esc>", function() self:close() end,
    { buffer = buf, nowait = true, silent = true })
  vim.keymap.set("n", "q", function() self:close() end,
    { buffer = buf, nowait = true, silent = true })
  vim.keymap.set("n", "Y", function()
    vim.fn.setreg("+", self:text())
    vim.notify("[xli] ответ скопирован в регистр +", vim.log.levels.INFO)
  end, { buffer = buf, silent = true, desc = "XLI: скопировать ответ" })

  return self
end

function Window:is_open()
  return self.win ~= nil and vim.api.nvim_win_is_valid(self.win)
end

-- The buffer holds `entries` followed by the optional live status line. A
-- fresh buffer starts with one empty line, which we count as nothing.
function Window:_rendered()
  local lines = {}
  for _, entry in ipairs(self.entries) do
    table.insert(lines, entry[1])
  end
  if self.status then table.insert(lines, self.status[1]) end
  if #lines == 0 then lines = { "" } end
  return lines
end

function Window:_refresh()
  if not vim.api.nvim_buf_is_valid(self.buf) then return end
  local lines = self:_rendered()
  vim.bo[self.buf].modifiable = true
  vim.api.nvim_buf_set_lines(self.buf, 0, -1, false, lines)
  vim.api.nvim_buf_clear_namespace(self.buf, self.ns, 0, -1)

  local row = 0
  for _, entry in ipairs(self.entries) do
    if entry[2] and entry[1] ~= "" then
      vim.api.nvim_buf_add_highlight(self.buf, self.ns, entry[2], row, 0, -1)
    end
    row = row + 1
  end
  if self.status and self.status[2] and self.status[1] ~= "" then
    vim.api.nvim_buf_add_highlight(self.buf, self.ns, self.status[2], row, 0, -1)
  end

  if self:is_open() then
    vim.api.nvim_win_set_cursor(self.win, { math.max(1, #lines), 0 })
  end
end

---Append lines. Accepts plain text, a list of strings, or `{text, group}`.
---@param lines any
function Window:append(lines)
  if type(lines) == "string" then lines = { lines } end
  if #lines == 1 and type(lines[1]) == "table" and lines[1][1] == nil then
    lines = lines[1]
  end

  for _, item in ipairs(lines) do
    if type(item) == "table" then
      table.insert(self.entries, { item[1] or "", item[2] })
    else
      table.insert(self.entries, { tostring(item), nil })
    end
  end
  self:_refresh()
end

---Replace the live status line (spinner, step counter, elapsed time).
function Window:set_status(text, group)
  if text == nil then
    self.status = nil
  else
    self.status = { text, group or "XliDim" }
  end
  self:_refresh()
end

function Window:clear()
  self.entries = {}
  self.status = nil
  self:_refresh()
end

---The whole transcript as text, for the clipboard or a test.
function Window:text()
  local parts = {}
  for _, entry in ipairs(self.entries) do
    table.insert(parts, entry[1])
  end
  return table.concat(parts, "\n")
end

function Window:set_title(title)
  if not self:is_open() then return end
  vim.api.nvim_win_set_config(self.win, { title = title, title_pos = "center" })
end

function Window:close()
  if self.win and vim.api.nvim_win_is_valid(self.win) then
    vim.api.nvim_win_close(self.win, true)
  end
  self.win = nil
end

return M
