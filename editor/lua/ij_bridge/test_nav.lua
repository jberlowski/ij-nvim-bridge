-- Go to test / go to the class under test (FEATURES.md §6c): `$/ij/testTargets`.
--
-- One request, one direction, chosen by what the caret is in: from a class, its test(s); from a
-- test, the class it tests. Several results go to the quickfix list, as Neovim's own
-- `vim.lsp.buf.definition` does for several locations. No test yet: asks, then writes it (the
-- same "first write attaches it" as a new file, FEATURES.md §5e).
local log = require('ij_bridge.log')

local M = {}

local function say(msg, level)
  vim.notify('ij-bridge: ' .. msg, level or vim.log.levels.INFO)
end

local function create(client, created)
  vim.ui.select({ 'Yes', 'No' }, { prompt = ('No test yet. Create %s?'):format(created.className) }, function(choice)
    if choice ~= 'Yes' then
      return
    end
    local path = vim.uri_to_fname(created.uri)
    vim.fn.mkdir(vim.fs.dirname(path), 'p')
    vim.cmd.edit(vim.fn.fnameescape(path))
    vim.api.nvim_buf_set_lines(0, 0, -1, false, vim.split((created.text:gsub('\n$', '')), '\n', { plain = true }))
    vim.cmd.write()
    log.info('test_created', { path = path, class = created.className })
  end)
end

--- Go to the test for the class at the cursor, or the class the test at the cursor tests.
function M.go()
  local client = require('ij_bridge').client(0)
  if not client then
    say('no IntelliJ connection', vim.log.levels.WARN)
    return
  end
  local params = vim.lsp.util.make_position_params(0, client.offset_encoding)
  client:request('$/ij/testTargets', params, function(err, result)
    if err then
      say(err.message, vim.log.levels.WARN)
      return
    end
    if not result then
      say('nothing here to find a test for')
      return
    end
    local locations = result.locations or {}
    if #locations == 1 then
      vim.lsp.util.show_document(locations[1], client.offset_encoding, { focus = true })
      return
    elseif #locations > 1 then
      vim.fn.setqflist({}, ' ', {
        title = result.kind == 'test' and 'Tests' or 'Classes under test',
        items = vim.lsp.util.locations_to_items(locations, client.offset_encoding),
      })
      vim.cmd('botright copen')
      return
    end
    if result.create then
      create(client, result.create)
    else
      say(result.kind == 'test' and 'no test, and nowhere suitable to create one' or 'no class under test found')
    end
  end, 0)
end

return M
