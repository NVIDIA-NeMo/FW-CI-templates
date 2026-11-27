#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Attach GitLab review pools resolved from GitHub CODEOWNERS.

`# suggest:` entries become optional pools. With --require-codeowners, enforced
entries become approval rules requiring one approval, mirroring GitHub's
required code-owner review. Authentication uses the installed gh and glab
clients. Dry-run is the default; only --apply permits writes. The identity map
contains account correspondences, never team rosters or file ownership rules.
"""

import argparse
import base64
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from codeowners_suggestions import enforced_owner_groups, suggested_owners

RULE_PREFIX = "github-review-pool: "
REQUIRED_PREFIX = "github-codeowners: "
APPROVALS_BY_PREFIX = {RULE_PREFIX: 0, REQUIRED_PREFIX: 1}
MANAGED_PREFIXES = tuple(APPROVALS_BY_PREFIX)


def managed_prefix(name: str) -> str | None:
    """Return the managed prefix of an approval rule name, if any."""
    return next((prefix for prefix in MANAGED_PREFIXES if name.startswith(prefix)), None)


def rewritable(rule: dict) -> bool:
    """Whether the adapter may rewrite this rule.

    Managed regular rules are rewritable, except an optional pool a human made
    required. Required code-owner pools stay rewritable whatever their current
    approval count, so they can be reset to one approval.
    """
    prefix = managed_prefix(rule["name"])
    if prefix is None or rule.get("rule_type") != "regular":
        return False
    return prefix == REQUIRED_PREFIX or rule["approvals_required"] == 0


def event_mr(path: Path, project: str, target_branch: str) -> int | None:
    """Select an MR from a GitLab webhook without executing payload content.

    Args:
        path: GitLab's TRIGGER_PAYLOAD file.
        project: Expected numeric target project ID from trusted configuration.
        target_branch: Expected protected target branch.

    Returns:
        The positive MR IID for supported events, or None for irrelevant events.
    """
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Webhook payload exceeds one MiB")
    event = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(event, dict) or event.get("object_kind") != "merge_request":
        return None
    attributes = event.get("object_attributes", {})
    if (
        str(event.get("project", {}).get("id")) != project
        or str(attributes.get("target_project_id")) != project
        or attributes.get("target_branch") != target_branch
        or attributes.get("action") not in {"open", "reopen", "update"}
        or attributes.get("state") != "opened"
        or attributes.get("draft", attributes.get("work_in_progress", False))
    ):
        return None
    iid = attributes.get("iid")
    if type(iid) is not int or iid <= 0:
        raise ValueError("Webhook payload has no valid MR IID")
    return iid


def api(
    provider: str,
    endpoint: str,
    hostname: str = "",
    *,
    method: str = "GET",
    body: dict | None = None,
    paginate: bool = False,
):
    """Call an authenticated provider CLI with structured input and bounded runtime.

    Args:
        provider: Either gh or glab.
        endpoint: Provider-relative API endpoint.
        hostname: GitLab host selected by the caller.
        method: Explicit HTTP method.
        body: JSON request body, sent on stdin without shell interpolation.
        paginate: Fetch all pages of a collection.

    Returns:
        Decoded JSON, flattening paginated collections.
    """
    command = [provider, "api", endpoint, "--method", method]
    if provider == "glab":
        command += ["--hostname", hostname]
    else:
        command += ["--hostname", "github.com"]
    if paginate:
        command += ["--paginate"]
    if body is not None:
        command += ["--input", "-", "--header", "Content-Type: application/json"]
    label = f"{provider} {method} {endpoint.split('?')[0]}"
    try:
        result = subprocess.run(
            command,
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{label} timed out") from None
    if result.returncode:
        raise RuntimeError(f"{label} failed (exit {result.returncode})")
    decoder = json.JSONDecoder()
    remaining = result.stdout.strip()
    pages = []
    try:
        while remaining:
            value, offset = decoder.raw_decode(remaining)
            pages.append(value)
            remaining = remaining[offset:].lstrip()
    except ValueError:
        raise RuntimeError(f"{label} returned invalid JSON") from None
    if paginate:
        return [item for page in pages for item in page]
    return pages[0] if pages else None


def trusted_rules(repository: str, ref: str) -> tuple[str, str]:
    """Read GitHub ownership at one immutable commit resolved from a trusted ref."""
    root = f"repos/{repository}"
    sha = api("gh", f"{root}/commits/{quote(ref, safe='')}")["sha"]
    file = api("gh", f"{root}/contents/.github/CODEOWNERS?ref={sha}")
    if file.get("encoding") != "base64":
        raise ValueError("Expected base64-encoded GitHub ownership content")
    return sha, base64.b64decode(file["content"]).decode("utf-8")


def mr_changed_paths(mr: dict, *, include_deleted: bool = False) -> list[str]:
    """Require a complete MR diff and preserve GitHub all_changed_files behavior.

    Args:
        mr: Merge request with its changes.
        include_deleted: Also return removed paths, deleted files and the old
            side of renames; required code-owner review covers them, while
            suggestions follow all_changed_files.
    """
    if mr.get("overflow") is not False or not mr.get("diff_refs", {}).get("head_sha"):
        raise ValueError("MR diff is incomplete or still being prepared")
    if mr["diff_refs"]["head_sha"] != mr.get("sha"):
        raise ValueError("MR diff does not match its current head")
    changes = mr["changes"]
    paths = [(c.get("old_path", c["new_path"]), c["new_path"], c["deleted_file"]) for c in changes]
    deleted = {old for old, new, gone in paths if gone or old != new} if include_deleted else set()
    return sorted({change["new_path"] for change in changes if not change["deleted_file"]} | deleted)


@functools.cache
def github_members(owner: str) -> list[str]:
    """Expand a GitHub team live, or retain a single account.

    Cached for the process lifetime: one run reads each team once even when it
    appears as a suggested owner, a required owner and the fallback.
    """
    name = owner.removeprefix("@").casefold()
    if "@" in name:
        raise RuntimeError("email owners are not supported; use a GitHub login or team")
    if "/" not in name:
        return [name]
    organization, team = name.split("/", 1)
    users = api("gh", f"orgs/{organization}/teams/{team}/members?per_page=100", paginate=True)
    return sorted(user["login"] for user in users)


def sso_roster() -> dict:
    """Reuse the existing audited-roster downloader with an isolated credential."""
    credential = os.environ.get("GH_SSO_MANAGMENT_PAT")
    if not credential:
        raise ValueError("GH_SSO_MANAGMENT_PAT is required unless --identity-map is supplied")
    downloader = Path(__file__).resolve().parents[1] / "actions/check-nvidia-sso/download_sso.py"
    env = dict(os.environ, GH_TOKEN=credential, SSO_FILENAME="users_sso.json")
    with tempfile.TemporaryDirectory(prefix="review-identities-") as directory:
        result = subprocess.run(
            [sys.executable, str(downloader)],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            timeout=80,
        )
        if result.returncode:
            raise RuntimeError("Audited SSO roster unavailable; no review pools changed")
        return json.loads((Path(directory) / "users_sso.json").read_text(encoding="utf-8"))


def resolve_identities(
    logins: set[str], hostname: str, *, username_from_email: bool = False
) -> tuple[dict[str, str], list[str]]:
    """Join audited emails to GitLab accounts using an explicit site convention.

    Args:
        logins: GitHub accounts to resolve from the audited roster.
        hostname: GitLab host.
        username_from_email: Use the audited NVIDIA email's account name as the
            GitLab username, for sites where private emails cannot be searched.

    Returns:
        Unambiguous GitHub-to-GitLab account correspondences, and one problem per
        account whose GitLab lookup failed; the other accounts still resolve so a
        fallback team can take over.
    """
    if not logins:
        return {}, []
    roster = {login.casefold(): entry for login, entry in sso_roster().items()}
    identities, problems = {}, []
    for login in sorted(logins):
        entry = roster.get(login.casefold(), {})
        email = str(entry.get("nvidia_email", "")).strip().lower() if isinstance(entry, dict) else ""
        if not re.fullmatch(r"[a-z0-9._%+-]{1,64}@nvidia\.com", email):
            continue
        username = email.split("@", 1)[0]
        query = f"username={quote(username, safe='')}" if username_from_email else f"search={quote(email, safe='')}"
        try:
            users = api("glab", f"users?{query}&per_page=100", hostname, paginate=True)
            if username_from_email:
                users = [user for user in users if user["username"].casefold() == username]
            if len(users) != 1:
                continue
            user = users[0]
            if not username_from_email:
                user = api("glab", f"users/{user['id']}", hostname)
                emails = {str(user.get(field) or "").strip().casefold() for field in ("email", "public_email")}
                if email not in emails:
                    continue
        except (RuntimeError, KeyError, TypeError) as error:
            problems.append(f"{login}: {error}")
            continue
        identities[login] = user["username"]
    return identities, problems


def resolve_pools(
    memberships: dict[str, list[str]],
    identities: dict[str, str],
    members: list[dict],
    author_id: int,
    *,
    prefix: str = RULE_PREFIX,
) -> tuple[list[dict], list[str], list[str]]:
    """Map live GitHub membership to eligible GitLab accounts without guessing.

    Args:
        memberships: Rule names (mentions or owner sets) and their current GitHub members.
        identities: Explicit GitHub-login to GitLab-username mapping.
        members: Effective GitLab project members.
        author_id: MR author to exclude from approval pools.
        prefix: Managed rule prefix; it determines the required approval count.

    Returns:
        Rule payloads, blocking problems, and skipped-member warnings.
    """
    identities = {login.casefold(): username.casefold() for login, username in identities.items()}
    eligible = {
        member["username"].casefold(): member
        for member in members
        if member.get("state") == "active" and member["access_level"] >= 30
    }
    pools, problems, warnings = [], [], []
    for owner, logins in memberships.items():
        users = []
        for login in logins:
            username = identities.get(login.casefold())
            if username is None:
                problems.append(f"{owner}: no GitLab identity for {login}")
            elif username not in eligible:
                warnings.append(f"{owner}: skipping {login}, ineligible GitLab account {username}")
            elif eligible[username]["id"] != author_id:
                users.append(eligible[username]["id"])
        if not users:
            problems.append(f"{owner}: no eligible reviewers other than the MR author")
        pools.append(
            {
                "name": prefix + owner,
                "approvals_required": APPROVALS_BY_PREFIX[prefix],
                "user_ids": sorted(set(users)),
                "group_ids": [],
            }
        )
    return pools, sorted(set(problems)), sorted(set(warnings))


class Side(NamedTuple):
    """One family of approval rules: its desired pools and what went wrong resolving them."""

    pools: list[dict]
    problems: list[str]
    warnings: tuple[str, ...] = ()


def expand_owners(owners: list[str]) -> tuple[list[str], list[str]]:
    """Expand owners to GitHub logins; a failed lookup becomes a problem instead of a crash."""
    logins, problems = set(), []
    for owner in owners:
        try:
            logins.update(github_members(owner))
        except (RuntimeError, KeyError, TypeError) as error:
            problems.append(f"{owner}: {error}")
    return sorted(logins), problems


def resolve_sides(
    owners: list[str],
    groups: list[list[str]],
    fallback: str | None,
    identities_for: Callable[[set[str]], tuple[dict[str, str], list[str]]],
    members: list[dict],
    author_id: int,
) -> tuple[Side, Side, Side | None, list[str]]:
    """Resolve the optional, required and fallback sides from live GitHub membership.

    Args:
        owners: Suggested owners, one optional pool each.
        groups: Enforced owner sets, one required pool each.
        fallback: Fallback team for the required side, or None.
        identities_for: Maps GitHub logins to GitLab usernames, with per-account problems.
        members: Effective GitLab project members.
        author_id: MR author to exclude from pools.

    Returns:
        The optional side, the required side, the fallback side (None when not
        configured) and identity-lookup problems that belong to no single side.
    """
    expanded = {
        RULE_PREFIX: {owner: expand_owners([owner]) for owner in owners},
        REQUIRED_PREFIX: {" ".join(group): expand_owners(group) for group in groups},
        "fallback": {f"fallback {fallback}": expand_owners([fallback])} if fallback else {},
    }
    logins = {login for side in expanded.values() for members_, _ in side.values() for login in members_}
    identities, identity_problems = identities_for(logins)

    def side(key: str, prefix: str) -> Side:
        memberships = {name: logins for name, (logins, _) in expanded[key].items()}
        pools, problems, warnings = resolve_pools(memberships, identities, members, author_id, prefix=prefix)
        lookup_problems = [problem for _, problems_ in expanded[key].values() for problem in problems_]
        return Side(pools, sorted({*problems, *lookup_problems}), tuple(warnings))

    optional, required = side(RULE_PREFIX, RULE_PREFIX), side(REQUIRED_PREFIX, REQUIRED_PREFIX)
    return optional, required, side("fallback", REQUIRED_PREFIX) if fallback else None, identity_problems


def plan_sides(
    optional: Side, required: Side, fallback: Side | None, *, enforce: bool
) -> tuple[dict[str, list[dict]], list[str], bool]:
    """Decide what one run reconciles, so a problem on one side never weakens the other.

    Args:
        optional: Suggested pools and their problems.
        required: Code-owner pools and their problems.
        fallback: The fallback pool and its problems, when configured.
        enforce: Whether required pools are managed in this run.

    Returns:
        The pools to reconcile per managed prefix, required side first so it is
        written and verified before any optional work, the blocking problems,
        and whether the required side fell back.

    The optional side is advisory: any problem skips optional reconciliation for
    this run. The required side fails closed: its problems block every write,
    unless a fallback without problems of its own replaces the exact pools. When
    nothing is left to reconcile, every problem blocks, as it always did for
    optional-only runs.
    """
    degraded = bool(required.problems and fallback and not fallback.problems)
    sides = {}
    if enforce:
        sides[REQUIRED_PREFIX] = fallback.pools if degraded else required.pools
    if not optional.problems:
        sides[RULE_PREFIX] = optional.pools
    blocking = [] if degraded else list(required.problems)
    if not sides:
        blocking = sorted({*optional.problems, *required.problems})
    return sides, blocking, degraded


def voided_required_rules(rules: list[dict], owned: set[str]) -> list[str]:
    """Name this run's required pools that GitLab would auto-approve because nobody may approve.

    GitLab treats a regular rule with no eligible approvers as satisfied. The
    adapter excludes only the MR author; "Prevent approvals by users who add
    commits" can remove everyone else.
    """
    return sorted(rule["name"] for rule in rules if rule["name"] in owned and rule.get("eligible_approvers") == [])


def apply_sides(
    hostname: str,
    root: str,
    mr: dict,
    sides: dict[str, list[dict]],
    changes_by_side: dict[str, list[dict]],
    existing: list[dict],
) -> list[dict]:
    """Apply and verify each side in order, then recheck every side against one final snapshot.

    Returns the final approval-rule snapshot. Raises when a side did not
    converge, a rule changed while a later side was being written, or one of
    this run's required rules is left with no eligible approvers.
    """
    actual = existing
    for prefix, side_changes in changes_by_side.items():
        apply_changes(hostname, root, mr, side_changes)
        actual = api("glab", root + "/approval_rules?per_page=100", hostname, paginate=True)
        if plan_changes(sides[prefix], actual, prefixes=(prefix,)):
            raise RuntimeError(f"{prefix.removesuffix(': ')} pools did not converge; inspect the MR and rerun")
    for prefix in changes_by_side:
        if plan_changes(sides[prefix], actual, prefixes=(prefix,)):
            raise RuntimeError(f"{prefix.removesuffix(': ')} pools changed during reconciliation; rerun")
    owned = {pool["name"] for pool in sides.get(REQUIRED_PREFIX, []) if REQUIRED_PREFIX in changes_by_side}
    if voided := voided_required_rules(actual, owned):
        raise RuntimeError(f"Required pools have no eligible approvers; check the committer-approval setting: {voided}")
    return actual


def plan_changes(pools: list[dict], existing: list[dict], *, prefixes: tuple[str, ...]) -> list[dict]:
    """Reconcile the managed pools under the given prefixes.

    Raises ValueError for an optional pool that a human made required; the
    caller decides whether that stops the run or only the optional side. A
    required code-owner pool that was weakened is restored to one approval.
    """
    managed = {}
    for rule in existing:
        if managed_prefix(rule["name"]) not in prefixes:
            continue
        if not rewritable(rule):
            raise ValueError(f"Refusing to change rule {rule['name']}: not a regular rule, or made required by hand")
        if rule["name"] in managed:
            raise ValueError(f"Duplicate managed rule {rule['name']}")
        managed[rule["name"]] = rule
    changes = []
    for pool in pools:
        current = managed.pop(pool["name"], None)
        if current is None:
            changes.append({"method": "POST", "body": pool})
        elif (
            sorted(user["id"] for user in current["users"]) != pool["user_ids"]
            or current["approvals_required"] != pool["approvals_required"]
            or current["groups"]
            or current.get("contains_hidden_groups", False)
        ):
            change = {"method": "PUT", "id": current["id"], "body": {**pool, "remove_hidden_groups": True}}
            changes.append({**change, "name": pool["name"]})
    changes.extend({"method": "DELETE", "id": rule["id"], "name": rule["name"]} for rule in managed.values())
    return changes


def approval_inheritance_problems(state: dict) -> list[str]:
    """Block replacing inherited required project rules with optional MR pools.

    GitLab uses MR-local regular rules once the first optional pool is created.
    Existing MR-local rules remain intact, but future project defaults are not
    automatically inherited. Consumers must explicitly accept that limitation.
    """
    if state["approval_rules_overwritten"]:
        return []
    return [
        f"Creating review pools would override inherited required rule: {rule['name']}"
        for rule in state["rules"]
        if rule["rule_type"] in {"regular", "any_approver"} and rule["approvals_required"] > 0
    ]


def require_current_mr(current: dict, snapshot: dict) -> None:
    """Reject writes after the MR head, target, or readiness changes.

    Args:
        current: Fresh merge request state.
        snapshot: Merge request used to resolve the pools.
    """
    if (current["sha"], current["target_branch"], current["state"], current.get("draft", False)) != (
        snapshot["sha"],
        snapshot["target_branch"],
        "opened",
        False,
    ):
        raise RuntimeError("MR changed while resolving reviewers; rerun from a fresh snapshot")


def suggested_reviewer_ids(pools: list[dict], author_id: int) -> list[int]:
    """Return distinct eligible pool members excluding the MR author.

    Args:
        pools: Resolved optional approval pools.
        author_id: MR author to exclude.

    Returns:
        Sorted reviewer user IDs.
    """
    return sorted({user_id for pool in pools for user_id in pool["user_ids"] if user_id != author_id})


def assign_reviewers_if_empty(hostname: str, root: str, snapshot: dict, pools: list[dict]) -> dict:
    """Assign pool members only when both the snapshot and fresh MR have no reviewers.

    Args:
        hostname: GitLab API host.
        root: Merge request API path.
        snapshot: Merge request used to resolve the pools.
        pools: Eligible optional approval pools.

    Returns:
        Assignment or skip details for the job log.
    """
    if snapshot.get("reviewers"):
        return {"skipped": "MR already has reviewers"}
    reviewer_ids = suggested_reviewer_ids(pools, snapshot["author"]["id"])
    if not reviewer_ids:
        return {"skipped": "No eligible reviewers"}
    current = api("glab", root, hostname)
    require_current_mr(current, snapshot)
    if current.get("reviewers"):
        return {"skipped": "Reviewers were assigned during reconciliation"}
    api("glab", root, hostname, method="PUT", body={"reviewer_ids": reviewer_ids})
    actual = api("glab", root, hostname)
    if not set(reviewer_ids).issubset({user["id"] for user in actual["reviewers"]}):
        raise RuntimeError("Reviewer assignment did not converge; inspect the MR")
    return {"assigned_reviewer_ids": reviewer_ids}


def apply_changes(hostname: str, root: str, mr: dict, changes: list[dict]) -> None:
    """Apply optional pool changes only while the MR snapshot remains current."""
    for change in changes:
        current = api("glab", root, hostname)
        require_current_mr(current, mr)
        if change["method"] == "POST":
            problems = approval_inheritance_problems(api("glab", root + "/approval_state", hostname))
            if problems:
                raise RuntimeError("; ".join(problems))
        endpoint = f"{root}/approval_rules"
        if "id" in change:
            endpoint += f"/{change['id']}"
            rule = api("glab", endpoint, hostname)
            if rule["name"] != change["name"] or not rewritable(rule):
                raise RuntimeError("Review pool was renamed or edited by hand since planning; refusing write")
        api("glab", endpoint, hostname, method=change["method"], body=change.get("body"))


def parse_args() -> argparse.Namespace:
    """Parse explicit provider inputs and default to a read-only plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gitlab-host", required=True)
    parser.add_argument("--project", required=True, help="Numeric GitLab project ID")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--mr", type=int)
    selection.add_argument(
        "--event-file", type=Path, help="GitLab TRIGGER_PAYLOAD file from a protected-branch pipeline"
    )
    parser.add_argument("--target-branch", required=True)
    parser.add_argument("--github-repository", required=True)
    parser.add_argument("--github-ref", default="main")
    parser.add_argument("--identity-map", type=Path, help="Optional verified account map instead of live SSO lookup")
    parser.add_argument(
        "--gitlab-username-from-email",
        action="store_true",
        help="Site convention: GitLab usernames equal audited NVIDIA email account names",
    )
    parser.add_argument(
        "--require-codeowners",
        action="store_true",
        help="Also mirror enforced CODEOWNERS entries as approval rules requiring one approval",
    )
    parser.add_argument(
        "--codeowners-fallback",
        metavar="@org/team",
        help="With --require-codeowners: keep the MR gated by this team while resolution has problems",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.codeowners_fallback and not args.require_codeowners:
        parser.error("--codeowners-fallback requires --require-codeowners")
    return args


def main() -> None:
    """Print a reviewable plan, optionally apply it, and verify convergence."""
    args = parse_args()
    if args.event_file:
        args.mr = event_mr(args.event_file, args.project, args.target_branch)
        if args.mr is None:
            print(json.dumps({"skipped": "Webhook does not request review-pool reconciliation"}))
            return
    root = f"projects/{quote(args.project, safe='')}/merge_requests/{args.mr}"
    mr = api("glab", root + "/changes?access_raw_diffs=true", args.gitlab_host)
    if mr["target_branch"] != args.target_branch:
        raise ValueError("MR does not target the configured branch")
    if mr["state"] != "opened" or mr.get("draft", False):
        print(json.dumps({"skipped": "MR is closed or draft", "mr": args.mr}))
        return
    paths = mr_changed_paths(mr)
    sha, text = trusted_rules(args.github_repository, args.github_ref)
    owners = suggested_owners(text, paths)
    groups = enforced_owner_groups(text, mr_changed_paths(mr, include_deleted=True)) if args.require_codeowners else []
    members = api(
        "glab", f"projects/{quote(args.project, safe='')}/members/all?per_page=100", args.gitlab_host, paginate=True
    )
    if args.identity_map:
        mapping = json.loads(args.identity_map.read_text(encoding="utf-8"))

        def identities_for(logins: set[str]) -> tuple[dict[str, str], list[str]]:
            return mapping, []

    else:
        identities_for = functools.partial(
            resolve_identities, hostname=args.gitlab_host, username_from_email=args.gitlab_username_from_email
        )
    author_id = mr["author"]["id"]
    optional, required, fallback, identity_problems = resolve_sides(
        owners, groups, args.codeowners_fallback, identities_for, members, author_id
    )
    resolved = [side for side in (optional, required, fallback) if side is not None]
    problems = [problem for side in resolved for problem in side.problems] + identity_problems
    warnings = [warning for side in resolved for warning in side.warnings]
    sides, blocking, degraded = plan_sides(optional, required, fallback, enforce=args.require_codeowners)
    reviewer_pools = optional.pools if RULE_PREFIX in sides else []
    existing = api("glab", root + "/approval_rules?per_page=100", args.gitlab_host, paginate=True)
    changes_by_side = {}
    for prefix, side_pools in sides.items():
        try:
            changes_by_side[prefix] = plan_changes(side_pools, existing, prefixes=(prefix,))
        except ValueError as error:
            if prefix == REQUIRED_PREFIX:
                raise
            # A hand-required optional pool blocks only the optional side.
            problems.append(str(error))
            reviewer_pools = []
    changes = [change for side_changes in changes_by_side.values() for change in side_changes]
    if sides and not changes_by_side:
        blocking = list(problems)  # every side refused: nothing can be written safely
    if not blocking and any(change["method"] == "POST" for change in changes):
        blocking = approval_inheritance_problems(api("glab", root + "/approval_state", args.gitlab_host))
        problems += blocking
    problems, warnings = sorted(set(problems)), sorted(set(warnings))
    print(
        json.dumps(
            {
                "mr": args.mr,
                "head_sha": mr["sha"],
                "github_rules_sha": sha,
                "owners": owners,
                "required_owners": groups,
                "fallback_applied": degraded and not blocking,
                "changes": changes,
                "problems": problems,
                "warnings": warnings,
                "reviewer_ids_to_assign": []
                if mr.get("reviewers")
                else suggested_reviewer_ids(reviewer_pools, author_id),
                "mode": "apply" if args.apply else "dry-run",
            },
            indent=2,
        )
    )
    if blocking:
        raise SystemExit("Review-pool preconditions are not satisfied; no writes performed")
    if args.apply:
        apply_sides(args.gitlab_host, root, mr, sides, changes_by_side, existing)
        assignment = assign_reviewers_if_empty(args.gitlab_host, root, mr, reviewer_pools)
        pool_count = sum(len(sides[prefix]) for prefix in changes_by_side)
        print(json.dumps({"applied": True, "mr": args.mr, "pool_count": pool_count, "reviewers": assignment}))
    if degraded:
        raise SystemExit("Code-owner pools fell back to the configured team; fix the listed problems")
    if problems:
        raise SystemExit("Review pools were reconciled with problems; fix the listed problems")


if __name__ == "__main__":
    main()
