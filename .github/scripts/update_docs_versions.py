#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION.  All rights reserved.
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
"""Update optional documentation metadata without importing Sphinx configuration."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


STABLE_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
LATEST = re.compile(r"\s*\(latest\)", re.IGNORECASE)


def _stable_version(version: str) -> tuple[int, ...] | None:
    match = STABLE_VERSION.fullmatch(version)
    return tuple(map(int, match.groups())) if match else None


def _version_url(url: str, old_version: str, new_version: str, *, aliases_only: bool = False) -> str | None:
    versions = "latest" if aliases_only else f"{re.escape(old_version)}|latest|nightly"
    updated, count = re.subn(
        rf"(^|/)(?:{versions})(?=/|[?#]|$)", lambda match: match[1] + new_version, url, count=1
    )
    return updated if count else None


def _update_versions(entries: list[dict], version: str, publish_as_latest: bool) -> None:
    previous_versions = {
        entry["version"]
        for entry in entries
        if _stable_version(entry["version"]) is not None
        and (entry.get("preferred") is True or LATEST.search(entry.get("name", "")))
    }
    if not any(entry["version"] == version for entry in entries):
        url = next(
            (
                candidate
                for entry in entries
                if entry.get("url")
                and (candidate := _version_url(entry["url"], entry["version"], version))
            ),
            f"../{version}/",
        )
        index = 0
        while index < len(entries) and entries[index]["version"].lower() == "nightly":
            index += 1
        entries.insert(index, {"name": version, "version": version, "url": url})

    eligible = [
        index
        for index, entry in enumerate(entries)
        if _stable_version(entry["version"]) is not None
        and (publish_as_latest or entry["version"] in previous_versions)
    ]
    latest_index = max(eligible, key=lambda index: _stable_version(entries[index]["version"]), default=None)
    for index, entry in enumerate(entries):
        entry.pop("preferred", None)
        if "name" in entry:
            entry["name"] = LATEST.sub("", entry["name"]).strip()
        if index == latest_index:
            name = entry.get("name") or entry["version"]
            label, separator, calendar = name.partition(" · ")
            entry["name"] = f"{label} (latest){separator}{calendar}"
            entry["preferred"] = True
        elif entry["version"].lower() != "nightly" and entry.get("url"):
            immutable_url = _version_url(entry["url"], entry["version"], entry["version"], aliases_only=True)
            if immutable_url is not None:
                entry["url"] = immutable_url


def _json_content(value: object, original: str) -> str:
    if "\n" not in original.rstrip("\n"):
        return json.dumps(value, ensure_ascii=False) + ("\n" if original.endswith("\n") else "")
    indents = re.findall(r"^([ \t]+)\S", original, re.MULTILINE)
    indent = min(indents, key=len) if indents else "    "
    return json.dumps(value, ensure_ascii=False, indent=indent) + "\n"


def update_docs_versions(docs_dir: Path, version: str, publish_as_latest: bool = True) -> list[Path]:
    """Update existing version files and return absolute paths for git staging."""
    if not re.fullmatch(r"v?\d+\.\d+\.\d+[\w.+-]*", version):
        raise ValueError(f"Invalid documentation version: {version!r}")
    planned = []
    filenames = ("conf.py", "project.json", "versions1.json")
    for filename in filenames:
        path = docs_dir / filename
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        if filename == "conf.py":
            updated = re.sub(
                r'''(?m)^(release\s*=\s*)(["'])[^"'\n]*\2([ \t]*(?:#.*)?)$''',
                lambda match: f"{match[1]}{match[2]}{version}{match[2]}{match[3]}",
                original,
            )
        else:
            value = json.loads(original)
            if filename == "project.json":
                value["version"] = version
            else:
                _update_versions(value, version, publish_as_latest)
            # Leave formatting alone when no metadata changed.
            updated = original if value == json.loads(original) else _json_content(value, original)
        if updated != original:
            planned.append((path.resolve(), updated))
    for path, content in planned:
        path.write_text(content, encoding="utf-8")
    return [path for path, _ in planned]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--publish-as-latest", choices=("true", "false"), default="true")
    args = parser.parse_args()
    for path in update_docs_versions(args.docs_dir, args.version, args.publish_as_latest == "true"):
        print(path)


if __name__ == "__main__":
    main()
