-- Install the persistent connection label without replacing the user's statusline.
local M = {}
local function label() return require('ij_bridge').statusline() end

function M.setup(group)
  local function install()
    if require('ij_bridge').opts.connection_indicator == false then return end
    local ok, lualine = pcall(require, 'lualine')
    if ok then
      local config = lualine.get_config()
      config.sections = config.sections or {}
      config.sections.lualine_x = config.sections.lualine_x or {}
      for _, component in ipairs(config.sections.lualine_x) do
        if type(component) == 'table' and component[1] == label then return end
      end
      table.insert(config.sections.lualine_x, { label })
      lualine.setup(config)
    else
      local component = "%{%v:lua.require('ij_bridge').statusline()%}"
      if not vim.o.statusline:find(component, 1, true) then
        vim.o.statusline = vim.o.statusline .. ' ' .. component
      end
    end
  end
  vim.schedule(install)
  -- A statusline plugin loaded after the bridge can replace the initial configuration.
  vim.api.nvim_create_autocmd('User', { group = group, pattern = 'LazyLoad', callback = function(args)
    if args.data == 'lualine.nvim' then vim.schedule(install) end
  end })
end

return M
