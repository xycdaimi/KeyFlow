# KeyFlow Gateway Control Plane Design

## Purpose

KeyFlow will add an independent gateway service for multi-node credential management.
The gateway is a control plane for the ai_router management UI. It does not participate
in runtime key allocation, request execution, or success/error reporting from executors.

The reason for keeping runtime traffic out of the gateway is that each KeyFlow child
node must share the same egress IP as the executor that uses its credentials. Executors
therefore call their local or same-egress KeyFlow node directly for allocation and
reporting.

## Architecture

There are two separate paths:

```text
Management path:
ai_router management UI -> keyflow-gateway -> selected KeyFlow child node

Runtime path:
executor -> local or same-egress KeyFlow child node -> provider
```

The gateway is deployed as its own container and exposes management APIs to ai_router.
Existing KeyFlow instances are deployed as child nodes, initially in `local` runtime
mode with their own SQLite storage.

## Responsibilities

### Gateway

- Store child node profiles: `node_id`, display name, base URL, per-node internal key,
  tags, version, enabled flag, and recent status metadata.
- Accept active child node registration.
- Accept child node heartbeat updates.
- Expose node, provider, key, model, explain, and health management APIs to ai_router.
- Forward credential management requests to the selected child node.
- Aggregate child node management capabilities for the ai_router UI.
- Degrade per node when a child node is unavailable, without failing unrelated nodes.

The gateway must not store credential payloads as authoritative data. Credential data
stays in the child node that owns it.

### Child Node

- Keep existing KeyFlow responsibilities: provider plugins, key status, pool handling,
  model synchronization, key-level scheduling, and leases.
- Store credentials locally in SQLite for v1 deployment.
- Expose existing management APIs protected by `X-Internal-Key`.
- Register itself with the gateway on startup.
- Send periodic heartbeats to the gateway.

### Child Node Gateway Client

v1 includes a small gateway client inside the existing KeyFlow child node service. It is enabled
only when all required gateway registration settings are present.

Suggested child-node environment variables:

```env
GATEWAY_URL=http://keyflow-gateway:8000
GATEWAY_REGISTER_KEY=...
NODE_ID=node-shanghai-01
NODE_DISPLAY_NAME=Shanghai Node 01
NODE_PUBLIC_BASE_URL=http://keyflow-node-01:8000
NODE_TAGS=shanghai,telecom
NODE_HEARTBEAT_INTERVAL_SECONDS=30
```

Behavior:

- Enable the gateway client only when `GATEWAY_URL`, `GATEWAY_REGISTER_KEY`,
  `NODE_ID`, and `NODE_PUBLIC_BASE_URL` are all configured. If any required value
  is missing, keep the client fully disabled.
- On startup, register with the gateway before the first heartbeat loop iteration.
- If registration fails, retry in the background with exponential backoff capped at 60 seconds.
- Send heartbeat every `NODE_HEARTBEAT_INTERVAL_SECONDS`.
- If heartbeat returns `404 node_not_found`, immediately run registration again.
- Registration and heartbeat failures must not stop the child node's local allocate/report/admin
  APIs.
- The client sends the child node's existing `INTERNAL_API_KEY` as `internal_key` during
  registration.

### ai_router

- Uses only the gateway for management UI data and credential CRUD.
- Does not see child node real base URLs or internal keys.
- Does not call child node management APIs directly.

### Executor

- Calls the matching child node directly for `allocate-key`, `allocate-by-model`,
  `report-success`, and `report-error`.
- Does not use the gateway for runtime scheduling.

## Authentication

v1 uses two separate secrets:

- `GATEWAY_INTERNAL_KEY`: ai_router uses this to call gateway management APIs.
- `GATEWAY_REGISTER_KEY`: child nodes use this to register and heartbeat with the gateway.

Each child node keeps its own `INTERNAL_API_KEY`. During registration, the child node sends
that key to the gateway. The gateway stores it and uses it as `X-Internal-Key` when forwarding
management requests to that child node.

Repeated registration for an existing `node_id` is allowed to update `base_url`, `internal_key`,
tags, and version. This keeps v1 operations simple and lets a child node rotate its
`INTERNAL_API_KEY` by re-registering itself. The protection boundary for v1 is the
`GATEWAY_REGISTER_KEY`; anyone who has it is trusted to register or update nodes. Later versions
can replace this with per-node registration tokens.

Gateway responses must never include child node internal keys. Node list responses expose only
`has_internal_key`.

This design leaves room for a later v2 token model where nodes register with a bootstrap token
and the gateway issues per-node access tokens.

## Data Model

v1 uses SQLite for gateway persistence. The repository boundary should be designed so PostgreSQL
can be added later without changing the API or service layer.

### `gateway_nodes`

```text
node_id       TEXT PRIMARY KEY
display_name  TEXT NOT NULL
base_url      TEXT NOT NULL
internal_key  TEXT NOT NULL
tags_json     TEXT NOT NULL DEFAULT '[]'
enabled       INTEGER NOT NULL DEFAULT 1
version       TEXT NULL
registered_at DATETIME NULL
last_heartbeat_at DATETIME NULL
last_runtime_status TEXT NULL
last_probe_at DATETIME NULL
last_probe_status TEXT NOT NULL DEFAULT 'unknown'
last_probe_error TEXT NULL
created_at    DATETIME NOT NULL
updated_at    DATETIME NOT NULL
```

Rules:

- `node_id` is stable across child node restarts.
- `base_url` is for gateway internal use only.
- `internal_key` is never returned by APIs and must not be logged.
- `enabled=false` disables forwarding and capability aggregation for the node.
- Registration creates the node if missing and updates connection/runtime metadata if present.
- Registration must not automatically re-enable a disabled node.
- Registration may replace an existing node's `base_url` and `internal_key` when the request has
  a valid `GATEWAY_REGISTER_KEY`.
- Registration, heartbeat, and child health probing have separate storage fields. Do not collapse
  them into one status field.
- `base_url` must be normalized before storage: allow only `http` or `https`, require a host,
  reject path, query string, and fragment, and remove trailing slashes. v1 accepts only an origin
  such as `http://keyflow-node-01:8000`; it does not support a base path.

Future runtime mode configuration:

```env
APP_NAME=KeyFlow Gateway
APP_VERSION=0.1.0
API_PREFIX=/api/gateway
KEYFLOW_RUNTIME_MODE=local
LOCAL_SQLITE_PATH=data/keyflow_gateway.db

# future:
# KEYFLOW_RUNTIME_MODE=dev
# DATABASE_URL_WRITE=postgresql+asyncpg://...
```

## Gateway API Contract

All management APIs except node registration and heartbeat require:

```http
X-Gateway-Internal-Key: <gateway_internal_key>
```

Node registration and heartbeat require:

```http
X-Gateway-Register-Key: <gateway_register_key>
```

### Register Node

```http
POST /api/gateway/nodes/register
```

```json
{
  "node_id": "node-shanghai-01",
  "display_name": "Shanghai Node 01",
  "base_url": "http://keyflow-node-01:8000",
  "internal_key": "node-specific-internal-key",
  "tags": ["shanghai", "telecom"],
  "version": "1.1.0"
}
```

Behavior:

- Insert a new node when `node_id` is unknown.
- Update base URL, internal key, tags, version, `registered_at`, and `updated_at` when known.
- Validate and normalize `base_url` before storage. Invalid URLs return `422` validation errors.
- Preserve existing `enabled=false`.
- Do not update heartbeat or probe fields from this endpoint.

### Heartbeat

```http
POST /api/gateway/nodes/{node_id}/heartbeat
```

```json
{
  "version": "1.1.0",
  "runtime_status": "running"
}
```

Behavior:

- Return `404 node_not_found` when the node is unknown. The child node should then register again.
- Update `last_heartbeat_at`, `last_runtime_status`, `version`, and `updated_at`.
- Do not update registration fields or probe fields from this endpoint.
- Accept heartbeat from disabled nodes, but keep them disabled for management forwarding.

Suggested defaults:

```env
GATEWAY_HEARTBEAT_INTERVAL_SECONDS=30
GATEWAY_HEARTBEAT_TIMEOUT_SECONDS=90
GATEWAY_NODE_HTTP_CONNECT_TIMEOUT_SECONDS=1
GATEWAY_NODE_HTTP_READ_TIMEOUT_SECONDS=5
GATEWAY_NODE_PROBE_CACHE_SECONDS=15
```

### List Nodes

```http
GET /api/gateway/nodes
```

Response omits internal keys and real child-node connection secrets:

```json
[
  {
    "node_id": "node-shanghai-01",
    "display_name": "Shanghai Node 01",
    "tags": ["shanghai", "telecom"],
    "enabled": true,
    "status": "online",
    "registered_at": "2026-05-19T09:59:00Z",
    "last_heartbeat_at": "2026-05-19T10:00:00Z",
    "last_probe_status": "healthy",
    "last_probe_at": "2026-05-19T10:00:00Z",
    "has_internal_key": true
  }
]
```

`GET /api/gateway/nodes` does not perform live child-node probing. It computes `status` from
stored management state and heartbeat freshness:

- `disabled` when `enabled=false`.
- `online` when the last heartbeat is within `GATEWAY_HEARTBEAT_TIMEOUT_SECONDS`.
- `stale` when the heartbeat timeout is exceeded.
- `unknown` when the node has never sent a heartbeat.

`last_probe_status` and `last_probe_at` are returned as cached diagnostics from the most recent
capabilities or explicit probe call. They do not control the node-list `status`.

The node list must not return child `base_url` or `internal_key`. v1 returns no address hint. A
future UI can add a deliberately redacted host hint if operations need it, but the real URL stays
gateway-internal.

### Update Node

```http
PATCH /api/gateway/nodes/{node_id}
```

```json
{
  "display_name": "Shanghai Node 01",
  "tags": ["shanghai", "telecom"],
  "enabled": true
}
```

Behavior:

- Validate `X-Gateway-Internal-Key`.
- Return `404 node_not_found` when the node is unknown.
- Update only management metadata: `display_name`, `tags`, and `enabled`.
- Do not update `base_url` or `internal_key` through this endpoint.
- Return the updated node view without secrets.

### Capabilities

```http
GET /api/gateway/capabilities
```

The gateway calls enabled child nodes' `/health` and `/api/providers`, then returns a management
resource tree for ai_router:

- `/health` is called without child-node authentication.
- `/api/providers` is called with the child node's stored `internal_key` as `X-Internal-Key`.
- Disabled nodes are included in the response with `status=disabled`, `providers=[]`, and no
  child-node probing.

```json
{
  "nodes": [
    {
      "node_id": "node-shanghai-01",
      "display_name": "Shanghai Node 01",
      "status": "healthy",
      "providers": [
        {
          "name": "gemini_oauth",
          "available": true,
          "auth_type": "oauth_json",
          "model_source": "remote",
          "credential_hint": "{\"access_token\":\"...\"}",
          "actions": [
            "create_key",
            "list_keys",
            "update_key",
            "delete_key",
            "move_pool",
            "models",
            "explain"
          ]
        }
      ],
      "error": null
    }
  ]
}
```

If one node fails, only that node is degraded:

```json
{
  "node_id": "node-shanghai-01",
  "display_name": "Shanghai Node 01",
  "status": "timeout",
  "providers": [],
  "error": "node_timeout"
}
```

Disabled node example:

```json
{
  "node_id": "node-shanghai-01",
  "display_name": "Shanghai Node 01",
  "status": "disabled",
  "providers": [],
  "error": null
}
```

Capabilities probing updates only `last_probe_at`, `last_probe_status`, and `last_probe_error`.
It does not update registration or heartbeat fields.

Probe results are cached per node for `GATEWAY_NODE_PROBE_CACHE_SECONDS`. A capabilities request
does not probe or write probe fields while cached probe data is fresh. When the cache is stale,
the gateway probes the node once and writes the new `last_probe_at`, `last_probe_status`, and
`last_probe_error` result. This avoids writing to SQLite on every management-page refresh.

### Forwarded Credential Management APIs

Gateway paths add `node_id` to the existing child-node management shape:

```http
POST   /api/gateway/nodes/{node_id}/providers/{provider}/keys
GET    /api/gateway/nodes/{node_id}/providers/{provider}/keys
GET    /api/gateway/nodes/{node_id}/keys/{key_id}
PUT    /api/gateway/nodes/{node_id}/keys/{key_id}
PUT    /api/gateway/nodes/{node_id}/keys/{key_id}/pool
DELETE /api/gateway/nodes/{node_id}/keys/{key_id}
GET    /api/gateway/nodes/{node_id}/providers/{provider}/keys/{key_id}/models
GET    /api/gateway/nodes/{node_id}/keys/{key_id}/explain
```

Forwarding behavior:

- Validate `X-Gateway-Internal-Key`.
- Load the node by `node_id`.
- Reject unknown nodes with `404 node_not_found`.
- Reject disabled nodes with `409 node_disabled`.
- Forward to the child node using its stored `base_url` and `X-Internal-Key`.
- Preserve child node status codes and `detail` values for business errors.
- Apply strict connect/read timeouts.

The gateway may forward credential payloads because these are explicit management actions, but
request/response logging must redact credential fields.

## Status And Error Semantics

Gateway-owned errors:

```text
401 {"detail": "invalid gateway internal key"}
401 {"detail": "invalid gateway register key"}
404 {"detail": "node_not_found"}
409 {"detail": "node_disabled"}
503 {"detail": "node_unreachable"}
504 {"detail": "node_timeout"}
```

Child-node business errors are passed through when forwarding:

```text
provider_not_found
key_not_found
duplicate_credential
provider_not_ready
upstream_unreachable
no_available_key
```

Node-list `status` values:

```text
unknown       Node has never sent a heartbeat.
online        Last heartbeat is fresh.
stale         Heartbeat timeout exceeded.
disabled      Node is administratively disabled.
```

Capabilities and probe `status` values:

```text
unknown       Gateway has not probed the node yet.
healthy       Child /health returned 200.
degraded      Child /health returned 503 but node is reachable.
unreachable   Connection failed.
timeout       Request timed out.
disabled      Node is administratively disabled.
```

`disabled` takes precedence over runtime status in management responses.

## Testing Scope

v1 should include focused tests for:

- Node registration inserts a new node.
- Repeated registration updates base URL, internal key, tags, and version.
- Repeated registration does not re-enable a disabled node.
- Registration normalizes valid `base_url` values and rejects invalid ones.
- Child node gateway client registers on startup, retries failed registration, sends heartbeats,
  and re-registers after heartbeat returns `node_not_found`.
- Node list responses do not expose `internal_key`.
- Node list responses do not expose child `base_url`.
- Node list does not perform live child-node probing.
- Heartbeat updates `last_heartbeat_at`, `last_runtime_status`, and version.
- Heartbeat for an unknown node returns `node_not_found`.
- Node update can change `display_name`, `tags`, and `enabled` but cannot change `base_url` or
  `internal_key`.
- Capabilities aggregation returns providers for healthy nodes.
- Capabilities calls child `/api/providers` with that node's `X-Internal-Key`.
- Capabilities aggregation degrades failed nodes without failing the entire response.
- Capabilities includes disabled nodes as `status=disabled` with empty providers and does not
  probe them.
- Capabilities updates probe fields without changing registration or heartbeat fields.
- Capabilities reuses fresh probe cache and avoids SQLite writes on every request.
- Forwarded create/list/get/update/move/delete/model/explain routes use the child node's
  `X-Internal-Key`.
- Child-node business errors are preserved.
- Gateway auth rejects invalid `X-Gateway-Internal-Key`.
- Registration and heartbeat reject invalid `X-Gateway-Register-Key`.
- Timeout and unreachable child-node calls return gateway-owned errors.

## Non-Goals For v1

- No runtime allocation through the gateway.
- No gateway-level scheduling or health scoring for allocation.
- No gateway-level credential authority or credential replication.
- No cross-node key migration.
- No PostgreSQL implementation yet, only a repository boundary that allows it later.
- No gateway-issued per-node token yet; v1 uses child node `INTERNAL_API_KEY`.
