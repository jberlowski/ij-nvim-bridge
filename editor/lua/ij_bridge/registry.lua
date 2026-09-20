-- Discovery (SPEC.md §4.2).
--
-- The Brain publishes a Registry mapping each Project Root to the socket serving
-- it. The Editor resolves a buffer by longest-prefix match of its absolute path
-- against `root`. No match is Dormant, and Dormant is normal: most files a
-- developer opens are not in a Project Root, and nvim must then behave exactly
-- as if the Bridge were not installed. Nothing here may raise, print or block.
local uv = vim.uv

local M = {}

--- $XDG_RUNTIME_DIR is unset on WSL without systemd; the fallback is not optional.
function M.dir()
  local runtime = vim.env.XDG_RUNTIME_DIR
  if runtime and runtime ~= '' then
    return runtime .. '/ij-nvim-bridge'
  end
  return (vim.env.HOME or '') .. '/.ij-nvim-bridge'
end

local function read(path)
  local fd = uv.fs_open(path, 'r', 438)
  if not fd then
    return nil
  end
  local stat = uv.fs_fstat(fd)
  local data = stat and uv.fs_read(fd, stat.size, 0)
  uv.fs_close(fd)
  return data
end

local function alive(pid)
  -- uv.kill with signal 0 only asks whether the process exists.
  return type(pid) == 'number' and uv.kill(pid, 0) == 0
end

--- Registry entries whose process is alive and whose socket file exists.
--- A stale entry is skipped rather than reported: the Brain that wrote it is gone.
function M.brains()
  local dir = M.dir()
  local raw = read(dir .. '/registry.json')
  if not raw then
    return {}
  end
  local ok, doc = pcall(vim.json.decode, raw)
  if not ok or type(doc) ~= 'table' or type(doc.brains) ~= 'table' then
    return {} -- torn or foreign file: treat as no Brain, never as an error
  end
  local live = {}
  for _, entry in ipairs(doc.brains) do
    local sock = dir .. '/' .. tostring(entry.sock)
    if alive(entry.pid) and uv.fs_stat(sock) then
      table.insert(live, { root = entry.root, sock = sock, pid = entry.pid, ide = entry.ide })
    end
  end
  return live
end

--- Longest-prefix match of an absolute path against the live Project Roots.
--- @return table|nil entry  nil means Dormant
function M.resolve(path)
  if not path or path == '' then
    return nil
  end
  path = uv.fs_realpath(path) or path
  local best
  for _, entry in ipairs(M.brains()) do
    local root = entry.root:gsub('/+$', '')
    if path == root or path:sub(1, #root + 1) == root .. '/' then
      if not best or #root > #best.root then
        best = { root = root, sock = entry.sock, pid = entry.pid, ide = entry.ide }
      end
    end
  end
  return best
end

return M
