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

"""Credential-minimal analyzer loop over the local audited retriever."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .context import validate_manifest
from .contracts import MAX_OUTPUT_BYTES, ReviewError
from .mcp import MCP_TOOLS, mcp_tool_call
from .utils import contained_path, read_json, write_json

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 120
MODEL_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _request(base_url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/v1/messages"
    parsed_url = urllib.parse.urlsplit(url)
    if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        raise ReviewError("inference base URL must use credential-free HTTPS authority")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-api-key": api_key,
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise ReviewError(f"inference request failed: {error}") from error
    if len(data) > MAX_RESPONSE_BYTES:
        raise ReviewError("inference response exceeds bounded size")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewError("inference response is not valid JSON") from error
    if not isinstance(value, dict):
        raise ReviewError("inference response must be an object")
    return value


def _assistant_content(response: dict[str, Any]) -> list[dict[str, Any]]:
    content = response.get("content")
    if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
        raise ReviewError("inference response content must be an array of objects")
    return content


def _tool_result(context: str, audit: str, block: dict[str, Any]) -> dict[str, Any]:
    tool_id = block.get("id")
    name = block.get("name")
    arguments = block.get("input")
    if not isinstance(tool_id, str) or not tool_id or not isinstance(name, str):
        raise ReviewError("invalid inference tool-use block")
    result = mcp_tool_call(context, audit, name, arguments)
    return {"type": "tool_result", "tool_use_id": tool_id, "content": json.dumps(result, separators=(",", ":"))}


def _structured_output(content: list[dict[str, Any]]) -> Any:
    tools = [block for block in content if block.get("type") == "tool_use"]
    if len(tools) != 1 or tools[0].get("name") != "submit_review":
        raise ReviewError("final inference response must call submit_review exactly once")
    return tools[0].get("input")


def analyze(args: argparse.Namespace) -> None:
    context = Path(args.context).resolve()
    validate_manifest(context)
    try:
        audit_relative = Path(args.audit).resolve().relative_to(context).as_posix()
    except ValueError as error:
        raise ReviewError("audit path escapes context root") from error
    audit = contained_path(context, audit_relative)
    prompt = Path(args.prompt).read_text(encoding="utf-8")
    if not prompt.strip() or len(prompt.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ReviewError("analysis prompt is empty or exceeds the bounded size")
    schema = read_json(Path(args.schema), max_bytes=MAX_OUTPUT_BYTES)
    if not isinstance(schema, dict):
        raise ReviewError("review output schema must be an object")
    if args.max_turns < 1 or args.max_turns > 128:
        raise ReviewError("max turns must be between 1 and 128")
    if not isinstance(args.model, str) or not MODEL_RE.fullmatch(args.model):
        raise ReviewError("model must be a bounded provider model identifier")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ReviewError("ANTHROPIC_API_KEY is required")

    retrieval_tools = [
        {"name": tool["name"], "description": tool["description"], "input_schema": tool["inputSchema"]}
        for tool in MCP_TOOLS
    ]
    tools = [
        *retrieval_tools,
        {
            "name": "submit_review",
            "description": "Return the final structured review; this does not publish it",
            "input_schema": schema,
        },
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    for _ in range(args.max_turns):
        payload = {
            "model": args.model,
            "max_tokens": 8192,
            "messages": json.loads(json.dumps(messages)),
            "tools": tools,
            "tool_choice": {"type": "any"},
        }
        response = _request(args.base_url, api_key, payload)
        content = _assistant_content(response)
        messages.append({"role": "assistant", "content": content})
        submit = [
            block
            for block in content
            if block.get("type") == "tool_use" and block.get("name") == "submit_review"
        ]
        if submit:
            write_json(Path(args.output), _structured_output(content))
            return
        calls = [block for block in content if block.get("type") == "tool_use"]
        if not calls:
            raise ReviewError("inference response neither retrieved context nor submitted a review")
        results = [_tool_result(str(context), str(audit), block) for block in calls]
        messages.append({"role": "user", "content": results})
    raise ReviewError("analysis turn budget exhausted")
