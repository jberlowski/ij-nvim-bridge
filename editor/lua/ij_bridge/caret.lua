-- Caret following, opt-in (FEATURES.md §6d): the Neovim cursor moves the caret of the buffer's Mirror in IntelliJ, and
-- the developer moving that caret in the IntelliJ window moves the cursor here. Off unless asked for
-- (`setup({ follow_caret = true })`, or `:IjBridge follow on`), so a developer who wants IntelliJ as a silent analysis
-- engine never has a window jump under them.
--
-- Neovim -> IntelliJ: `$/ij/caret { textDocument, version, position }`, debounced (~100 ms: holding `j` is one event
-- at the end, not one per line) and only if the position changed. It carries the buffer's LSP version, since buffer
-- changes are sent debounced too and a caret can beat the change it refers to: the Brain waits for it.
--
-- IntelliJ -> Neovim: `$/ij/caret { uri, version, position }` moves the cursor of the window showing that buffer,
-- if the buffer is at that version. It does not switch buffers or windows: this is caret following, not file following.
local M = {}

M.enabled = false

--- buf -> "line:character" last sent or applied, so neither side hears the other's echo, nor the same place twice.
local last = {}
local timers = {}
local DEBOUNCE_MS = 100

local function ij()
  return require('ij_bridge')
end

local function version(buf)
  local versions = vim.lsp.util.buf_versions
  return versions and versions[buf] or 0
end

--- The cursor of the window showing `buf`, as an LSP position (UTF-16), or nil.
local function position(buf)
  local win = vim.fn.bufwinid(buf)
  if win == -1 then
    return nil
  end
  local cursor = vim.api.nvim_win_get_cursor(win)
  local text = vim.api.nvim_buf_get_lines(buf, cursor[1] - 1, cursor[1], false)[1] or ''
  local ok, character = pcall(vim.str_utfindex, text, 'utf-16', math.min(cursor[2], #text))
  return { line = cursor[1] - 1, character = ok and character or cursor[2] }
end

local function key(pos)
  return pos.line .. ':' .. pos.character
end

function M.send(buf, tries)
  if not (M.enabled and vim.api.nvim_buf_is_valid(buf)) then
    return
  end
  local client = ij().client(buf)
  if not client then
    -- Not attached yet (a buffer just entered): the attach is asynchronous.
    if (tries or 0) < 3 then
      vim.defer_fn(function() M.send(buf, (tries or 0) + 1) end, 400)
    end
    return
  end
  local pos = position(buf)
  if not pos or last[buf] == key(pos) then
    return
  end
  last[buf] = key(pos)
  client:notify('$/ij/caret', { textDocument = { uri = vim.uri_from_bufnr(buf) }, version = version(buf), position = pos })
end

--- The cursor of `buf` may have moved: send it once it has settled.
function M.touch(buf)
  if not M.enabled then
    return
  end
  local timer = timers[buf]
  if not timer then
    timer = vim.uv.new_timer()
    timers[buf] = timer
  end
  timer:stop()
  timer:start(DEBOUNCE_MS, 0, vim.schedule_wrap(function() M.send(buf) end))
end

--- `$/ij/caret` from IntelliJ.
function M.on_caret(params)
  if not M.enabled then
    return
  end
  local buf = vim.uri_to_bufnr(params.uri)
  if not vim.api.nvim_buf_is_loaded(buf) then
    return
  end
  local behind = params.version and version(buf) ~= params.version
  local win = vim.fn.bufwinid(buf)
  if behind or win == -1 then
    return
  end
  local pos = params.position
  local text = vim.api.nvim_buf_get_lines(buf, pos.line, pos.line + 1, false)[1]
  if not text then
    return
  end
  local ok, byte = pcall(vim.str_byteindex, text, 'utf-16', pos.character)
  last[buf] = key(pos) -- so that moving the cursor here is not sent back
  pcall(vim.api.nvim_win_set_cursor, win, { pos.line + 1, ok and byte or #text })
end

local function tell(client, on)
  client:notify('$/ij/follow', { enabled = on })
end

--- A Session has just come up: tell it, if following is on.
function M.on_client(client)
  if M.enabled then
    tell(client, true)
  end
end

function M.enable(on)
  M.enabled = on == true
  last = {}
  local group = vim.api.nvim_create_augroup('IjBridgeCaret', { clear = true })
  if M.enabled then
    vim.api.nvim_create_autocmd({ 'CursorMoved', 'CursorMovedI' }, {
      group = group,
      callback = function(args) M.touch(args.buf) end,
    })
  end
  for _, client in ipairs(vim.lsp.get_clients({ name = ij().name })) do
    tell(client, M.enabled)
  end
  if M.enabled then
    M.touch(vim.api.nvim_get_current_buf())
  end
end

return M
