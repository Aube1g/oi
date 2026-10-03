-- xli.rpc — JSON-RPC 2.0 client over a unix socket.
--
-- The Neovim plugin speaks exactly the protocol the Python kernel serves, so
-- there is no Python dependency at runtime and nothing nvim-specific on the
-- server side. The same wire format is what a Go or Rust frontend would use.
--
-- Framing is newline-delimited JSON: one object per line, UTF-8.

local M = {}

local uv = vim.uv or vim.loop

local PROTOCOL_VERSION = 1

---@class xli.rpc.Client
---@field socket userdata|nil
---@field pending table<integer, function>
---@field handlers table<string, function>
---@field next_id integer
---@field buffer string
---@field connected boolean
local Client = {}
Client.__index = Client

---Create a client. Nothing is connected until :connect().
---@return xli.rpc.Client
function M.new()
  return setmetatable({
    socket = nil,
    pending = {},
    handlers = {},
    next_id = 1,
    buffer = "",
    connected = false,
  }, Client)
end

---Register a handler for server-initiated notifications.
---@param method string
---@param fn function
function Client:on(method, fn)
  self.handlers[method] = fn
end

---Open the unix socket and perform the protocol handshake.
---@param path string
---@param callback fun(err: string|nil, hello: table|nil)
function Client:connect(path, callback)
  if self.connected then
    if callback then callback(nil, nil) end
    return
  end

  local socket = assert(uv.new_pipe(), "xli: unable to create a pipe handle")
  self.socket = socket

  socket:connect(path, function(err)
    if err then
      self.connected = false
      if callback then callback("cannot reach the kernel at " .. path .. ": " .. err) end
      return
    end

    self.connected = true
    socket:read_start(function(read_err, chunk)
      if read_err then
        self:_fail(read_err)
        return
      end
      if not chunk then
        self:_fail("kernel closed the connection")
        return
      end
      self:_ingest(chunk)
    end)

    self:request("hello", {
      protocol_version = PROTOCOL_VERSION,
      client = "nvim",
      capabilities = { "tools", "agent", "config", "kernel" },
    }, callback)
  end)
end

---Send a request and invoke `callback(err, result)` when the reply lands.
---@param method string
---@param params table|nil
---@param callback fun(err: string|nil, result: any)|nil
---@return integer id
function Client:request(method, params, callback)
  local id = self.next_id
  self.next_id = id + 1

  if callback then self.pending[id] = callback end

  local frame = vim.json.encode({
    jsonrpc = "2.0",
    id = id,
    method = method,
    params = params or {},
  })

  self:_send(frame)
  return id
end

---Fire-and-forget message to the server.
---@param method string
---@param params table|nil
function Client:notify(method, params)
  self:_send(vim.json.encode({
    jsonrpc = "2.0",
    method = method,
    params = params or {},
  }))
end

function Client:close()
  if self.socket then
    self.socket:read_stop()
    if not self.socket:is_closing() then self.socket:close() end
    self.socket = nil
  end
  self.connected = false
  -- Fail everything still waiting so no caller hangs forever.
  for id, callback in pairs(self.pending) do
    callback("connection closed")
    self.pending[id] = nil
  end
end

function Client:is_connected()
  return self.connected
end

-- ---------------------------------------------------------------- internals

function Client:_send(frame)
  if not (self.socket and self.connected) then return end
  self.socket:write(frame .. "\n")
end

---Buffer until whole lines arrive; a frame may straddle any number of reads.
function Client:_ingest(chunk)
  self.buffer = self.buffer .. chunk
  while true do
    local newline = self.buffer:find("\n", 1, true)
    if not newline then break end
    local line = self.buffer:sub(1, newline - 1)
    self.buffer = self.buffer:sub(newline + 1)
    if line ~= "" then self:_dispatch(line) end
  end
end

function Client:_dispatch(line)
  local ok, frame = pcall(vim.json.decode, line)
  if not ok or type(frame) ~= "table" then return end

  -- A reply to something we asked for.
  if frame.id ~= nil and (frame.result ~= nil or frame.error ~= nil) then
    local callback = self.pending[frame.id]
    self.pending[frame.id] = nil
    if callback then
      if frame.error then
        callback(frame.error.message or "kernel error", nil)
      else
        callback(nil, frame.result)
      end
    end
    return
  end

  -- A server-initiated notification.
  if frame.method then
    local handler = self.handlers[frame.method]
    if handler then handler(frame.params or {}) end
  end
end

---A dead connection must not leave callers waiting on futures that never fire.
function Client:_fail(reason)
  self.connected = false
  for id, callback in pairs(self.pending) do
    callback(reason)
    self.pending[id] = nil
  end
end

return M
