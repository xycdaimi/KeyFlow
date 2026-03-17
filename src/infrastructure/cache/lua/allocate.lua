local now_ts = tonumber(ARGV[1])
local provider_zset = KEYS[1]

for i = 2, #ARGV do
  local key_id = ARGV[i]
  if redis.call("ZSCORE", provider_zset, key_id) then
    local hash_key = "keyflow:key:" .. key_id
    local status = redis.call("HGET", hash_key, "status")
    local cooldown_until = redis.call("HGET", hash_key, "cooldown_until")

    local usable = status == "available"
    if (status == "rate_limited" or status == "cooldown") and cooldown_until then
      usable = tonumber(cooldown_until) <= now_ts
    end

    if usable then
      redis.call("HSET", hash_key, "status", "available")
      redis.call("HSET", hash_key, "cooldown_until", "")
      redis.call("HSET", hash_key, "last_used_at", tostring(now_ts))
      return key_id
    end
  end
end

return nil
