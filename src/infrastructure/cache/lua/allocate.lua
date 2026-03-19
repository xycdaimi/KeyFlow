local now_ts = tonumber(ARGV[1])
local lease_seconds = tonumber(ARGV[2])
local provider_zset = KEYS[1]
local lease_zset = KEYS[2]

for i = 3, #ARGV do
  local key_id = ARGV[i]
  if redis.call("ZSCORE", provider_zset, key_id) then
    local lease_until = redis.call("ZSCORE", lease_zset, key_id)
    if not (lease_until and tonumber(lease_until) > now_ts) then
      if lease_until then
        redis.call("ZREM", lease_zset, key_id)
      end

      local hash_key = "keyflow:key:" .. key_id
      local status = redis.call("HGET", hash_key, "status")
      local cooldown_until = redis.call("HGET", hash_key, "cooldown_until")

      local usable = status == "available"
      if (status == "rate_limited" or status == "cooldown") and cooldown_until and cooldown_until ~= "" then
        local cooldown_ts = tonumber(cooldown_until)
        usable = cooldown_ts and cooldown_ts <= now_ts
      end

      if usable then
        local expires_at = now_ts + lease_seconds
        redis.call("ZADD", lease_zset, expires_at, key_id)
        redis.call("HSET", hash_key, "status", "available")
        redis.call("HSET", hash_key, "cooldown_until", "")
        redis.call("HSET", hash_key, "last_used_at", tostring(now_ts))
        return key_id
      end
    end
  end
end

return nil