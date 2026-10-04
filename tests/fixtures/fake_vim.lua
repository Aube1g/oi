-- A fake enough Neovim for the XLI plugin tests.
--
-- Not an emulator: it stores buffers, windows, highlights and command
-- definitions in Lua tables so a test can assert on them. Anything the plugin
-- calls must exist here, otherwise the test fails with a plain "attempt to
-- index a nil value" — which is exactly what loading the plugin on a Neovim
-- that lacks the API would do, so the failure mode matches reality.

_G.__state = {
  bufs = {},
  next_buf = 1,
  next_win = 1,
  windows = {},
  commands = {},
  highlights = {},
  keymaps = {},
  notifications = {},
  schedules = {},
}

-- ---------------------------------------------------------------- helpers
local function ensure(tbl, key)
  -- rawget/rawset, not tbl[key]: this runs as an __index metamethod, so a
  -- plain read would call itself forever.
  local value = rawget(tbl, key)
  if value == nil then
    value = {}
    rawset(tbl, key, value)
  end
  return value
end

local function deepcopy(value)
  if type(value) ~= "table" then return value end
  local copy = {}
  for key, item in pairs(value) do
    copy[key] = deepcopy(item)
  end
  return copy
end

local function tbl_extend(behaviour, ...)
  local result = {}
  local sources = { ... }
  if behaviour == "force" then
    for _, source in ipairs(sources) do
      for key, value in pairs(source or {}) do result[key] = value end
    end
    return result
  end
  -- "keep": the first definition wins, which is what ui.lua relies on.
  for _, source in ipairs(sources) do
    for key, value in pairs(source or {}) do
      if result[key] == nil then result[key] = value end
      if type(result[key]) == "table" and type(value) == "table" then
        for inner, item in pairs(value) do
          if result[key][inner] == nil then result[key][inner] = item end
        end
      end
    end
  end
  return result
end

-- -------------------------------------------------------------------- vim
local vim = {
  g = {},
  env = { HOME = "/home/user", XLI_RUNTIME_DIR = "/tmp/xli-test" },
  log = { levels = { INFO = 1, WARN = 2, ERROR = 3 } },
  deepcopy = deepcopy,
  tbl_extend = tbl_extend,
  tbl_deep_extend = function(_, ...) return tbl_extend("force", ...) end,
  tbl_contains = function(list, value)
    for _, item in ipairs(list or {}) do
      if item == value then return true end
    end
    return false
  end,
  -- Plain-string splitting, like vim.split(text, sep, { plain = true }):
  -- no trailing empty element, and never a Lua pattern.
  split = function(text, sep, _)
    local parts = {}
    local start = 1
    while true do
      local index = text:find(sep, start, true)
      if not index then
        table.insert(parts, text:sub(start))
        break
      end
      table.insert(parts, text:sub(start, index - 1))
      start = index + #sep
    end
    return parts
  end,
  notify = function(message)
    table.insert(_G.__state.notifications, tostring(message))
  end,
  schedule = function(fn) table.insert(_G.__state.schedules, fn) end,
  defer_fn = function(fn) return 1 end,
  loop = {
    hrtime = function() return 0 end,
    fs_stat = function() return nil end,
    new_pipe = function()
      local pipe = { writes = {}, closed = false, read_callback = nil, path = nil }
      function pipe:connect(path, cb)
        self.path = path
        if cb then cb(nil) end
      end
      function pipe:read_start(cb) self.read_callback = cb end
      function pipe:read_stop() self.read_callback = nil end
      function pipe:write(data) table.insert(self.writes, data) end
      function pipe:is_closing() return self.closed end
      function pipe:close() self.closed = true end
      _G.__state.pipe = pipe
      return pipe
    end,
  },
  fn = {
    has = function() return 1 end,
    expand = function() return "/home/user/file.py" end,
    getpos = function() return { 0, 1, 1, 0 } end,
    line = function() return 1 end,
    filereadable = function() return 0 end,
    jobstart = function() return 1 end,
    jobstop = function() end,
    setreg = function() end,
    getcwd = function() return "/home/user/project" end,
  },
  diagnostic = { get = function() return {} end },
  json = {
    encode = function() return "{}" end,
    decode = function() return {} end,
  },
  bo = setmetatable({}, {
    __index = function(tbl, key) return ensure(tbl, key) end,
  }),
  wo = setmetatable({}, {
    __index = function(tbl, key) return ensure(tbl, key) end,
  }),
  -- Plain values, not the write-through proxy: the plugin reads these to size
  -- a window, and a table where a number belongs is not a graceful failure.
  o = { columns = 120, lines = 40 },
  keymap = {
    set = function(mode, lhs, rhs, opts)
      table.insert(_G.__state.keymaps, { mode = mode, lhs = lhs, opts = opts or {} })
    end,
  },
  api = {
    nvim_set_hl = function(ns, name, spec)
      _G.__state.highlights[name] = spec or {}
    end,
    nvim_create_buf = function()
      local id = _G.__state.next_buf
      _G.__state.next_buf = id + 1
      _G.__state.bufs[id] = { lines = { "" }, highlights = {} }
      return id
    end,
    nvim_buf_is_valid = function(buf) return _G.__state.bufs[buf] ~= nil end,
    nvim_buf_line_count = function(buf)
      local lines = _G.__state.bufs[buf].lines
      return math.max(1, #lines)
    end,
    nvim_buf_get_lines = function(buf, start, finish, _)
      local lines = _G.__state.bufs[buf].lines
      if finish == -1 then finish = #lines end
      local out = {}
      for index = start + 1, finish do
        table.insert(out, lines[index] or "")
      end
      return out
    end,
    nvim_buf_set_lines = function(buf, start, finish, _, lines)
      _G.__state.bufs[buf].lines = deepcopy(lines)
    end,
    nvim_buf_clear_namespace = function(buf, _, first, last)
      _G.__state.bufs[buf].highlights = {}
    end,
    nvim_buf_add_highlight = function(buf, _, group, row, _, _)
      table.insert(_G.__state.bufs[buf].highlights, { row = row, group = group })
    end,
    nvim_create_namespace = function(name) return 1 end,
    nvim_open_win = function(buf, enter, opts)
      local id = _G.__state.next_win
      _G.__state.next_win = id + 1
      opts = deepcopy(opts)
      opts.buf = buf
      _G.__state.windows[id] = opts
      return id
    end,
    nvim_win_is_valid = function(win) return _G.__state.windows[win] ~= nil end,
    nvim_win_close = function(win) _G.__state.windows[win] = nil end,
    nvim_win_set_config = function(win, opts)
      for key, value in pairs(opts) do _G.__state.windows[win][key] = value end
    end,
    nvim_win_set_cursor = function(win, pos)
      _G.__state.windows[win].cursor = pos
    end,
    nvim_create_user_command = function(name, fn, opts)
      _G.__state.commands[name] = { fn = fn, opts = opts or {} }
    end,
    nvim_buf_get_lines_count = function() return 1 end,
  },
  cmd = function() end,
}

vim.uv = vim.loop

-- Draining has to happen in Lua: `tbl.clear()` from Python would be a Lua
-- lookup for a function named "clear" and would return nil.
function _G.__drain()
  local pending = {}
  for _, callback in ipairs(_G.__state.schedules) do
    table.insert(pending, callback)
  end
  _G.__state.schedules = {}
  for _, callback in ipairs(pending) do
    callback()
  end
  return #pending
end

_G.vim = vim

-- require() shim: modules registered by the test harness, everything else
-- resolves to a permissive table so an unrelated require cannot explode.
_G.__modules = {}
function _G.require(name)
  local module = _G.__modules[name]
  if module == nil then
    return setmetatable({}, {
      __index = function() return function() end end,
    })
  end
  return module
end
