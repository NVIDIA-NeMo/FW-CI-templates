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
"""Shared parsing/matching logic for the `# suggest:` convention layered on
top of a repo's .github/CODEOWNERS file. Imported via sys.path insertion
from the python-script steps in _suggest_reviewers.yml and
_validate_codeowners_membership.yml, and unit tested directly in
test_codeowners_suggestions.py -- kept out of the workflow YAML so there is
one place to fix and one place to test.

Convention: a normal CODEOWNERS line is enforced (GitHub reads it, branch
protection can require it). A `# suggest: <pattern> @owner ...` line uses
the same path-pattern syntax but is a comment, so GitHub's own CODEOWNERS
parser ignores it entirely -- it can never become a required reviewer. It's
meant to be read by a workflow that requests those owners as PR reviewers
without blocking merge.
"""

from __future__ import annotations

import re

_SUGGEST_PREFIX = re.compile(r"^#\s*suggest:\s*(.+)$")


def to_glob(pattern: str) -> list[str]:
    """Converts one CODEOWNERS-style pattern into one or more minimatch-
    compatible glob strings (the syntax step-security/changed-files'
    `files_yaml:` input expects), preserving the same CODEOWNERS/.gitignore
    semantics:
      - a leading "/" (or any "/" at all) anchors the pattern to the repo root
      - a trailing "/" is a directory pattern: matches that directory and
        everything under it
      - a pattern with no "/" anywhere matches at any depth (basename match)
      - "**"/"*" keep their normal glob meaning; minimatch's own "**" already
        handles the zero-or-more-directories case correctly
    Returns a list because the "matches at any depth" case needs two
    alternatives (root-level and nested) rather than one pattern with
    branching.
    """
    anchored = pattern.startswith("/")
    p = pattern[1:] if anchored else pattern
    dir_only = p.endswith("/")
    if dir_only:
        p = p[:-1]

    if p == "*":
        return ["**"]

    with_dir_suffix = f"{p}/**" if dir_only else p

    if anchored or "/" in p:
        return [with_dir_suffix]
    # Unanchored, no "/" anywhere in the pattern (e.g. "docker/" or a bare
    # filename like "package.json") -- matches at any depth, not just the root.
    return [f"{p}/**", f"**/{p}/**"] if dir_only else [p, f"**/{p}"]


def resolve_last_match_per_file(rules_with_matches: list[dict]) -> dict[str, dict]:
    """CODEOWNERS/.gitignore semantics: the LAST matching rule wins for a
    path. Takes rules in FILE ORDER, each annotated with which changed files
    it matched (however that matching was actually done -- by us, or by an
    external tool like step-security/changed-files) and resolves each file
    to exactly one winning rule by walking the rules in order and letting
    each later match overwrite any earlier one for the same file.
    """
    winning_rule_by_file: dict[str, dict] = {}
    for rule in rules_with_matches:
        for filename in rule["matched_files"]:
            winning_rule_by_file[filename] = rule
    return winning_rule_by_file


def parse_rule_line(line: str) -> dict:
    parts = line.strip().split()
    return {"pattern": parts[0], "owners": parts[1:]}


def parse_codeowners(text: str) -> dict:
    """Splits a raw CODEOWNERS file into its two rule kinds:
      enforced_rules -- real, non-comment lines. GitHub reads these; branch
                        protection can require them.
      suggest_rules  -- `# suggest: <pattern> @owner ...` comment lines.
                        Invisible to GitHub's own CODEOWNERS parser;
                        advisory only.
    """
    lines = [line.strip() for line in text.split("\n")]

    enforced_rules = [parse_rule_line(line) for line in lines if line and not line.startswith("#")]

    suggest_rules = []
    for line in lines:
        m = _SUGGEST_PREFIX.match(line)
        if m and m.group(1).strip():
            suggest_rules.append(parse_rule_line(m.group(1).strip()))

    return {"enforced_rules": enforced_rules, "suggest_rules": suggest_rules}


def _normalize_pattern(pattern: str) -> dict:
    """Best-effort static "does pattern A's matched file set fully contain
    pattern B's" check, for the redundancy lint below. Only reasons about
    the simple anchored file/directory patterns and the bare `*` backstop --
    anything with an inner wildcard is reported as "unknown" rather than
    guessed at, so this only ever produces confident, low-noise warnings,
    never false positives from a pattern it doesn't understand.
    """
    p = pattern[1:] if pattern.startswith("/") else pattern
    if p == "*":
        return {"type": "wildcard"}
    if "*" in p:
        return {"type": "unknown"}
    if p.endswith("/"):
        return {"type": "dir", "value": p[:-1]}
    return {"type": "file", "value": p}


def pattern_covers(covering_pattern: str, covered_pattern: str) -> bool:
    a = _normalize_pattern(covering_pattern)
    b = _normalize_pattern(covered_pattern)
    if a["type"] == "unknown" or b["type"] == "unknown":
        return False
    if a["type"] == "wildcard":
        return True
    if b["type"] == "wildcard":
        return False
    if a["type"] == "file":
        return b["type"] == "file" and a["value"] == b["value"]
    return b["value"] == a["value"] or b["value"].startswith(a["value"] + "/")


def _same_owners(a: list[str], b: list[str]) -> bool:
    return len(a) == len(b) and set(a) == set(b)


def find_redundant_rules(enforced_rules: list[dict], suggest_rules: list[dict]) -> list[dict]:
    """Flags a later rule (enforced or suggested, in file order) as
    redundant when an earlier rule already covers its full path AND grants
    the exact same owners -- i.e. adding it changes nothing, since
    last-match-wins means the earlier rule already applied to every file the
    later one matches. Example: `docker/ @team` followed later by
    `# suggest: docker/scripts/ @team` is redundant; the same followed by
    `@other-team` is not (that's a deliberate, meaningful override).
    """
    all_rules = [{**r, "source": "enforced", "order": i} for i, r in enumerate(enforced_rules)] + [
        {**r, "source": "suggest", "order": i + len(enforced_rules)} for i, r in enumerate(suggest_rules)
    ]

    redundant = []
    for j in range(len(all_rules)):
        for i in range(j):
            earlier = all_rules[i]
            later = all_rules[j]
            if earlier["pattern"] == later["pattern"] and earlier["source"] == later["source"]:
                redundant.append({"rule": later, "covered_by": earlier, "reason": "exact duplicate pattern"})
                break
            if pattern_covers(earlier["pattern"], later["pattern"]) and _same_owners(
                earlier["owners"], later["owners"]
            ):
                redundant.append({"rule": later, "covered_by": earlier, "reason": "nested path, identical owners"})
                break
    return redundant
