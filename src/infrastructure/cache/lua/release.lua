local lease_zset = KEYS[1]
local key_id = ARGV[1]
local hash_key = "keyflow:key:" .. key_id
local active_count = tonumber(redis.call("HGET", hash_key, "active_lease_count") or "0") or 0

if active_count <= 1 then
  redis.call("HSET", hash_key, "active_lease_count", "0")
  redis.call("ZREM", lease_zset, key_id)
  return 0
end

active_count = active_count - 1
redis.call("HSET", hash_key, "active_lease_count", tostring(active_count))
return active_count
