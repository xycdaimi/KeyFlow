local now_ts = tonumber(ARGV[1])
local lease_seconds = tonumber(ARGV[2])
local provider_zset = KEYS[1]
local lease_zset = KEYS[2]

local function is_usable(key_id)
  local hash_key = "keyflow:key:" .. key_id
  local status = redis.call("HGET", hash_key, "status")
  local cooldown_until = redis.call("HGET", hash_key, "cooldown_until")

  local usable = status == "available"
  if (status == "rate_limited" or status == "cooldown") and cooldown_until and cooldown_until ~= "" then
    local cooldown_ts = tonumber(cooldown_until)
    usable = cooldown_ts and cooldown_ts <= now_ts
  end

  return usable
end

local function mark_allocated(key_id)
  local expires_at = now_ts + lease_seconds
  local hash_key = "keyflow:key:" .. key_id
  local active_count = tonumber(redis.call("HGET", hash_key, "active_lease_count") or "0") or 0
  redis.call("ZADD", lease_zset, expires_at, key_id)
  redis.call("HSET", hash_key, "active_lease_count", tostring(active_count + 1))
  redis.call("HSET", hash_key, "status", "available")
  redis.call("HSET", hash_key, "cooldown_until", "")
  redis.call("HSET", hash_key, "last_used_at", tostring(now_ts))
  return key_id
end

local function active_count_for(key_id)
  local hash_key = "keyflow:key:" .. key_id
  local lease_until = redis.call("ZSCORE", lease_zset, key_id)
  if lease_until and tonumber(lease_until) <= now_ts then
    redis.call("ZREM", lease_zset, key_id)
    redis.call("HSET", hash_key, "active_lease_count", "0")
    return 0
  end
  return tonumber(redis.call("HGET", hash_key, "active_lease_count") or "0") or 0
end

local function max_concurrent_for(key_id)
  local hash_key = "keyflow:key:" .. key_id
  local value = tonumber(redis.call("HGET", hash_key, "max_concurrent_uses") or "1") or 1
  if value < 1 then
    return 1
  end
  return value
end

for i = 4, #ARGV do
  local key_id = ARGV[i]
  if redis.call("ZSCORE", provider_zset, key_id) then
    if is_usable(key_id) and active_count_for(key_id) < max_concurrent_for(key_id) then
      return mark_allocated(key_id)
    end
  end
end

return nil
