-- All state transitions run in this single script on one Valkey instance.
-- KEYS[1] is the namespace prefix; this scheduler intentionally uses no cluster.
local prefix = KEYS[1]
local op = ARGV[1]
local now = tonumber(ARGV[2])
local today = ARGV[3]
local config = prefix .. ':config'
local global = prefix .. ':global'
local ready = prefix .. ':ready'
local active = prefix .. ':active'
local outcomes = prefix .. ':outcomes'

local function result(status, body)
  return cjson.encode({status = status, body = body})
end

local function number_field(key, field, fallback)
  return tonumber(redis.call('HGET', key, field)) or fallback
end

local function project_key(index)
  return prefix .. ':project:' .. index
end

local function lease_key(id)
  return prefix .. ':lease:' .. id
end

local function member(index)
  return string.format('%03d', index)
end

local function prune_active()
  redis.call('ZREMRANGEBYSCORE', active, '-inf', now)
end

local function reset_day_if_needed()
  local previous_day = redis.call('HGET', config, 'pacific_day')
  if not previous_day or previous_day == today then return end
  local count = number_field(config, 'key_count', 0)
  redis.call('DEL', ready)
  for index = 0, count - 1 do
    redis.call('HSET', project_key(index),
      'requests_today', 0, 'daily_exhausted', 0, 'cooldown_until', 0,
      'success_count', 0, 'rate_limited_count', 0, 'error_count', 0)
    redis.call('ZADD', ready, 0, member(index))
  end
  redis.call('HSET', config, 'pacific_day', today)
end

local function recent_stats()
  local recent = redis.call('LRANGE', outcomes, 0, 19)
  local unavailable = 0
  local successes = 0
  for _, status in ipairs(recent) do
    if status == '503' then unavailable = unavailable + 1 end
    if status == '200' then successes = successes + 1 end
  end
  return #recent, unavailable, successes
end

if op == 'configure' then
  local mapping = ARGV[4]
  local count = tonumber(ARGV[5])
  local rpm = tonumber(ARGV[6])
  local rpd = tonumber(ARGV[7])
  local configured_max = tonumber(ARGV[8])
  local previous_mapping = redis.call('HGET', config, 'mapping_hash')
  if previous_mapping and previous_mapping ~= mapping then
    return result(409, {error = 'project_mapping_changed'})
  end
  prune_active()
  if previous_mapping and redis.call('ZCARD', active) > 0 and
    (number_field(config, 'rpm_per_project', 0) ~= rpm or
     number_field(config, 'rpd_per_project', 0) ~= rpd) then
    return result(409, {error = 'quota_limits_in_use'})
  end
  if not previous_mapping then
    redis.call('DEL', ready)
    for index = 0, count - 1 do
      redis.call('HSET', project_key(index),
        'requests_today', 0, 'daily_exhausted', 0, 'cooldown_until', 0,
        'success_count', 0, 'rate_limited_count', 0, 'error_count', 0)
      redis.call('ZADD', ready, 0, member(index))
    end
    redis.call('HSET', config, 'mapping_hash', mapping, 'key_count', count,
      'pacific_day', today)
    redis.call('HSET', global, 'max_inflight_current', configured_max)
  else
    reset_day_if_needed()
  end
  redis.call('HSET', config, 'rpm_per_project', rpm, 'rpd_per_project', rpd)
  return result(200, {configured = true, keyCount = count, projectCount = count,
    pacificDate = today, rpmPerProject = rpm, rpdPerProject = rpd})
end

if not redis.call('HGET', config, 'mapping_hash') then
  return result(503, {error = 'quota_pool_not_configured'})
end
reset_day_if_needed()

if op == 'lease' then
  local id = ARGV[4]
  local configured_max = tonumber(ARGV[5])
  local rps = tonumber(ARGV[6])
  local burst = tonumber(ARGV[7])
  local lease_timeout = tonumber(ARGV[8])
  local jitter = tonumber(ARGV[9])
  local existing = lease_key(id)
  if redis.call('EXISTS', existing) == 1 then
    local project = number_field(existing, 'project_index', -1)
    return result(200, {projectIndex = project, keyIndex = project,
      leaseId = id, globalMaxInflight = number_field(global,
      'max_inflight_current', configured_max), replayed = true})
  end
  local cooldown = number_field(global, 'cooldown_until', 0)
  if cooldown > now then
    return result(202, {reason = 'global_cooldown',
      waitMs = math.min(30000, cooldown - now + jitter)})
  end
  prune_active()
  local max_inflight = math.min(configured_max,
    number_field(global, 'max_inflight_current', configured_max))
  if redis.call('ZCARD', active) >= max_inflight then
    local oldest = redis.call('ZRANGE', active, 0, 0, 'WITHSCORES')
    local wait = oldest[2] and tonumber(oldest[2]) - now or 1000
    return result(202, {reason = 'max_inflight',
      waitMs = math.max(250, math.min(5000, wait))})
  end
  local tokens = number_field(global, 'tokens', burst)
  local updated = number_field(global, 'tokens_updated_at', now)
  tokens = math.min(burst, tokens + math.max(0, now - updated) * rps / 1000)
  if tokens < 1 then
    return result(202, {reason = 'global_rate',
      waitMs = math.max(2000, math.ceil((1 - tokens) * 1000 / rps)) + jitter})
  end
  local next_project = redis.call('ZRANGE', ready, 0, 0, 'WITHSCORES')
  if not next_project[1] then
    return result(429, {error = 'quota_pool_exhausted', pacificDate = today})
  end
  local project = tonumber(next_project[1])
  local ready_at = tonumber(next_project[2])
  if ready_at > now then
    return result(202, {reason = 'project_quota',
      waitMs = math.max(250, math.min(30000, math.ceil(ready_at - now)))})
  end
  local rpm = number_field(config, 'rpm_per_project', 15)
  local rpd = number_field(config, 'rpd_per_project', 500)
  local used = number_field(project_key(project), 'requests_today', 0) + 1
  redis.call('HSET', global, 'tokens', tokens - 1, 'tokens_updated_at', now)
  redis.call('HSET', project_key(project), 'requests_today', used,
    'last_assigned_at', now)
  if used >= rpd then
    redis.call('HSET', project_key(project), 'daily_exhausted', 1)
    redis.call('ZREM', ready, member(project))
  else
    redis.call('ZADD', ready, now + math.ceil(60000 * 1.08 / rpm),
      member(project))
  end
  redis.call('HSET', existing, 'project_index', project, 'created_at', now,
    'pacific_day', today)
  redis.call('EXPIRE', existing, 7 * 24 * 3600)
  redis.call('ZADD', active, now + lease_timeout, id)
  return result(200, {projectIndex = project, keyIndex = project,
    leaseId = id, globalMaxInflight = max_inflight})
end

if op == 'report' then
  local id = ARGV[4]
  local status = tonumber(ARGV[5])
  local cooldown_ms = tonumber(ARGV[6])
  local daily_exhausted = ARGV[7] == '1'
  local quota_type = ARGV[8]
  local configured_max = tonumber(ARGV[9])
  local jitter = tonumber(ARGV[10])
  local lease = lease_key(id)
  if redis.call('EXISTS', lease) == 0 then
    return result(404, {error = 'lease_not_found'})
  end
  if redis.call('HEXISTS', lease, 'reported_at') == 1 then
    return result(200, {reported = true, duplicate = true})
  end
  local project = number_field(lease, 'project_index', -1)
  local lease_day = redis.call('HGET', lease, 'pacific_day')
  redis.call('HSET', lease, 'reported_at', now, 'http_status', status)
  redis.call('ZREM', active, id)
  redis.call('LPUSH', outcomes, status)
  redis.call('LTRIM', outcomes, 0, 19)
  if lease_day == today then
    local project_state = project_key(project)
    if status == 429 then
      redis.call('HINCRBY', project_state, 'rate_limited_count', 1)
      if daily_exhausted then
        redis.call('HSET', project_state, 'daily_exhausted', 1,
          'requests_today', number_field(config, 'rpd_per_project', 500))
        redis.call('ZREM', ready, member(project))
      else
        local until_time = now + math.min(86400000,
          math.max(1000, cooldown_ms))
        until_time = math.max(until_time,
          number_field(project_state, 'cooldown_until', 0))
        redis.call('HSET', project_state, 'cooldown_until', until_time)
        local existing_score = redis.call('ZSCORE', ready, member(project))
        if existing_score then
          redis.call('ZADD', ready, math.max(tonumber(existing_score),
            until_time), member(project))
        end
      end
    elseif status == 200 then
      redis.call('HINCRBY', project_state, 'success_count', 1)
    else
      redis.call('HINCRBY', project_state, 'error_count', 1)
    end
  end
  if status == 429 and (quota_type == 'unknown' or
    quota_type == 'tpm' or quota_type == 'spend') then
    local until_time = now + 2000 + (jitter % 3001)
    redis.call('HSET', global, 'cooldown_until',
      math.max(until_time, number_field(global, 'cooldown_until', 0)))
  end
  local count, unavailable, successes = recent_stats()
  local last_change = number_field(global, 'controller_changed_at', 0)
  local current_max = math.min(configured_max,
    number_field(global, 'max_inflight_current', configured_max))
  -- Backpressure controller, graded in both directions. The previous recovery
  -- gate required 19 successes out of the last 20 outcomes, which a free-tier
  -- model that answers 503 on roughly half of its calls can never satisfy, so
  -- the pool pinned itself at 8 in-flight plus a long global cooldown forever.
  local degrade_ratio = tonumber(ARGV[11]) or 0.35
  local recover_ratio = tonumber(ARGV[12]) or 0.60
  if count >= 10 and unavailable / count >= degrade_ratio and
    now - last_change >= 30000 then
    local stage = math.min(3, number_field(global, 'adaptive_stage', 0) + 1)
    local next_max = math.min(configured_max, current_max > 12 and 12 or 8)
    local minimum = stage == 1 and 5000 or (stage == 2 and 15000 or 30000)
    local span = stage == 1 and 10001 or (stage == 2 and 15001 or 30001)
    local until_time = now + minimum + (jitter % span)
    redis.call('HSET', global, 'max_inflight_current', next_max,
      'adaptive_stage', stage, 'controller_changed_at', now,
      'cooldown_until', math.max(until_time,
      number_field(global, 'cooldown_until', 0)))
  elseif status == 200 and count >= 10 and
    successes / count >= recover_ratio and
    current_max < configured_max and now - last_change >= 20000 then
    local next_max = configured_max
    if current_max <= 8 then
      next_max = 12
    elseif current_max <= 12 then
      next_max = 16
    end
    redis.call('HSET', global, 'max_inflight_current',
      math.min(configured_max, next_max),
      'adaptive_stage', math.max(0, number_field(global, 'adaptive_stage', 0) - 1),
      'controller_changed_at', now)
  end
  return result(200, {reported = true, duplicate = false,
    staleLease = lease_day ~= today})
end

if op == 'status' then
  prune_active()
  local count = number_field(config, 'key_count', 0)
  local projects = {}
  local total_requests = 0
  for index = 0, count - 1 do
    local key = project_key(index)
    local requests = number_field(key, 'requests_today', 0)
    total_requests = total_requests + requests
    projects[index + 1] = {projectIndex = index, requestsToday = requests,
      cooldownUntil = number_field(key, 'cooldown_until', 0),
      dailyExhausted = number_field(key, 'daily_exhausted', 0) == 1,
      successCount = number_field(key, 'success_count', 0),
      rateLimitedCount = number_field(key, 'rate_limited_count', 0),
      errorCount = number_field(key, 'error_count', 0)}
  end
  local configured_max = tonumber(ARGV[4])
  local rps = tonumber(ARGV[5])
  local burst = tonumber(ARGV[6])
  local lease_timeout = tonumber(ARGV[7])
  local cooldown = number_field(global, 'cooldown_until', 0)
  local recent_count, unavailable, successes = recent_stats()
  local active_ids = redis.call('ZRANGE', active, 0, -1)
  local active_projects = {}
  for _, id in ipairs(active_ids) do
    local project = redis.call('HGET', lease_key(id), 'project_index')
    if project then active_projects[project] = true end
  end
  local active_count = 0
  for _ in pairs(active_projects) do active_count = active_count + 1 end
  return result(200, {configured = true, keyCount = count,
    projectCount = count, pacificDate = today,
    rpmPerProject = number_field(config, 'rpm_per_project', 15),
    rpdPerProject = number_field(config, 'rpd_per_project', 500),
    requestsToday = total_requests, activeLeases = #active_ids,
    projects = projects, global = {
      maxInflight = math.min(configured_max,
        number_field(global, 'max_inflight_current', configured_max)),
      configuredMaxInflight = configured_max, activeLeases = #active_ids,
      requestsPerSecond = rps, burst = burst,
      leaseTimeoutSeconds = lease_timeout / 1000,
      cooldownUntil = cooldown > now and cooldown or cjson.null,
      cooldownRemainingMs = math.max(0, cooldown - now),
      adaptiveStage = number_field(global, 'adaptive_stage', 0),
      recentOutcomes = recent_count,
      recent503Ratio = recent_count > 0 and unavailable / recent_count or 0,
      recentSuccesses = successes, activeProjects = active_count}})
end

return result(404, {error = 'not_found'})
