-- nvim --headless -u NONE -l tests/workflow.lua
vim.opt.rtp:prepend(vim.fn.getcwd() .. '/editor')
local tasks = require('ij_bridge.tasks')
local function project(name, path, names, children)
  return { name = name, path = path, groups = { { name = 'other', tasks = vim.tbl_map(function(n) return { name = n } end, names) } }, projects = children }
end
local tree = { projects = { project('root', '/project', { 'build', 'clean' }, {
  project('a', '/project/a', { 'build', 'clean', 'generateOpenApi', 'bootJar' }),
  project('b', '/project/b', { 'build', 'clean', 'generateOpenApi', 'bootJar' }),
}) } }
local picker = require('ij_bridge.picker')
tasks.fetch = function(cb) cb(tree) end
tasks.pick()
assert(picker.current.ranked[1].item.name == 'build', 'build must be first')
assert(picker.current.ranked[2].item.name == 'clean build', 'clean build must be second')
local aggregate
for _, item in ipairs(picker.current.items) do
  if item.name == 'generateOpenApi' and item.aggregate then aggregate = item end
end
assert(aggregate, 'shared task must have an all-projects option')
assert(aggregate.path == '/project' and vim.deep_equal(aggregate.tasks, { 'generateOpenApi' }))
local texts = function(item) return item.name, item.group .. ' ' .. item.project end
local ranked = tasks.rank(picker.current.items, 'generateOpenApi', texts)
assert(#ranked == 3 and ranked[1].item.aggregate, 'all-project option must precede both subprojects')
for _, entry in ipairs(ranked) do assert(entry.item.name == 'generateOpenApi') end
assert(#tasks.rank(picker.current.items, 'noSuchTask', texts) == 0)
local by_group = tasks.rank({
  { name = 'assemble', group = 'build', project = 'root', aggregate = true },
  { name = 'build', group = 'build', project = 'a' },
  { name = 'build', group = 'build', project = 'root', aggregate = true },
}, 'build', texts)
assert(by_group[1].item.aggregate and by_group[1].item.name == 'build')
assert(not by_group[2].item.aggregate, 'an aggregate group-only match must not outrank an exact subproject task')
picker.current.close()
local sent
local fake = { config = { root_dir = '/project' }, request = function(_, method, params) sent = params end }
require('ij_bridge').client = function() return fake end
tasks.run({ name = 'clean build', path = '/project', tasks = { 'clean', 'build' } })
assert(vim.deep_equal(sent.tasks, { 'clean', 'build' }), 'clean/build must be separate task arguments')
print('task workflow OK')
local diagnostics = require('ij_bridge.diagnostics')
local buf = vim.api.nvim_get_current_buf()
vim.bo[buf].filetype = 'markdown'
vim.api.nvim_buf_set_name(buf, '/tmp/bridge-workflow.md')
vim.api.nvim_buf_set_lines(buf, 0, -1, false, { '# probe' })
vim.lsp.get_client_by_id = function(id)
  if id == 42 then return { id = id, name = 'ij-bridge', offset_encoding = 'utf-16' } end
end
local function shown()
  return vim.diagnostic.get(buf, { namespace = vim.lsp.diagnostic.get_namespace(42) })
end
local other = vim.api.nvim_create_namespace('other-diagnostics')
vim.diagnostic.set(other, buf, { { lnum = 0, col = 0, message = 'other source' } })
local params = { uri = vim.uri_from_bufnr(buf), diagnostics = { { severity = 2, message = 'probe', range = { start = { line = 0, character = 0 }, ['end'] = { line = 0, character = 7 } } } } }
diagnostics.on_publish(nil, params, { client_id = 42 })
assert(#shown() == 1)
local state = vim.fn.tempname()
diagnostics.setup({ state_path = state })
assert(diagnostics.exclude('markdown'))
assert(#shown() == 0, 'exclude must clear existing diagnostics')
diagnostics.on_publish(nil, params, { client_id = 42 })
assert(#shown() == 0, 'exclude must hide incoming diagnostics')
assert(#vim.diagnostic.get(buf, { namespace = other }) == 1, 'other diagnostic sources must remain visible')
assert(#diagnostics.report(buf) == 1, 'raw diagnostics must remain available')
package.loaded['ij_bridge.diagnostics'] = nil
local reloaded = require('ij_bridge.diagnostics')
reloaded.setup({ state_path = state })
assert(vim.deep_equal(reloaded.exclusions(), { 'markdown' }), 'exclusions must persist')
assert(diagnostics.include('markdown'))
assert(#shown() == 1, 'include must restore cached diagnostics')
vim.fn.delete(state)
print('diagnostic workflow OK')
