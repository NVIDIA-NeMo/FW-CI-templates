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

"""Updates the 'needs-follow-up' label on issues that need attention for on-call support.

Issues that need follow-up are community issues that have not been responded to in 48 hours.
The issue requires a response if the last commentor is the issue author.
"""
import argparse
import csv
import os
from datetime import datetime, timedelta, timezone

import requests


GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_API_URL = "https://api.github.com"
NEEDS_FOLLOWUP_LABEL = "needs-follow-up"
LABEL_COLOR = "d93f0b"
LABEL_DESCRIPTION = "Issue needs follow-up"


def run_graphql_query(query: str, variables: dict, token: str) -> dict:
    """Execute a GraphQL query against GitHub's API."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        GITHUB_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=headers,
    )

    if response.status_code != 200:
        print(f"Error: GitHub API returned status code {response.status_code}")
        print(f"Response: {response.text}")
        raise RuntimeError("GraphQL query request failed")

    result = response.json()

    if "errors" in result:
        print("GraphQL errors:")
        for error in result["errors"]:
            print(f"  - {error.get('message', error)}")
        raise RuntimeError("GraphQL query returned errors")

    return result


def ensure_label_exists(org: str, repo: str, token: str) -> bool:
    """Ensure the 'needs-follow-up' label exists in the repository.

    Creates it if it doesn't exist.

    Args:
        org: GitHub organization name
        repo: Repository name
        token: GitHub personal access token

    Returns:
        True if label exists or was created successfully, False otherwise
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Check if label exists
    url = f"{GITHUB_REST_API_URL}/repos/{org}/{repo}/labels/{NEEDS_FOLLOWUP_LABEL}"
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        return True  # Label already exists

    if response.status_code == 404:
        # Label doesn't exist, create it
        create_url = f"{GITHUB_REST_API_URL}/repos/{org}/{repo}/labels"
        label_data = {
            "name": NEEDS_FOLLOWUP_LABEL,
            "color": LABEL_COLOR,
            "description": LABEL_DESCRIPTION,
        }
        create_response = requests.post(create_url, headers=headers, json=label_data)

        if create_response.status_code == 201:
            print(f"  Created label '{NEEDS_FOLLOWUP_LABEL}' in {org}/{repo}")
            return True
        else:
            print(f"  Warning: Failed to create label in {org}/{repo}: {create_response.status_code}")
            return False

    print(f"  Warning: Failed to check label in {org}/{repo}: {response.status_code}")
    return False


def add_label_to_issue(org: str, repo: str, issue_number: int, token: str) -> bool:
    """Add the 'needs-follow-up' label to an issue.

    Args:
        org: GitHub organization name
        repo: Repository name
        issue_number: Issue number
        token: GitHub personal access token

    Returns:
        True if label was added successfully, False otherwise
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    url = f"{GITHUB_REST_API_URL}/repos/{org}/{repo}/issues/{issue_number}/labels"
    response = requests.post(url, headers=headers, json={"labels": [NEEDS_FOLLOWUP_LABEL]})
    if response.status_code == 200:
        return True
    else:
        print(f"  Warning: Failed to add label to {org}/{repo}#{issue_number}: {response.status_code}")
        return False


def remove_label_from_issue(org: str, repo: str, issue_number: int, token: str) -> bool:
    """Remove the 'needs-follow-up' label from an issue.

    Args:
        org: GitHub organization name
        repo: Repository name
        issue_number: Issue number
        token: GitHub personal access token

    Returns:
        True if label was removed successfully, False otherwise
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    url = f"{GITHUB_REST_API_URL}/repos/{org}/{repo}/issues/{issue_number}/labels/{NEEDS_FOLLOWUP_LABEL}"
    response = requests.delete(url, headers=headers)

    if response.status_code in (200, 204):
        return True
    elif response.status_code == 404:
        # Label was already not on the issue
        return True
    else:
        print(f"  Warning: Failed to remove label from {org}/{repo}#{issue_number}: {response.status_code}")
        return False


def get_repo_org(repo: str, default_org: str) -> str:
    """Get the organization for a given repository.

    Some repos have hardcoded orgs that differ from the project org.

    Args:
        repo: Repository name
        default_org: Default organization to use

    Returns:
        The organization name for the repository
    """
    # Hardcoded org overrides for specific repos
    repo_org_overrides = {
        "Megatron-LM": "NVIDIA",
    }
    return repo_org_overrides.get(repo, default_org)


def apply_labels_to_issues_needing_attention(issues: list[dict], org: str, token: str):
    """Apply the 'needs-follow-up' label to all issues that need attention

    Args:
        issues: List of issue dictionaries
        org: GitHub organization name
        token: GitHub personal access token
    """
    # Filter for issues that need attention AND don't already have the label
    issues_to_label = [
        i for i in issues
        if i["needs_attention"] == "Y" and i["has_followup_label"] == "N"
    ]

    if not issues_to_label:
        print("No issues need the 'needs-follow-up' label (all already labeled or none need attention).")
        return

    print(f"\nApplying '{NEEDS_FOLLOWUP_LABEL}' label to {len(issues_to_label)} issues...")

    # Track repos where we've already ensured the label exists
    repos_with_label: set[str] = set()
    labeled_count = 0

    for issue in issues_to_label:
        repo = issue["repo_name"]
        issue_number = issue["issue_id"]

        # Get the correct org for this repo (some repos have hardcoded orgs)
        repo_org = get_repo_org(repo, org)

        # Ensure label exists in this repo (only check once per repo)
        if repo not in repos_with_label:
            if ensure_label_exists(repo_org, repo, token):
                repos_with_label.add(repo)
            else:
                continue  # Skip this issue if we couldn't create the label

        # Add label to the issue
        if add_label_to_issue(repo_org, repo, issue_number, token):
            labeled_count += 1

    print(f"Successfully labeled {labeled_count} of {len(issues_to_label)} issues.")


def remove_labels_from_resolved_issues(issues: list[dict], org: str, token: str):
    """Remove the 'needs-follow-up' label from issues that no longer need follow-up.

    An issue no longer needs follow-up if the last commenter is not the issue author.

    Args:
        issues: List of issue dictionaries
        org: GitHub organization name
        token: GitHub personal access token
    """
    # Filter for issues that have the label but no longer need attention
    # (last commenter is not the issue author)
    issues_to_unlabel = [
        i for i in issues
        if i["has_followup_label"] == "Y" and i["needs_attention"] == "N"
    ]

    if not issues_to_unlabel:
        print(f"No issues need the '{NEEDS_FOLLOWUP_LABEL}' label removed.")
        return

    print(f"\nRemoving '{NEEDS_FOLLOWUP_LABEL}' label from {len(issues_to_unlabel)} resolved issues...")

    removed_count = 0

    for issue in issues_to_unlabel:
        repo = issue["repo_name"]
        issue_number = issue["issue_id"]

        # Get the correct org for this repo (some repos have hardcoded orgs)
        repo_org = get_repo_org(repo, org)

        # Remove label from the issue
        if remove_label_from_issue(repo_org, repo, issue_number, token):
            removed_count += 1

    print(f"Successfully removed label from {removed_count} of {len(issues_to_unlabel)} issues.")


def fetch_project_issues(org: str, project_number: int, token: str) -> list[dict]:
    """Fetch all open issues from a GitHub Project.

    Args:
        org: GitHub organization name
        project_number: The project number (not the node ID)
        token: GitHub personal access token

    Returns:
        List of issue dictionaries with id, title, repo, created_at, last commenter, and last comment date
    """
    query = """
    query($org: String!, $projectNumber: Int!, $cursor: String) {
      organization(login: $org) {
        projectV2(number: $projectNumber) {
          title
          items(first: 100, after: $cursor) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              content {
                ... on Issue {
                  __typename
                  number
                  title
                  state
                  createdAt
                  author {
                    login
                  }
                  repository {
                    name
                  }
                  comments(last: 1) {
                    nodes {
                      author {
                        login
                      }
                      createdAt
                    }
                  }
                  labels(first: 100) {
                    nodes {
                      name
                    }
                  }
                }
                ... on PullRequest {
                  __typename
                }
              }
            }
          }
        }
      }
    }
    """

    issues = []
    cursor = None
    page = 1

    print(f"Fetching issues from project #{project_number} in org '{org}'...")

    while True:
        variables = {
            "org": org,
            "projectNumber": project_number,
            "cursor": cursor,
        }

        result = run_graphql_query(query, variables, token)

        project = result.get("data", {}).get("organization", {}).get("projectV2")

        if not project:
            print(f"Error: Could not find project #{project_number} in organization '{org}'")
            print("Make sure the project number and organization are correct.")
            raise RuntimeError("Could not find project")

        if page == 1:
            print(f"Project title: {project.get('title', 'Unknown')}")

        items = project.get("items", {}).get("nodes", [])

        for item in items:
            content = item.get("content")
            if not content:
                continue

            # Skip pull requests - only process issues
            if content.get("__typename") != "Issue":
                continue

            # Only include open issues
            if content.get("state") != "OPEN":
                continue

            # Get issue author as fallback
            issue_author = content.get("author", {}).get("login", "") if content.get("author") else ""
            issue_created_at = content.get("createdAt", "")

            # Check for comments - use last comment if available, otherwise use issue author/date
            comments = content.get("comments", {}).get("nodes", [])
            if comments and len(comments) > 0:
                last_comment = comments[0]
                last_commenter = last_comment.get("author", {}).get("login", "") if last_comment.get("author") else ""
                last_comment_date = last_comment.get("createdAt", "")
            else:
                # No comments - use issue author and created date
                last_commenter = issue_author
                last_comment_date = issue_created_at

            # Determine if issue needs attention:
            # Y if last commenter is the issue author AND last comment is more than 48 hours old
            needs_attention = "N"
            if last_commenter == issue_author and last_comment_date:
                try:
                    comment_dt = datetime.fromisoformat(last_comment_date.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    if now - comment_dt > timedelta(hours=48):
                        needs_attention = "Y"
                except ValueError:
                    pass

            # Check if issue already has the needs-follow-up label
            labels = content.get("labels", {}).get("nodes", [])
            label_names = [label.get("name", "") for label in labels]
            has_followup_label = "Y" if NEEDS_FOLLOWUP_LABEL in label_names else "N"

            issue = {
                "issue_id": content.get("number"),
                "issue_title": content.get("title"),
                "repo_name": content.get("repository", {}).get("name", ""),
                "issue_author": issue_author,
                "issue_created_date": issue_created_at,
                "last_commenter": last_commenter,
                "last_comment_date": last_comment_date,
                "needs_attention": needs_attention,
                "has_followup_label": has_followup_label,
            }
            issues.append(issue)

        page_info = project.get("items", {}).get("pageInfo", {})

        if page_info.get("hasNextPage"):
            cursor = page_info.get("endCursor")
            page += 1
            print(f"  Fetching page {page}...")
        else:
            break

    print(f"Found {len(issues)} open issues (excluding pull requests)")
    needs_attention_count = sum(1 for issue in issues if issue["needs_attention"] == "Y")
    print(f"Issues needing attention: {needs_attention_count}")
    has_label_count = sum(1 for issue in issues if issue["has_followup_label"] == "Y")
    print(f"Issues with '{NEEDS_FOLLOWUP_LABEL}' label: {has_label_count}")
    return issues


def main():
    """Add or remove the 'needs-follow-up' label to issues that need attention"""
    parser = argparse.ArgumentParser(
        description="Add or remove the 'needs-follow-up' label to issues that need attention"
    )
    parser.add_argument(
        "--project-id",
        type=int,
        required=True,
        help="GitHub Project number (the number shown in the project URL)",
    )
    parser.add_argument(
        "--org",
        type=str,
        required=True,
        help="GitHub organization name",
    )
    parser.add_argument(
        "--update-labels",
        action="store_true",
        help="Add or remove the 'needs-follow-up' label to issues that need attention",
    )

    args = parser.parse_args()
    token = os.environ["GITHUB_TOKEN"]
    issues = fetch_project_issues(args.org, args.project_id, token)
    if args.update_labels:
        apply_labels_to_issues_needing_attention(issues, args.org, token)
        remove_labels_from_resolved_issues(issues, args.org, token)


if __name__ == "__main__":
    main()

