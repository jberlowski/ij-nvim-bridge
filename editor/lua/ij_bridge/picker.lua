-- A small floating picker: an input line above a list, filtered as you type by fuzzy matching (fuzzy.lua),
-- so it does not depend on which `vim.ui.select` a setup has (some are not fuzzy at all).
--
--   <CR> choose   <Esc> / <C-c> close   <C-n> <C-p> <Down> <Up> move   <C-d> <C-u> half a page
local fuzzy = require('ij_bridge.fuzzy')

local M = {}

local NS = vim.api.nvim_create_namespace('ij_bridge_picker')

--- The picker open now, for tests and for closing: { items, query, ranked, index, close }.
M.current = nil

--- @param items table[]
--- @param opts { title?: string, label: fun(item: table): string, texts: fun(item: table): string, string?, detail?: fun(item: table): string?, on_choose: fun(item: table), height?: integer, width?: integer }
function M.open(items, opts)
  if M.current then
    M.current.close()
  end
  local columns, lines = vim.o.columns, vim.o.lines
  local width = math.min(opts.width or 90, columns - 6)
  local height = math.min(opts.height or 16, lines - 8)
  local row = math.max(1, math.floor((lines - height) / 2) - 2)
  local col = math.floor((columns - width) / 2)

  local input_buf = vim.api.nvim_create_buf(false, true)
  local list_buf = vim.api.nvim_create_buf(false, true)
  vim.bo[input_buf].buftype = 'prompt'
  vim.bo[list_buf].bufhidden = 'wipe'
  vim.fn.prompt_setprompt(input_buf, '> ')

  local input_win = vim.api.nvim_open_win(input_buf, true, {
    relative = 'editor', row = row, col = col, width = width, height = 1, style = 'minimal', border = 'rounded',
    title = ' ' .. (opts.title or 'Find') .. ' ', title_pos = 'left', zindex = 60,
  })
  local list_win = vim.api.nvim_open_win(list_buf, false, {
    relative = 'editor', row = row + 3, col = col, width = width, height = height, style = 'minimal', border = 'rounded',
    zindex = 60, focusable = false,
  })
  vim.wo[list_win].cursorline = false
  vim.wo[list_win].wrap = false

  local state = { items = items, query = '', ranked = {}, index = 1 }
  M.current = state

  local function render()
    local shown = {}
    local top = math.max(1, math.min(state.index - math.floor(height / 2), #state.ranked - height + 1))
    for i = top, math.min(#state.ranked, top + height - 1) do
      local entry = state.ranked[i]
      local detail = opts.detail and opts.detail(entry.item)
      shown[#shown + 1] = { i = i, text = (i == state.index and '▶ ' or '  ') .. opts.label(entry.item)
        .. ((detail and detail ~= '') and ('  ' .. detail) or ''), entry = entry }
    end
    vim.api.nvim_buf_set_lines(list_buf, 0, -1, false, #shown > 0 and vim.tbl_map(function(s) return s.text end, shown) or { '  (nothing matches)' })
    vim.api.nvim_buf_clear_namespace(list_buf, NS, 0, -1)
    for row_index, s in ipairs(shown) do
      if s.i == state.index then
        vim.api.nvim_buf_set_extmark(list_buf, NS, row_index - 1, 0, { line_hl_group = 'Visual' })
      end
      -- underline what matched in the name (positions are into the label's first `#name` characters)
      local label = opts.label(s.entry.item)
      local offset = 2 + (label:find(s.entry.item.name or '', 1, true) or 1) - 1
      for _, p in ipairs(s.entry.positions) do
        pcall(vim.api.nvim_buf_set_extmark, list_buf, NS, row_index - 1, offset + p - 1, { end_col = offset + p, hl_group = 'Special' })
      end
      local detail = opts.detail and opts.detail(s.entry.item)
      if detail and detail ~= '' then
        pcall(vim.api.nvim_buf_set_extmark, list_buf, NS, row_index - 1, #s.text - #detail, { end_col = #s.text, hl_group = 'Comment' })
      end
    end
  end

  local function refilter()
    state.ranked = fuzzy.rank(items, state.query, opts.texts)
    state.index = math.min(math.max(1, state.index), math.max(1, #state.ranked))
    if state.query == '' then
      state.index = 1
    end
    render()
  end

  local function close()
    M.current = nil
    pcall(vim.cmd.stopinsert)
    for _, win in ipairs({ input_win, list_win }) do
      if vim.api.nvim_win_is_valid(win) then
        vim.api.nvim_win_close(win, true)
      end
    end
  end
  state.close = close

  local function move(by)
    if #state.ranked == 0 then
      return
    end
    state.index = ((state.index - 1 + by) % #state.ranked) + 1
    render()
  end

  vim.fn.prompt_setcallback(input_buf, function(text)
    -- Typed fast, or pasted, <CR> can arrive before the filter has caught up with the last letters: what is
    -- chosen is what the input says now, not what was last drawn.
    if text ~= state.query then
      state.query = text
      state.ranked = fuzzy.rank(items, text, opts.texts)
      state.index = 1
    end
    local entry = state.ranked[state.index]
    close()
    if entry then
      opts.on_choose(entry.item)
    end
  end)
  vim.api.nvim_create_autocmd('TextChangedI', {
    buffer = input_buf,
    callback = function()
      local line = vim.api.nvim_buf_get_lines(input_buf, 0, 1, false)[1] or ''
      state.query = line:sub(3)
      state.index = 1
      refilter()
    end,
  })
  local function map(lhs, fn)
    vim.keymap.set({ 'i', 'n' }, lhs, fn, { buffer = input_buf, nowait = true })
  end
  map('<Esc>', close)
  map('<C-c>', close)
  map('<C-n>', function() move(1) end)
  map('<Down>', function() move(1) end)
  map('<C-p>', function() move(-1) end)
  map('<Up>', function() move(-1) end)
  map('<C-d>', function() move(math.floor(height / 2)) end)
  map('<C-u>', function() move(-math.floor(height / 2)) end)
  vim.api.nvim_create_autocmd('WinLeave', { buffer = input_buf, once = true, callback = close })

  refilter()
  vim.cmd.startinsert()
  return state
end

return M
