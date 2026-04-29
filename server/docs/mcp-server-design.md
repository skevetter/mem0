# MCP Server for Self-Hosted Mem0 — Architecture Design

## Status

Draft

## Context

The self-hosted mem0 server (`server/`) exposes a REST API (FastAPI on :8888) for memory operations but has no MCP (Model Context Protocol) endpoint. AI editors (Claude Code, Cursor, Codex) connect to mem0 via MCP, and currently the only MCP option is the hosted platform at `mcp.mem0.ai`. Adding MCP support to the self-hosted server enables local-first memory workflows without depending on the hosted platform.

The repo contains two existing MCP implementations that inform this design:
1. `openmemory/api/app/mcp_server.py` — 574-line reference implementation with FastMCP + Streamable HTTP, mounted on FastAPI via `APIRouter(prefix="/mcp")`
2. `mem0-plugin/` — client-side MCP config files (`.mcp.json`) pointing to the hosted platform, defining 9 standard tools

## Decision

Mount an MCP endpoint on the existing FastAPI app using the `APIRouter` pattern from `openmemory/api/`. Expose all 9 platform-standard MCP tools, delegating each to the existing `mem0.Memory` instance via `server_state.get_memory_instance()`. Use Streamable HTTP transport only (SSE is deprecated). Reuse the server's existing auth model (API key via `Authorization` header) for MCP requests.

## Architecture

### Components

```
┌─────────────────────────────────────────────────┐
│  FastAPI App (server/main.py)                   │
│                                                 │
│  ┌──────────────┐  ┌─────────────────────────┐  │
│  │ REST Routes   │  │ MCP Router (/mcp)       │  │
│  │ /memories     │  │                         │  │
│  │ /search       │  │ POST /mcp               │  │
│  │ /entities     │  │ GET  /mcp               │  │
│  │ /auth         │  │ DELETE /mcp             │  │
│  │ /configure    │  │                         │  │
│  └──────┬───────┘  └────────┬────────────────┘  │
│         │                   │                    │
│         ▼                   ▼                    │
│  ┌─────────────────────────────────────────┐     │
│  │   server_state.get_memory_instance()    │     │
│  │   (mem0.Memory — pgvector backend)      │     │
│  └─────────────────────────────────────────┘     │
│                                                  │
│  Auth: verify_auth() — JWT / API Key / Admin Key │
│  CORS: CORSMiddleware (outermost)                │
└──────────────────────────────────────────────────┘
```

REST routes and MCP routes share the same `Memory` instance, the same pgvector backend, and the same auth middleware. The MCP endpoint runs stateless — each JSON-RPC request creates a fresh `StreamableHTTPServerTransport`, executes the tool, and returns the response.

### New Files

| File | Purpose | Lines (est.) |
|------|---------|-------------|
| `server/mcp_server.py` | `FastMCP` instance, 9 `@mcp.tool()` definitions, Streamable HTTP handler, `setup_mcp(app)` entry point | ~250 |

### Modified Files

| File | Change |
|------|--------|
| `server/main.py` | Import and call `setup_mcp(app)` after CORS middleware, add `/mcp` to `SKIPPED_REQUEST_LOG_PREFIXES` |
| `server/requirements.txt` | Add `mcp[cli]>=1.6.0` |
| `server/docker-compose.yaml` | No port changes needed — MCP runs on the same :8888 port |
| `server/.env.example` | Document `MCP_ENABLED` env var |

### Data Flow

1. Claude Code sends `POST /mcp` with JSON-RPC body (`tools/list`, `tools/call`, etc.) and `Authorization: Bearer <api-key>` header
2. FastAPI routes to `handle_mcp_request()` in `mcp_server.py`
3. Handler extracts and validates auth from the `Authorization` header using `verify_auth()`
4. Handler creates a `StreamableHTTPServerTransport(mcp_session_id=None, is_json_response_enabled=True)`
5. Transport parses JSON-RPC, dispatches to the matching `@mcp.tool()` function
6. Tool function calls `get_memory_instance().add()` / `.search()` / `.get()` / etc.
7. `Memory` instance executes against pgvector (embeddings via Bedrock Titan v2, LLM via Bedrock Claude Sonnet)
8. Result returns through transport → JSON-RPC response → HTTP 200

### MCP Tools

All 9 tools match the platform standard at `mcp.mem0.ai`, ensuring client-side MCP configs (`.mcp.json`) work with both hosted and self-hosted servers by changing only the URL.

| # | Tool | Parameters | Delegates to |
|---|------|-----------|-------------|
| 1 | `add_memory` | `messages: list[dict]`, `user_id: str`, `agent_id: str?`, `run_id: str?`, `metadata: dict?` | `Memory.add()` |
| 2 | `search_memories` | `query: str`, `user_id: str?`, `agent_id: str?`, `run_id: str?`, `limit: int? = 10` | `Memory.search()` |
| 3 | `get_memories` | `user_id: str?`, `agent_id: str?`, `run_id: str?` | `Memory.get_all()` |
| 4 | `get_memory` | `memory_id: str` | `Memory.get()` |
| 5 | `update_memory` | `memory_id: str`, `text: str`, `metadata: dict?` | `Memory.update()` |
| 6 | `delete_memory` | `memory_id: str` | `Memory.delete()` |
| 7 | `delete_all_memories` | `user_id: str?`, `agent_id: str?`, `run_id: str?` | `Memory.delete_all()` |
| 8 | `delete_entities` | `entity_type: str`, `entity_id: str` | Entities router logic (vector_store delete + optional graph reset) |
| 9 | `list_entities` | `entity_type: str? = "user"` | Entities router logic (vector_store.list + grouping) |

### API Surface

Single endpoint, three HTTP methods:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/mcp` | JSON-RPC requests (`initialize`, `tools/list`, `tools/call`) |
| `GET` | `/mcp` | Server-sent events stream (optional, for future streaming responses) |
| `DELETE` | `/mcp` | Session termination (no-op in stateless mode, returns 405) |

### Authentication Model

Reuse the server's existing `verify_auth()` function. MCP clients pass credentials via HTTP headers, which the MCP handler extracts before dispatching to the transport.

| Auth Method | Header | MCP Config |
|-------------|--------|-----------|
| Per-user API Key | `Authorization: Bearer m0sk_...` | `"headers": {"Authorization": "Bearer ${MEM0_API_KEY}"}` |
| Admin API Key | `Authorization: Bearer <admin-key>` | Same format |
| JWT | `Authorization: Bearer <jwt-token>` | Same format |
| Auth disabled | (none) | No headers needed |

Claude Code `.mcp.json` example for self-hosted:
```json
{
  "mcpServers": {
    "mem0-local": {
      "type": "http",
      "url": "http://localhost:8888/mcp",
      "headers": {
        "Authorization": "Bearer ${MEM0_API_KEY}"
      }
    }
  }
}
```

The handler converts the `Authorization` header into a FastAPI `Request` object that `verify_auth()` can process, reusing the full auth chain (JWT decode → API key lookup → admin key check → auth-disabled bypass).

### Transport

**Streamable HTTP only.** SSE is deprecated by the MCP spec (2025-03-26+). Claude Code, Cursor, and Codex all support `type: "http"` transport.

The handler follows the `openmemory/api/app/mcp_server.py:496-566` pattern:
1. Create `StreamableHTTPServerTransport(mcp_session_id=None, is_json_response_enabled=True)` per request
2. Use `anyio.create_task_group()` to run the MCP server and handle the request concurrently
3. Intercept ASGI `send` via `capture_send()` to avoid FastAPI's double-response bug
4. Return the captured response as a `starlette.responses.Response`

Stateless mode (`stateless=True`) enables horizontal scaling — no session affinity needed.

### Feature Flag

`MCP_ENABLED` env var (default: `true`). When `false`, the MCP router is not mounted and `/mcp` returns 404. This allows operators to disable MCP without rebuilding the image.

```python
MCP_ENABLED = os.environ.get("MCP_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
```

### Bedrock Compatibility

MCP tools call `get_memory_instance()` which returns the pre-configured `mem0.Memory` singleton. The Bedrock LLM (Claude Sonnet 4) and embedder (Titan v2) are configured via the server's existing `DEFAULT_CONFIG` / DB-stored config in `server_state.py`. MCP tools do not interact with LLM/embedder directly — they delegate to `Memory.add()`, `Memory.search()`, etc., which handle embedding and LLM calls internally.

No Bedrock-specific code is needed in the MCP layer.

### Docker Compose Changes

None required for basic operation. MCP runs on the same port (8888) as the REST API. The existing Docker Compose config already exposes :8888 and the `~/.aws:ro` volume mount provides Bedrock credentials.

Optional additions to `.env.example`:
```
MCP_ENABLED=true
```

### CORS

MCP Streamable HTTP clients send requests with `Accept: application/json, text/event-stream`. The existing `CORSMiddleware` allows all methods and headers for the dashboard origin. For MCP clients running on `localhost` (e.g., Claude Code), CORS is not an issue (same-origin or non-browser).

For remote MCP clients, the `allow_origins` list may need to include the MCP client's origin or `*`. This is a deployment-time configuration, not a code change.

## Alternatives Considered

| Approach | Pros | Cons | Why Not |
|----------|------|------|---------|
| **A: APIRouter mount** (chosen) | Single process, shared Memory instance, reuses auth, follows openmemory pattern, no new ports | Couples MCP to FastAPI lifecycle, `capture_send` ASGI interception is non-trivial | **Selected** — simplest, fewest moving parts |
| **B: Standalone MCP sidecar** | Independent process, separate scaling, crash isolation | New Docker service, new port (8889?), must proxy auth to REST API or share DB, doubles memory footprint | Over-engineered for a single-tenant self-hosted server |
| **C: `mcp.streamable_http_app()` ASGI mount** | Cleaner SDK integration, no manual transport handling | Requires FastAPI lifespan changes (`mcp.session_manager.run()`), less control over auth injection, harder to pass user identity to tools | Lifespan change is invasive; auth injection requires middleware that the high-level API doesn't expose |

## Constraints

- **Branch**: h3 (CEO's fork)
- **Python**: 3.12 (server Dockerfile)
- **Package manager**: uv (no Poetry)
- **AWS mount**: `~/.aws:/root/.aws:ro` in Docker — read-only, Bedrock credentials
- **No OpenAI fallback**: Bedrock is the only LLM/embedder provider
- **Auth model**: Existing JWT/API-key/admin-key chain must be preserved for MCP

## Migration Path

1. **Phase 1 (this PR)**: Add `mcp_server.py` with 9 tools, mount on existing FastAPI, add `mcp[cli]>=1.6.0` to requirements.txt. Feature-flagged via `MCP_ENABLED`.
2. **Phase 2 (future)**: Add stdio transport entry point (`python -m server.mcp_server`) for local Claude Code usage without a running server.
3. **Phase 3 (future)**: Add MCP resources (expose memory collections as browsable resources) and prompts (pre-built prompt templates for memory operations).

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `capture_send` ASGI pattern breaks on FastAPI/Starlette upgrade | Low | High — MCP endpoint stops working | Pin Starlette version; add integration test that exercises the full JSON-RPC round-trip |
| MCP SDK breaking changes (v1.x → v2.x) | Medium | Medium — tools stop registering | Pin `mcp[cli]>=1.6.0,<2.0` |
| Auth extraction from MCP request fails for edge cases | Low | Medium — unauthorized access or false rejections | Test all 4 auth modes (JWT, API key, admin key, disabled) with MCP client |
| Memory instance not initialized when MCP request arrives | Low | Low — returns clear error | Check `get_memory_instance()` at tool invocation, return MCP error content if None |
| CORS blocks remote MCP clients | Medium | Low — config-only fix | Document CORS configuration for remote MCP access |

## Testing Strategy

| Test | Type | Validates |
|------|------|-----------|
| JSON-RPC `initialize` → `tools/list` round-trip | Integration | Transport setup, tool registration |
| Each of 9 tools with valid params | Integration | Tool delegation to Memory instance |
| Auth: API key, JWT, admin key, disabled | Integration | Auth extraction from MCP request headers |
| Auth: missing/invalid credentials | Integration | Proper 401/403 error response |
| Concurrent MCP requests | Load | Stateless mode handles parallel requests |
| `MCP_ENABLED=false` → 404 | Unit | Feature flag works |

## Dependencies

| Package | Version | Why |
|---------|---------|-----|
| `mcp[cli]` | `>=1.6.0,<2.0` | MCP SDK — `FastMCP`, `StreamableHTTPServerTransport`. Floor at 1.6.0 for stable Streamable HTTP. Ceiling at <2.0 to avoid breaking changes. |
| `anyio` | (transitive via mcp) | Task group for concurrent transport handling |

No other new dependencies. `FastMCP`, `StreamableHTTPServerTransport`, `anyio` are all provided by or transitively required by `mcp[cli]`.
