# Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Minimal MCP stdio adapter over the bounded retriever."""

from __future__ import annotations

from .contracts import *  # noqa: F403
from .retrieval import retriever

MCP_TOOLS = [
    {"name": "metadata", "description": "Get normalized pull-request metadata and captured revision", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "changed_files", "description": "List changed files with status and coverage", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "governing_base", "description": "List captured trusted BASE_SHA instructions and skills", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "tree", "description": "List a bounded captured repository tree", "inputSchema": {"type": "object", "additionalProperties": False, "required": ["snapshot"], "properties": {"snapshot": {"enum": ["base", "head"]}, "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "read", "description": "Read bounded UTF-8 text from a changed-file snapshot", "inputSchema": {"type": "object", "additionalProperties": False, "required": ["snapshot", "path"], "properties": {"snapshot": {"enum": ["base", "head"]}, "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "byte_limit": {"type": "integer", "minimum": 1, "maximum": 524288}}}},
    {"name": "diff", "description": "Read the immutable textual pull-request diff incrementally", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"offset": {"type": "integer", "minimum": 0}, "byte_limit": {"type": "integer", "minimum": 1, "maximum": 524288}}}},
    {"name": "trusted_base_read", "description": "Read bounded UTF-8 text from any captured trusted BASE_SHA regular file", "inputSchema": {"type": "object", "additionalProperties": False, "required": ["path"], "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "byte_limit": {"type": "integer", "minimum": 1, "maximum": 524288}}}},
    {"name": "trusted_base_search", "description": "Search bounded captured trusted BASE_SHA source for literal text", "inputSchema": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {"path": {"type": "string"}, "query": {"type": "string", "minLength": 1, "maxLength": 1000}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "coverage", "description": "Derive current completeness from the retrieval audit", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "search", "description": "Search captured changed-file snapshots for literal text", "inputSchema": {"type": "object", "additionalProperties": False, "required": ["snapshot", "query"], "properties": {"snapshot": {"enum": ["base", "head"]}, "query": {"type": "string", "minLength": 1, "maxLength": 1000}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "diff_hunks", "description": "List immutable diff hunks incrementally", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "history", "description": "List optional bounded trusted-base history", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}},
]


def mcp_tool_call(context: str, audit: str, name: str, arguments: Any) -> Any:
    if not isinstance(arguments, dict):
        raise ReviewError("MCP tool arguments must be an object")
    operation = name.replace("_", "-")
    known = {item["name"]: set(item["inputSchema"].get("properties", {})) for item in MCP_TOOLS}
    if name not in known or set(arguments) - known[name]:
        raise ReviewError("unknown MCP tool or argument")
    values = {
        "context": context,
        "audit": audit,
        "operation": operation,
        "snapshot": arguments.get("snapshot"),
        "path": arguments.get("path"),
        "query": arguments.get("query"),
        "offset": arguments.get("offset", 0),
        "limit": arguments.get("limit", 100),
        "byte_limit": arguments.get("byte_limit", 64 * 1024),
    }
    raw = io.BytesIO()
    text = io.TextIOWrapper(raw, encoding="utf-8")
    previous = sys.stdout
    try:
        sys.stdout = text
        retriever(argparse.Namespace(**values))
        text.flush()
    finally:
        sys.stdout = previous
    return json.loads(raw.getvalue())


def mcp_server(args: argparse.Namespace) -> None:
    """Serve only the audited retriever through MCP over stdio."""
    validate_manifest(Path(args.context).resolve())
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ReviewError("MCP request must be an object")
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                continue
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "review-context", "version": "1.0"}}
            elif method == "tools/list":
                result = {"tools": MCP_TOOLS}
            elif method == "tools/call":
                parameters = request.get("params") or {}
                value = mcp_tool_call(args.context, args.audit, parameters.get("name"), parameters.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}], "isError": False}
            elif method == "ping":
                result = {}
            else:
                raise ReviewError(f"unsupported MCP method: {method}")
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as error:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32000, "message": str(error)}}
        print(json.dumps(response, separators=(",", ":")), flush=True)
