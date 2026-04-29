import json
import logging
import os
import secrets
from typing import Any, Dict, List, Optional

import anyio
from fastapi import FastAPI, Request
from fastapi.routing import APIRouter
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.responses import JSONResponse, Response

from auth import (
    ADMIN_API_KEY,
    AUTH_DISABLED,
    _resolve_user_from_api_key,
    _resolve_user_from_jwt,
    decode_token,
)
from db import SessionLocal
from server_state import get_memory_instance

logger = logging.getLogger(__name__)

MCP_ENABLED = os.environ.get("MCP_ENABLED", "false").lower() in {"1", "true", "yes", "on"}

mcp = FastMCP("mem0-server", stateless_http=True, json_response=True)


# ---------------------------------------------------------------------------
# Auth helper — extracts credentials from raw HTTP headers, bypassing
# FastAPI's Depends() injection which is unavailable inside the MCP handler.
# ---------------------------------------------------------------------------

def _authenticate_request(request: Request) -> None:
    """Validate auth from the incoming HTTP request. Raises JSONResponse-ready exceptions."""
    if AUTH_DISABLED:
        return

    auth_header = request.headers.get("authorization", "")
    api_key = request.headers.get("x-api-key", "")

    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        if ADMIN_API_KEY and secrets.compare_digest(token, ADMIN_API_KEY):
            return
        with SessionLocal() as db:
            try:
                _resolve_user_from_api_key(token, db)
                return
            except Exception:
                pass
            try:
                _resolve_user_from_jwt(token, db)
                return
            except Exception:
                pass
        raise _auth_error()

    if api_key:
        if ADMIN_API_KEY and secrets.compare_digest(api_key, ADMIN_API_KEY):
            return
        with SessionLocal() as db:
            try:
                _resolve_user_from_api_key(api_key, db)
                return
            except Exception:
                pass
        raise _auth_error()

    raise _auth_error()


class _AuthError(Exception):
    pass


def _auth_error() -> _AuthError:
    return _AuthError("Authentication required. Provide Authorization: Bearer <token> header.")


# ---------------------------------------------------------------------------
# MCP Tools — 9 standard tools matching the platform at mcp.mem0.ai
# ---------------------------------------------------------------------------

@mcp.tool(description="Store new memories from messages. Requires at least one identifier (user_id, agent_id, or run_id).")
def add_memory(
    messages: List[Dict[str, str]],
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    infer: Optional[bool] = None,
) -> str:
    if not any([user_id, agent_id, run_id]):
        return json.dumps({"error": "At least one identifier (user_id, agent_id, run_id) is required."})
    params: Dict[str, Any] = {}
    if user_id:
        params["user_id"] = user_id
    if agent_id:
        params["agent_id"] = agent_id
    if run_id:
        params["run_id"] = run_id
    if metadata:
        params["metadata"] = metadata
    if infer is not None:
        params["infer"] = infer
    try:
        result = get_memory_instance().add(messages=messages, **params)
        return json.dumps(result, default=str)
    except Exception as exc:
        logger.exception("add_memory failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(description="Search memories by semantic query. Returns the most relevant memories.")
def search_memories(
    query: str,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: Optional[int] = 10,
) -> str:
    params: Dict[str, Any] = {}
    filters = {k: v for k, v in {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}.items() if v}
    if filters:
        params["filters"] = filters
    if limit:
        params["top_k"] = limit
    try:
        result = get_memory_instance().search(query=query, **params)
        return json.dumps(result, default=str)
    except Exception as exc:
        logger.exception("search_memories failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(description="List all memories, optionally filtered by user_id, agent_id, or run_id.")
def get_memories(
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> str:
    try:
        if not any([user_id, agent_id, run_id]):
            results = get_memory_instance().vector_store.list(top_k=1000)
            rows = results[0] if results and isinstance(results, list) and isinstance(results[0], list) else results or []
            memories = []
            for row in rows:
                payload = getattr(row, "payload", None) or {}
                memories.append({
                    "id": getattr(row, "id", None),
                    "memory": payload.get("data"),
                    "user_id": payload.get("user_id"),
                    "agent_id": payload.get("agent_id"),
                    "run_id": payload.get("run_id"),
                    "created_at": payload.get("created_at"),
                    "updated_at": payload.get("updated_at"),
                })
            return json.dumps({"results": memories}, default=str)
        params = {k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v}
        result = get_memory_instance().get_all(**params)
        return json.dumps(result, default=str)
    except Exception as exc:
        logger.exception("get_memories failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(description="Retrieve a specific memory by its ID.")
def get_memory(memory_id: str) -> str:
    try:
        result = get_memory_instance().get(memory_id)
        return json.dumps(result, default=str)
    except Exception as exc:
        logger.exception("get_memory failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(description="Update a memory's text content and optional metadata.")
def update_memory(
    memory_id: str,
    text: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    try:
        result = get_memory_instance().update(memory_id=memory_id, data=text, metadata=metadata)
        return json.dumps(result, default=str)
    except Exception as exc:
        logger.exception("update_memory failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(description="Delete a specific memory by its ID.")
def delete_memory(memory_id: str) -> str:
    try:
        get_memory_instance().delete(memory_id=memory_id)
        return json.dumps({"message": "Memory deleted successfully"})
    except Exception as exc:
        logger.exception("delete_memory failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(description="Delete all memories for a given identifier. Requires at least one of user_id, agent_id, or run_id.")
def delete_all_memories(
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> str:
    if not any([user_id, agent_id, run_id]):
        return json.dumps({"error": "At least one identifier (user_id, agent_id, run_id) is required."})
    params = {k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v}
    try:
        get_memory_instance().delete_all(**params)
        return json.dumps({"message": "All relevant memories deleted"})
    except Exception as exc:
        logger.exception("delete_all_memories failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(description="Delete an entity (user, agent, or run) and all its associated memories.")
def delete_entities(entity_type: str, entity_id: str) -> str:
    type_to_field = {"user": "user_id", "agent": "agent_id", "run": "run_id"}
    if entity_type not in type_to_field:
        return json.dumps({"error": f"Invalid entity_type '{entity_type}'. Must be one of: user, agent, run"})
    try:
        get_memory_instance().delete_all(**{type_to_field[entity_type]: entity_id})
        return json.dumps({"message": f"Entity '{entity_id}' of type '{entity_type}' deleted"})
    except Exception as exc:
        logger.exception("delete_entities failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(description="List entities (users, agents, or runs) that have stored memories.")
def list_entities(entity_type: Optional[str] = "user") -> str:
    from collections import defaultdict
    from datetime import datetime

    type_to_field = {"user": "user_id", "agent": "agent_id", "run": "run_id"}
    if entity_type and entity_type not in type_to_field:
        return json.dumps({"error": f"Invalid entity_type '{entity_type}'. Must be one of: user, agent, run"})

    scan_types = [entity_type] if entity_type else list(type_to_field.keys())

    try:
        results = get_memory_instance().vector_store.list(top_k=10_000)
        rows = results[0] if results and isinstance(results, list) and isinstance(results[0], list) else results or []

        buckets: Dict[tuple, Dict[str, Any]] = defaultdict(
            lambda: {"total_memories": 0, "created_at": None, "updated_at": None}
        )

        for row in rows:
            payload = getattr(row, "payload", None) or {}
            created_raw = payload.get("created_at")
            updated_raw = payload.get("updated_at")
            created = None
            updated = None
            if created_raw:
                try:
                    created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                except ValueError:
                    pass
            if updated_raw:
                try:
                    updated = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
                except ValueError:
                    pass
            updated = updated or created

            for et in scan_types:
                value = payload.get(type_to_field[et])
                if not value:
                    continue
                bucket = buckets[(et, str(value))]
                bucket["total_memories"] += 1
                if created and (bucket["created_at"] is None or created < bucket["created_at"]):
                    bucket["created_at"] = created
                if updated and (bucket["updated_at"] is None or updated > bucket["updated_at"]):
                    bucket["updated_at"] = updated

        entities = [
            {"id": eid, "type": etype, **data}
            for (etype, eid), data in sorted(buckets.items())
        ]
        return json.dumps(entities, default=str)
    except Exception as exc:
        logger.exception("list_entities failed")
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Streamable HTTP transport handler
# ---------------------------------------------------------------------------

mcp_router = APIRouter(tags=["mcp"])


@mcp_router.get("/mcp")
async def mcp_health():
    return {"status": "ok", "server": "mem0-mcp", "transport": "streamable-http"}


@mcp_router.api_route("/mcp", methods=["POST", "DELETE"])
async def handle_mcp_request(request: Request):
    try:
        _authenticate_request(request)
    except _AuthError as exc:
        return JSONResponse(status_code=401, content={"error": str(exc)})

    if request.method == "DELETE":
        return JSONResponse(status_code=405, content={"error": "Session termination not supported in stateless mode"})

    response_started = False
    response_status = 200
    response_headers: list[tuple[bytes, bytes]] = []
    response_body = bytearray()

    async def capture_send(message):
        nonlocal response_started, response_status
        if message["type"] == "http.response.start":
            response_started = True
            response_status = message["status"]
            response_headers.extend(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    try:
        transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=True,
        )

        async with anyio.create_task_group() as tg:

            async def run_server(*, task_status=anyio.TASK_STATUS_IGNORED):
                async with transport.connect() as (read_stream, write_stream):
                    task_status.started()
                    await mcp._mcp_server.run(
                        read_stream,
                        write_stream,
                        mcp._mcp_server.create_initialization_options(),
                        stateless=True,
                    )

            await tg.start(run_server)
            await transport.handle_request(request.scope, request.receive, capture_send)
            await transport.terminate()
            tg.cancel_scope.cancel()
    except Exception:
        logger.exception("MCP transport error")
        return JSONResponse(status_code=500, content={"error": "Internal MCP server error"})

    if not response_started:
        return Response(status_code=500, content=b"Transport did not produce a response")

    return Response(
        content=bytes(response_body),
        status_code=response_status,
        headers={k.decode(): v.decode() for k, v in response_headers},
    )


# ---------------------------------------------------------------------------
# Setup function — called from main.py
# ---------------------------------------------------------------------------

def setup_mcp(app: FastAPI) -> None:
    if not MCP_ENABLED:
        logger.info("MCP server disabled (MCP_ENABLED is not set to true)")
        return
    app.include_router(mcp_router)
    logger.info("MCP server enabled at /mcp (Streamable HTTP, stateless)")
