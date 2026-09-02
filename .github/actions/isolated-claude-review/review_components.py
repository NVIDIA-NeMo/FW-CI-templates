#!/usr/bin/env python3
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

"""Compatibility entrypoint for isolated pull-request review components."""

from __future__ import annotations

import sys
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parent
if str(_TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOL_ROOT))

from reviewlib import analyzer  # noqa: E402,F401
from reviewlib.analyzer import analyze  # noqa: E402,F401
from reviewlib.cli import main  # noqa: E402,F401
from reviewlib.context import build_context, resolve_trusted_symlink, validate_manifest  # noqa: E402,F401
from reviewlib.contracts import MAX_REVIEW_BODY_BYTES, SCHEMA_VERSION, ReviewError  # noqa: E402,F401
from reviewlib.publisher import exchange_publisher_token, review_payload  # noqa: E402,F401
from reviewlib.retrieval import retrieval_coverage, retriever  # noqa: E402,F401
from reviewlib.utils import canonical_json, sha256_bytes, write_json  # noqa: E402,F401
from reviewlib.validation import validate_output_document  # noqa: E402,F401


if __name__ == "__main__":
    main()
