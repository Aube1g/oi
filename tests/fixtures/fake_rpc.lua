
---A fake `xli.rpc` module: records what the plugin asks for and lets a test
---play the kernel back, event by event.
local M = {}

function M.new()
  local client = {
    handlers = {},
    requests = {},
    pending = nil,
    connected = true,
  }

  function client:is_connected() return self.connected end
  function client:connect(path, cb) if cb then cb(nil, {}) end end
  function client:on(method, fn) self.handlers[method] = fn end
  function client:close() self.connected = false end
  function client:request(method, params, cb)
    table.insert(self.requests, { method = method, params = params })
    self.pending = cb
    return #self.requests
  end

  _G.__state.rpc_client = client
  return client
end

return M
