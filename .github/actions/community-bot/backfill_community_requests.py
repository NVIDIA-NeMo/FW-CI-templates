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

"""Backfill community-request labels and Project V2 membership for issues.

The script reads GITHUB_TOKEN from the environment. It fetches issues for a
repository, checks whether each issue author is a member of NVIDIA or
NVIDIA-NeMo, and for non-members adds the community-request label and adds the
issue to a GitHub Project V2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


GITHUB_API_URL = "https://api.github.com"
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_API_VERSION = "2022-11-28"
DEFAULT_MEMBER_ORGS = ("NVIDIA", "NVIDIA-NeMo")
DEFAULT_LABEL = "community-request"
DEFAULT_LABEL_COLOR = "0e8a16"
DEFAULT_LABEL_DESCRIPTION = "Issue opened by a community user"


class GitHubError(RuntimeError):
    """Raised when a GitHub API request fails."""


@dataclass
class Summary:
    processed: int = 0
    community_candidates: int = 0
    community_labels_to_add: int = 0
    skipped_internal: int = 0
    skipped_no_author: int = 0
    labeled: int = 0
    already_labeled: int = 0
    added_to_project: int = 0
    already_in_project: int = 0
    failed: int = 0


class GitHubClient:
    def __init__(self, token: str, dry_run: bool = False):
        self.token = token
        self.dry_run = dry_run

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        expected_statuses: tuple[int, ...] = (200,),
        max_retries: int = 5,
    ) -> Any:
        url = f"{GITHUB_API_URL}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(max_retries + 1):
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request) as response:
                    response_body = response.read().decode("utf-8")
                    if response.status not in expected_statuses:
                        raise GitHubError(f"{method} {url} returned HTTP {response.status}: {response_body}")
                    if not response_body:
                        return None
                    return json.loads(response_body)
            except urllib.error.HTTPError as error:
                response_body = error.read().decode("utf-8")
                if error.code in expected_statuses:
                    if not response_body:
                        return None
                    return json.loads(response_body)
                if error.code in (403, 429, 500, 502, 503, 504) and attempt < max_retries:
                    delay = 2**attempt
                    print(f"Retrying {method} {path} after HTTP {error.code} in {delay}s")
                    time.sleep(delay)
                    continue
                raise GitHubError(f"{method} {url} returned HTTP {error.code}: {response_body}") from error
            except urllib.error.URLError as error:
                if attempt < max_retries:
                    delay = 2**attempt
                    print(f"Retrying {method} {path} after transport error in {delay}s: {error}")
                    time.sleep(delay)
                    continue
                raise GitHubError(f"{method} {url} failed: {error}") from error

        raise GitHubError(f"{method} {url} failed after retries")

    def graphql(self, query: str, variables: dict[str, Any], *, max_retries: int = 5) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")

        for attempt in range(max_retries + 1):
            request = urllib.request.Request(
                GITHUB_GRAPHQL_URL,
                data=payload,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                response_body = error.read().decode("utf-8")
                if error.code in (403, 429, 500, 502, 503, 504) and attempt < max_retries:
                    delay = 2**attempt
                    print(f"Retrying GraphQL request after HTTP {error.code} in {delay}s")
                    time.sleep(delay)
                    continue
                raise GitHubError(f"GraphQL request returned HTTP {error.code}: {response_body}") from error
            except urllib.error.URLError as error:
                if attempt < max_retries:
                    delay = 2**attempt
                    print(f"Retrying GraphQL request after transport error in {delay}s: {error}")
                    time.sleep(delay)
                    continue
                raise GitHubError(f"GraphQL request failed: {error}") from error

            if "errors" in result:
                messages = "; ".join(error.get("message", str(error)) for error in result["errors"])
                raise GitHubError(f"GraphQL request returned errors: {messages}")
            return result

        raise GitHubError("GraphQL request failed after retries")


def fetch_issues(client: GitHubClient, owner: str, repo: str, state: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = client.request(
            "GET",
            f"/repos/{owner}/{repo}/issues",
            query={"state": state, "per_page": "100", "page": str(page)},
        )
        if not batch:
            return issues

        issues.extend(issue for issue in batch if "pull_request" not in issue)
        page += 1


def is_org_member(client: GitHubClient, org: str, username: str) -> bool:
    url = f"{GITHUB_API_URL}/orgs/{urllib.parse.quote(org)}/members/{urllib.parse.quote(username)}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {client.token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status == 204
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        response_body = error.read().decode("utf-8")
        raise GitHubError(f"GET {url} returned HTTP {error.code}: {response_body}") from error


def is_member_of_any_org(
    client: GitHubClient,
    username: str,
    orgs: tuple[str, ...],
    membership_cache: dict[str, bool],
) -> bool:
    if username not in membership_cache:
        membership_cache[username] = any(is_org_member(client, org, username) for org in orgs)
    return membership_cache[username]


def ensure_label(client: GitHubClient, owner: str, repo: str, label: str) -> None:
    try:
        client.request("GET", f"/repos/{owner}/{repo}/labels/{urllib.parse.quote(label, safe='')}")
        return
    except GitHubError as error:
        if "HTTP 404" not in str(error):
            raise

    if client.dry_run:
        print(f"DRY-RUN would create label {label} in {owner}/{repo}")
        return

    client.request(
        "POST",
        f"/repos/{owner}/{repo}/labels",
        body={
            "name": label,
            "color": DEFAULT_LABEL_COLOR,
            "description": DEFAULT_LABEL_DESCRIPTION,
        },
        expected_statuses=(201,),
    )
    print(f"Created label {label} in {owner}/{repo}")


def add_label(client: GitHubClient, owner: str, repo: str, issue_number: int, label: str) -> None:
    if client.dry_run:
        print(f"DRY-RUN would add label {label} to {owner}/{repo}#{issue_number}")
        return

    client.request(
        "POST",
        f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
        body={"labels": [label]},
    )


def resolve_project_id(client: GitHubClient, project_id: str, project_owner: str) -> str:
    if not project_id.isdigit():
        return project_id

    result = client.graphql(
        """
        query($owner: String!, $projectNumber: Int!) {
          organization(login: $owner) {
            projectV2(number: $projectNumber) {
              id
            }
          }
        }
        """,
        {"owner": project_owner, "projectNumber": int(project_id)},
    )
    project = result.get("data", {}).get("organization", {}).get("projectV2")
    if not project:
        raise GitHubError(f"Could not resolve project number {project_id} in organization {project_owner}")
    return project["id"]


def add_to_project(client: GitHubClient, project_id: str, issue_node_id: str) -> str:
    if client.dry_run:
        print(f"DRY-RUN would add issue node {issue_node_id} to project {project_id}")
        return "added"

    try:
        client.graphql(
            """
            mutation($projectId: ID!, $contentId: ID!) {
              addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
                item {
                  id
                }
              }
            }
            """,
            {"projectId": project_id, "contentId": issue_node_id},
        )
        return "added"
    except GitHubError as error:
        message = str(error)
        if "already exists" in message.lower() or "already added" in message.lower():
            return "already_exists"
        raise


def process_issues(
    client: GitHubClient,
    owner: str,
    repo: str,
    project_id: str,
    member_orgs: tuple[str, ...],
    label: str,
    state: str,
) -> Summary:
    summary = Summary()
    membership_cache: dict[str, bool] = {}
    issues = fetch_issues(client, owner, repo, state)
    print(f"Fetched {len(issues)} issues from {owner}/{repo} with state={state}")

    ensure_label(client, owner, repo, label)

    for issue in issues:
        summary.processed += 1
        issue_number = issue["number"]
        author = issue.get("user", {}).get("login")
        if not author:
            print(f"Skipping {owner}/{repo}#{issue_number}: issue has no author")
            summary.skipped_no_author += 1
            continue

        try:
            if is_member_of_any_org(client, author, member_orgs, membership_cache):
                print(f"Skipping {owner}/{repo}#{issue_number}: {author} is a member of {', '.join(member_orgs)}")
                summary.skipped_internal += 1
                continue

            summary.community_candidates += 1
            labels = {item["name"] for item in issue.get("labels", [])}
            if label in labels:
                summary.already_labeled += 1
            else:
                summary.community_labels_to_add += 1
                add_label(client, owner, repo, issue_number, label)
                summary.labeled += 1
                print(f"Added label {label} to {owner}/{repo}#{issue_number}")

            project_status = add_to_project(client, project_id, issue["node_id"])
            if project_status == "already_exists":
                summary.already_in_project += 1
            else:
                summary.added_to_project += 1
                print(f"Added {owner}/{repo}#{issue_number} to project")
        except GitHubError as error:
            summary.failed += 1
            print(f"Failed to process {owner}/{repo}#{issue_number}: {error}", file=sys.stderr)

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill community-request labels and project membership for repository issues."
    )
    parser.add_argument("owner", help="Repository owner or organization, for example NVIDIA-NeMo")
    parser.add_argument("repo", help="Repository name, for example NeMo")
    parser.add_argument(
        "project_id",
        help=(
            "GitHub Project V2 node ID. Numeric values are treated as a project "
            "number and resolved under --project-owner."
        ),
    )
    parser.add_argument(
        "--project-owner",
        default="NVIDIA-NeMo",
        help="Organization that owns the project when project_id is a numeric project number.",
    )
    parser.add_argument(
        "--member-org",
        action="append",
        dest="member_orgs",
        help="Organization whose members should be treated as internal. Can be passed more than once.",
    )
    parser.add_argument("--label", default=DEFAULT_LABEL, help="Label to apply to community issues.")
    parser.add_argument(
        "--state",
        choices=("open", "closed", "all"),
        default="all",
        help="Issue state to fetch.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print intended changes without writing to GitHub.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    member_orgs = tuple(args.member_orgs or DEFAULT_MEMBER_ORGS)
    client = GitHubClient(token=token, dry_run=args.dry_run)

    try:
        project_id = resolve_project_id(client, args.project_id, args.project_owner)
        summary = process_issues(
            client=client,
            owner=args.owner,
            repo=args.repo,
            project_id=project_id,
            member_orgs=member_orgs,
            label=args.label,
            state=args.state,
        )
    except GitHubError as error:
        print(error, file=sys.stderr)
        return 1

    print(
        "Summary: "
        f"processed={summary.processed}, "
        f"community_candidates={summary.community_candidates}, "
        f"community_labels_to_add={summary.community_labels_to_add}, "
        f"skipped_internal={summary.skipped_internal}, "
        f"skipped_no_author={summary.skipped_no_author}, "
        f"labeled={summary.labeled}, "
        f"already_labeled={summary.already_labeled}, "
        f"added_to_project={summary.added_to_project}, "
        f"already_in_project={summary.already_in_project}, "
        f"failed={summary.failed}"
    )

    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
