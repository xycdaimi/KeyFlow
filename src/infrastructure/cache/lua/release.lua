local provider = ARGV[1]
local pool = ARGV[2]
local key_id = ARGV[3]
local lease_id = ARGV[4]

local lease_hash = "keyflow:lease:" .. lease_id
local stored_key_id = redis.call("HGET", lease_hash, "key_id")
local stored_provider = redis.call("HGET", lease_hash, "provider")
local stored_pool = redis.call("HGET", lease_hash, "pool")

if stored_key_id ~= key_id or stored_provider ~= provider or stored_pool ~= pool then
  return 0
end

redis.call("DEL", lease_hash)
redis.call("ZREM", "keyflow:key:" .. key_id .. ":leases", lease_id)
return 1
