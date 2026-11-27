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
"""Resolve `# suggest:` review routing for the GitHub and GitLab adapters.

The membership validator also imports the parser. Path matching uses pinned
micromatch configured for gitignore-style CODEOWNERS patterns (see
review-matcher/match.cjs).

Convention: a normal CODEOWNERS line is enforced (GitHub reads it, branch
protection can require it). A `# suggest: <pattern> @owner ...` line uses
the same path-pattern syntax but is a comment, so GitHub's own CODEOWNERS
parser ignores it entirely -- it can never become a required reviewer. It's
meant to be read by a workflow that requests those owners as PR reviewers
without blocking merge.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_SUGGEST_PREFIX = re.compile(r"^#\s*suggest:\s*(.+)$")


def read_file_at_ref(ref: str, path: str) -> str | None:
    """Returns `path` as committed at `ref` (read from git, not the working
    tree), or None if the ref or the file doesn't exist there."""
    result = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True, encoding="utf-8")
    return result.stdout if result.returncode == 0 else None


_LINE_TERMINATORS = re.compile(r"[\n\r\v\f\x85\u2028\u2029]")


def to_glob(pattern: str) -> list[str]:
    """Convert one CODEOWNERS-style pattern into micromatch glob strings.

    Preserve the existing CODEOWNERS/.gitignore
    semantics:
      - a leading "/" (or any "/" at all) anchors the pattern to the repo root
      - a trailing "/" is a directory pattern: matches that directory and
        everything under it
      - a pattern without a trailing "/" names a file or a directory, so it
        also matches everything under it ("/src" owns "src/a.py"); only the
        explicit "dir/*" and "dir/**" forms are left as written ("/src/sec**"
        is a basename wildcard and still owns descendants)
      - a pattern with no "/" anywhere matches at any depth (basename match)
      - "**"/"*" keep their normal glob meaning; micromatch's own "**" already
        handles the zero-or-more-directories case correctly
      - "(", ")" and "|" are literal characters, as in gitignore
    Returns a list because each case needs several alternatives (root-level
    and nested, the path itself and its descendants).
    """
    anchored = pattern.startswith("/")
    p = pattern[1:] if anchored else pattern
    dir_only = p.endswith("/")
    if dir_only:
        p = p[:-1]
    # Parentheses and pipes are literal in CODEOWNERS; micromatch would read them as
    # groups. Quote the unescaped ones (an even number of preceding backslashes).
    p = re.sub(r"(?<!\\)((?:\\\\)*)([()|])", r"\1\\\2", p)

    if p == "*" and not dir_only:
        return ["**"]

    if dir_only:
        # Trailing "**" components name any directory ("**/", "/src/**/"), so they must
        # consume at least one directory component; micromatch's "**" may consume none.
        # Only an exact "**" component is a globstar: "***" and "\**" are ordinary stars.
        parts = p.split("/")
        if parts[-1] == "**":
            while parts and parts[-1] == "**":
                parts.pop()
            parts.append("*")
        globs = ["/".join(parts) + "/**"]
    elif p.endswith(("/*", "/**")):
        globs = [p]
    else:
        globs = [p, f"{p}/**"]

    if anchored or "/" in p:
        return globs
    # Unanchored, no "/" anywhere in the pattern (e.g. "docker/" or a bare
    # filename like "package.json") -- matches at any depth, not just the root.
    return globs + [f"**/{glob}" for glob in globs]


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


def match_rules(rules: list[dict], files: list[str]) -> list[dict]:
    """Match rules with the pinned engine used by both provider adapters.

    Args:
        rules: Parsed ownership or suggestion rules in file order.
        files: Repository-relative changed paths, supplied as data on stdin.

    Returns:
        Rules annotated with matching files, preserving their order.
    """
    matcher = Path(__file__).parent / "review-matcher" / "match.cjs"
    payload = {"files": files, "patterns": [to_glob(rule["pattern"]) for rule in rules]}
    result = subprocess.run(
        ["node", str(matcher)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return [dict(rule, matched_files=matches) for rule, matches in zip(rules, json.loads(result.stdout), strict=True)]


def suggested_owners(text: str, files: list[str]) -> list[str]:
    """Resolve advisory owners, suppressing enforced duplicates per file.

    Args:
        text: Ownership rules from the trusted GitHub branch.
        files: Changed paths, using the GitHub all_changed_files convention.

    Returns:
        Sorted unique suggested user or team mentions.
    """
    parsed = parse_codeowners(text)
    enforced = resolve_last_match_per_file(match_rules(parsed["enforced_rules"], files))
    suggested = resolve_last_match_per_file(match_rules(parsed["suggest_rules"], files))
    return sorted(
        {
            owner
            for path, rule in suggested.items()
            for owner in rule["owners"]
            if owner not in enforced.get(path, {}).get("owners", [])
        }
    )


def enforced_owner_groups(text: str, files: list[str]) -> list[list[str]]:
    """Resolve the enforced owners GitHub would require for the changed files.

    Args:
        text: Ownership rules from the trusted GitHub branch.
        files: Changed paths, using the GitHub all_changed_files convention.

    Returns:
        Sorted distinct owner sets, one per winning enforced rule. GitHub
        accepts an approval from any owner of a file's last matching rule, so
        each set maps to one approval rule with one required approval. Mentions
        are case-insensitive on GitHub, so they are lower-cased. A rule without
        owners un-owns its files and contributes no set.

    Raises:
        ValueError: A path contains a line terminator, which glob engines do
            not match reliably; stop instead of treating the file as unowned.
    """
    unsupported = sorted(file for file in files if _LINE_TERMINATORS.search(file))
    if unsupported:
        raise ValueError(f"Paths with line terminators cannot be matched safely: {unsupported!r}")
    parsed = parse_codeowners(text)
    enforced = resolve_last_match_per_file(match_rules(parsed["enforced_rules"], files))
    groups = {tuple(sorted({owner.casefold() for owner in rule["owners"]})) for rule in enforced.values()} - {()}
    return [list(group) for group in sorted(groups)]


def changed_files(base_sha: str, head_sha: str) -> list[str]:
    """Read changed paths from the merge base with GitHub's deletion semantics.

    Args:
        base_sha: Full base commit SHA, not a ref supplied by a PR.
        head_sha: Full head commit SHA.

    Returns:
        Changed paths, excluding deletions as all_changed_files does today.
    """
    for sha in (base_sha, head_sha):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Expected a full commit SHA")
    base = subprocess.run(
        ["git", "merge-base", base_sha, head_sha],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", "-z", base, head_sha, "--"],
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8").rstrip("\0").split("\0") if result.stdout else []


def parse_rule_line(line: str) -> dict:
    """Split a rule into pattern and owners, dropping an inline `# comment`."""
    parts = line.strip().split()
    comment = next((i for i, part in enumerate(parts) if part.startswith("#")), len(parts))
    return {"pattern": parts[0], "owners": parts[1:comment]}


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
    nested = b["value"].startswith(a["value"] + "/")
    if a["type"] == "file":
        # "/src" names a file or a directory, so it also owns everything under it.
        return b["value"] == a["value"] or nested
    return (b["type"] == "dir" and b["value"] == a["value"]) or nested


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
