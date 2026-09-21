-- Fuzzy matching, the way fzf does it: the letters typed must appear in the text in that order, anywhere,
-- in either case. Not a substring match: for `findMysteriousTreasure`, `eas` (an `e` in Mysterious, an `a`
-- and an `s` after it), `fMT` (its camel-hump initials) and `trea` (a substring) all find it.
--
-- What decides the order among what matches: a letter that starts a word (after `-_ :./`, or a capital after a
-- lower-case letter, or the first letter) counts most, so `fMT` beats a scattered match; letters typed next to each
-- other in the text count more than ones far apart; and a gap costs. Ties go to the shorter text.
local M = {}

local SCORE_MATCH = 16
local BONUS_BOUNDARY = 12 -- after a separator, or the first letter
local BONUS_CAMEL = 10 -- a capital after a lower-case letter, or a digit after a letter
local BONUS_CONSECUTIVE = 15
local PENALTY_GAP_START = 3
local PENALTY_GAP_EXTEND = 1
local BONUS_EXACT_CASE = 1

local function class(ch)
  if ch:match('%l') then
    return 'lower'
  elseif ch:match('%u') then
    return 'upper'
  elseif ch:match('%d') then
    return 'digit'
  elseif ch:match('[%-_ :%./\\|>]') then
    return 'sep'
  end
  return 'other'
end

--- How much matching the letter at `i` is worth, from what stands before it.
local function bonus_at(text, i)
  if i == 1 then
    return BONUS_BOUNDARY
  end
  local prev, cur = class(text:sub(i - 1, i - 1)), class(text:sub(i, i))
  if prev == 'sep' and cur ~= 'sep' then
    return BONUS_BOUNDARY
  end
  if prev == 'lower' and cur == 'upper' then
    return BONUS_CAMEL
  end
  if prev ~= 'digit' and cur == 'digit' then
    return BONUS_CAMEL
  end
  return 0
end

--- The best way to lay one term over the text.
--- @return integer? score nil when the letters do not occur in order
--- @return integer[]? positions the matched columns, 1-based
local function match_term(term, text)
  local n, m = #text, #term
  if m == 0 then
    return 0, {}
  end
  if m > n then
    return nil
  end
  local lt, lp = text:lower(), term:lower()

  -- best[i][j]: the best score with term[i] matched at text[j]; from[i][j] where term[i-1] was matched.
  local best, from = {}, {}
  local bonus = {}
  for j = 1, n do
    bonus[j] = bonus_at(text, j)
  end
  for i = 1, m do
    best[i], from[i] = {}, {}
    for j = 1, n do
      if lt:sub(j, j) == lp:sub(i, i) then
        local here = SCORE_MATCH + bonus[j] + ((text:sub(j, j) == term:sub(i, i)) and BONUS_EXACT_CASE or 0)
        if i == 1 then
          -- a match that starts late is worth a little less: the letters before it were skipped
          best[i][j] = here - ((j > 1) and (PENALTY_GAP_START + (j - 2) * PENALTY_GAP_EXTEND) or 0) * 0.5
        else
          local top, at
          for k = i - 1, j - 1 do
            local before = best[i - 1][k]
            if before then
              local gap = j - k - 1
              local joined
              if gap == 0 then
                -- next to it: consecutive, and worth at least the boundary the run began on
                joined = before + here + BONUS_CONSECUTIVE
              else
                joined = before + here - (PENALTY_GAP_START + (gap - 1) * PENALTY_GAP_EXTEND)
              end
              if not top or joined > top then
                top, at = joined, k
              end
            end
          end
          best[i][j], from[i][j] = top, at
        end
      end
    end
  end

  local score, last
  for j = m, n do
    local s = best[m][j]
    if s and (not score or s > score) then
      score, last = s, j
    end
  end
  if not score then
    return nil
  end
  local positions = {}
  local j = last
  for i = m, 1, -1 do
    positions[i] = j
    j = from[i][j]
  end
  return score, positions
end

--- Match a query (terms separated by spaces, all of which must match) against a text.
--- @return number? score
--- @return integer[]? positions
function M.match(query, text)
  local total, all = 0, {}
  local any = false
  for term in query:gmatch('%S+') do
    any = true
    local score, positions = match_term(term, text)
    if not score then
      return nil
    end
    total = total + score
    vim.list_extend(all, positions)
  end
  if not any then
    return 0, {}
  end
  table.sort(all)
  -- Shorter texts first among equal matches.
  return total - #text * 0.01, all
end

--- Filter and rank `items`: those that match, best first.
--- @param items table[]
--- @param query string
--- @param texts fun(item: table): string, string? the text to match, and an optional second one (weaker)
--- @return table[] ranked entries `{ item, score, positions }` (positions are into the first text)
function M.rank(items, query, texts)
  local out = {}
  for _, item in ipairs(items) do
    local primary, secondary = texts(item)
    local score, positions = M.match(query, primary)
    if score then
      score = score * 2
    elseif secondary then
      -- Not in the name, but in what surrounds it (its group, its project): worth less, and nothing to underline.
      score = M.match(query, secondary)
      positions = {}
    end
    if score then
      table.insert(out, { item = item, score = score, positions = positions or {} })
    end
  end
  table.sort(out, function(a, b)
    if a.score ~= b.score then
      return a.score > b.score
    end
    return tostring(select(1, texts(a.item))) < tostring(select(1, texts(b.item)))
  end)
  return out
end

return M
