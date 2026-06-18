local now_ts = tonumber(ARGV[1])
local lease_seconds = tonumber(ARGV[2])
local provider = ARGV[3]
local pool = ARGV[4]
local lease_id = ARGV[5]
local provider_zset = KEYS[1]

local function lease_hash(lease_id_value)
  return "keyflow:lease:" .. lease_id_value
end

local function key_lease_zset(key_id)
  return "keyflow:key:" .. key_id .. ":leases"
end

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

local function prune_expired(key_id)
  local zset_key = key_lease_zset(key_id)
  local expired = redis.call("ZRANGEBYSCORE", zset_key, "-inf", now_ts)
  for _, expired_lease_id in ipairs(expired) do
    redis.call("DEL", lease_hash(expired_lease_id))
  end
  if #expired > 0 then
    redis.call("ZREMRANGEBYSCORE", zset_key, "-inf", now_ts)
  end
end

local function active_count_for(key_id)
  prune_expired(key_id)
  return redis.call("ZCARD", key_lease_zset(key_id))
end

local function max_concurrent_for(key_id)
  local hash_key = "keyflow:key:" .. key_id
  local value = tonumber(redis.call("HGET", hash_key, "max_concurrent_uses") or "1") or 1
  if value < 1 then
    return 1
  end
  return value
end

local function mark_allocated(key_id)
  local expires_at = now_ts + lease_seconds
  local key_hash = "keyflow:key:" .. key_id
  redis.call("ZADD", key_lease_zset(key_id), expires_at, lease_id)
  redis.call(
    "HSET",
    lease_hash(lease_id),
    "key_id",
    key_id,
    "provider",
    provider,
    "pool",
    pool,
    "lease_until",
    tostring(expires_at)
  )
  redis.call("HSET", key_hash, "status", "available")
  redis.call("HSET", key_hash, "cooldown_until", "")
  redis.call("HSET", key_hash, "last_used_at", tostring(now_ts))
  return key_id
end

for i = 6, #ARGV do
  local key_id = ARGV[i]
  if redis.call("ZSCORE", provider_zset, key_id) then
    if is_usable(key_id) and active_count_for(key_id) < max_concurrent_for(key_id) then
      return mark_allocated(key_id)
    end
  end
end

return nil
