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

"""Classifies issues and PRs as waiting on author or maintainers using an LLM.

Uses the latest comment or review comment on each issue/PR to determine whether
the item is waiting on the original author or waiting on maintainers. Items
waiting on maintainers receive the 'waiting-on-maintainers' label.

Requires the following environment variables:
- GITHUB_TOKEN: GitHub Personal Access Token
- OPENAI_BASE_URL: Base URL for the OpenAI-compatible API
- OPENAI_API_KEY: API key for the LLM endpoint
- LLM_MODEL: Model name to use for classification
"""
import argparse
import csv
import os
from datetime import datetime, timedelta, timezone

import openai
import requests

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_API_URL = "https://api.github.com"
DEPRECATED_NEEDS_FOLLOWUP_LABEL = "needs-follow-up"  # Old label to migrate away from

WAITING_ON_MAINTAINERS_LABEL = "waiting-on-maintainers"
WAITING_ON_MAINTAINERS_COLOR = "d93f0b"
WAITING_ON_MAINTAINERS_DESCRIPTION = "Waiting on maintainers to respond"

WAITING_ON_CUSTOMER_LABEL = "waiting-on-customer"
WAITING_ON_CUSTOMER_COLOR = "c2e0c6"
WAITING_ON_CUSTOMER_DESCRIPTION = "Waiting on the original author to respond"

CLASSIFICATION_SYSTEM_PROMPT = """\
You are classifying GitHub issues and pull requests. Based on the conversation \
context provided, determine whether the item is currently waiting on the \
original author or waiting on the maintainers/reviewers to respond.

Respond with exactly one of:
- waiting-on-author
- waiting-on-maintainers

Rules:
- If the latest comment is from the original author (or another community \
member) asking a question, reporting a problem, or requesting help, it is \
waiting-on-maintainers.
- If the latest comment is from a maintainer/reviewer asking for changes, \
requesting information, or asking the author to do something, it is \
waiting-on-author.
- If the latest comment is from a maintainer/reviewer providing an answer, \
explanation, guidance, solution, or code review feedback, the ball is back \
with the author — classify as waiting-on-author. The author is expected to \
acknowledge, follow up, or close the issue.
- If a maintainer's comment fully resolves the discussion with no open \
questions remaining for the maintainers, it is waiting-on-author.
- Only classify as waiting-on-maintainers if there is a clear unanswered \
question or request directed at the maintainers.
- If unsure, default to waiting-on-maintainers.

Respond with ONLY "waiting-on-author" or "waiting-on-maintainers", nothing else."""


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


def ensure_label_exists(org: str, repo: str, label_name: str, label_color: str, label_description: str, token: str) -> bool:
    """Ensure a label exists in the repository. Creates it if missing."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    url = f"{GITHUB_REST_API_URL}/repos/{org}/{repo}/labels/{label_name}"
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        return True

    if response.status_code == 404:
        create_url = f"{GITHUB_REST_API_URL}/repos/{org}/{repo}/labels"
        label_data = {
            "name": label_name,
            "color": label_color,
            "description": label_description,
        }
        create_response = requests.post(create_url, headers=headers, json=label_data)

        if create_response.status_code == 201:
            print(f"  Created label '{label_name}' in {org}/{repo}")
            return True
        else:
            print(f"  Warning: Failed to create label in {org}/{repo}: {create_response.status_code}")
            return False

    print(f"  Warning: Failed to check label in {org}/{repo}: {response.status_code}")
    return False


def add_label_to_issue(org: str, repo: str, issue_number: int, label_name: str, token: str) -> bool:
    """Add a label to an issue."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    url = f"{GITHUB_REST_API_URL}/repos/{org}/{repo}/issues/{issue_number}/labels"
    response = requests.post(url, headers=headers, json={"labels": [label_name]})
    if response.status_code == 200:
        return True
    else:
        print(f"  Warning: Failed to add label to {org}/{repo}#{issue_number}: {response.status_code}")
        return False


def remove_label_from_issue(org: str, repo: str, issue_number: int, label_name: str, token: str) -> bool:
    """Remove a label from an issue."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    url = f"{GITHUB_REST_API_URL}/repos/{org}/{repo}/issues/{issue_number}/labels/{label_name}"
    response = requests.delete(url, headers=headers)

    if response.status_code in (200, 204):
        return True
    elif response.status_code == 404:
        return True
    else:
        print(f"  Warning: Failed to remove label from {org}/{repo}#{issue_number}: {response.status_code}")
        return False


def get_repo_org(repo: str, default_org: str) -> str:
    """Get the organization for a given repository."""
    repo_org_overrides = {
        "Megatron-LM": "NVIDIA",
    }
    return repo_org_overrides.get(repo, default_org)


def classify_with_llm(client: openai.OpenAI, model: str, item: dict) -> str:
    """Classify an issue/PR as waiting-on-author or waiting-on-maintainers using an LLM.

    Args:
        client: OpenAI client instance
        model: Model name to use
        item: Item dictionary with issue/PR metadata including recent comments

    Returns:
        "waiting-on-author" or "waiting-on-maintainers"
    """
    recent_comments = item.get("recent_comments", [])
    if not recent_comments:
        # No comments — item is waiting on maintainers by default
        return "waiting-on-maintainers"

    comments_text = ""
    for comment in recent_comments:
        comments_text += f"\n[{comment['author']}]: {comment['body']}\n"

    user_prompt = f"""\
Item type: {item["item_type"]}
Title: {item["issue_title"]}
Original author: {item["issue_author"]}

Recent comments (oldest to newest):
{comments_text}"""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=50,
        )
        raw = response.choices[0].message.content.strip().lower()

        # Extract classification from response, handling extra text or partial matches
        if "waiting-on-maintainers" in raw:
            return "waiting-on-maintainers"
        if "waiting-on-author" in raw:
            return "waiting-on-author"

        print(f"  Warning: Unexpected LLM response '{classification}' for {item['repo_name']}#{item['issue_id']}, defaulting to waiting-on-maintainers")
        return "waiting-on-maintainers"

    except Exception as e:
        print(f"  Warning: LLM classification failed for {item['repo_name']}#{item['issue_id']}: {e}")
        return "waiting-on-maintainers"


def _is_excluded(item: dict) -> bool:
    """Check if an item is excluded from labeling (Megatron-LM dev PRs, draft PRs)."""
    return (
        (
            item["repo_name"] == "Megatron-LM"
            and item["item_type"] == "PullRequest"
            and item.get("target_branch") == "dev"
        )
        or (item["item_type"] == "PullRequest" and item.get("is_draft"))
    )


def update_labels(issues: list[dict], org: str, token: str):
    """Update labels on all issues based on LLM classification.

    - waiting-on-maintainers (needs_attention=True): add waiting-on-maintainers, remove waiting-on-customer
    - waiting-on-author (needs_attention=False): add waiting-on-customer, remove waiting-on-maintainers
    - Excluded items (Megatron-LM dev PRs, draft PRs): remove both labels
    - Migration: remove deprecated 'needs-follow-up' label wherever found
    """
    repos_with_maintainers_label: set[str] = set()
    repos_with_waiting_label: set[str] = set()

    maintainers_added = 0
    maintainers_removed = 0
    waiting_added = 0
    waiting_removed = 0
    deprecated_removed = 0

    for issue in issues:
        repo = issue["repo_name"]
        issue_number = issue["issue_id"]
        repo_org = get_repo_org(repo, org)
        excluded = _is_excluded(issue)
        classification = issue.get("classification", "")

        # Migration: remove deprecated 'needs-follow-up' label
        if issue.get("has_deprecated_label"):
            if remove_label_from_issue(repo_org, repo, issue_number, DEPRECATED_NEEDS_FOLLOWUP_LABEL, token):
                deprecated_removed += 1

        # Handle waiting-on-maintainers label
        if issue["needs_attention"] and not issue["has_maintainers_label"] and not excluded:
            if repo not in repos_with_maintainers_label:
                if ensure_label_exists(repo_org, repo, WAITING_ON_MAINTAINERS_LABEL, WAITING_ON_MAINTAINERS_COLOR, WAITING_ON_MAINTAINERS_DESCRIPTION, token):
                    repos_with_maintainers_label.add(repo)
                else:
                    continue
            if add_label_to_issue(repo_org, repo, issue_number, WAITING_ON_MAINTAINERS_LABEL, token):
                maintainers_added += 1
        elif issue["has_maintainers_label"] and (not issue["needs_attention"] or excluded):
            if remove_label_from_issue(repo_org, repo, issue_number, WAITING_ON_MAINTAINERS_LABEL, token):
                maintainers_removed += 1

        # Handle waiting-on-customer label (mutually exclusive with waiting-on-maintainers)
        wants_waiting_label = classification == "waiting-on-author" and not issue["needs_attention"] and not excluded
        if wants_waiting_label and not issue["has_waiting_on_customer_label"]:
            if repo not in repos_with_waiting_label:
                if ensure_label_exists(repo_org, repo, WAITING_ON_CUSTOMER_LABEL, WAITING_ON_CUSTOMER_COLOR, WAITING_ON_CUSTOMER_DESCRIPTION, token):
                    repos_with_waiting_label.add(repo)
                else:
                    continue
            if add_label_to_issue(repo_org, repo, issue_number, WAITING_ON_CUSTOMER_LABEL, token):
                waiting_added += 1
        elif issue["has_waiting_on_customer_label"] and not wants_waiting_label:
            if remove_label_from_issue(repo_org, repo, issue_number, WAITING_ON_CUSTOMER_LABEL, token):
                waiting_removed += 1

    if deprecated_removed:
        print(f"\nMigration: removed deprecated '{DEPRECATED_NEEDS_FOLLOWUP_LABEL}' from {deprecated_removed} issues")
    print(f"'{WAITING_ON_MAINTAINERS_LABEL}': added={maintainers_added}, removed={maintainers_removed}")
    print(f"'{WAITING_ON_CUSTOMER_LABEL}': added={waiting_added}, removed={waiting_removed}")


def fetch_project_items(org: str, project_number: int, token: str, llm_client: openai.OpenAI, llm_model: str, limit: int = 0) -> list[dict]:
    """Fetch all open issues and pull requests from a GitHub Project and classify them with an LLM.

    Args:
        org: GitHub organization name
        project_number: The project number (not the node ID)
        token: GitHub personal access token
        llm_client: OpenAI client for LLM classification
        llm_model: Model name to use for classification
        limit: Maximum number of items to process (0 for unlimited)

    Returns:
        List of item dictionaries with classification results
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
                    __typename
                    login
                  }
                  repository {
                    name
                  }
                  comments(last: 100) {
                    nodes {
                      author {
                        __typename
                        login
                      }
                      createdAt
                      body
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
                  number
                  title
                  state
                  isDraft
                  createdAt
                  baseRefName
                  author {
                    __typename
                    login
                  }
                  repository {
                    name
                  }
                  comments(last: 100) {
                    nodes {
                      author {
                        __typename
                        login
                      }
                      createdAt
                      body
                    }
                  }
                  reviewThreads(first: 50) {
                    nodes {
                      comments(first: 30) {
                        nodes {
                          author {
                            __typename
                            login
                          }
                          createdAt
                          body
                        }
                      }
                    }
                  }
                  reviews(last: 20) {
                    nodes {
                      author {
                        __typename
                        login
                      }
                      body
                      state
                      submittedAt
                    }
                  }
                  labels(first: 100) {
                    nodes {
                      name
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    items_list = []
    cursor = None
    page = 1

    print(f"Fetching issues and PRs from project #{project_number} in org '{org}'...")

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

            item_type = content.get("__typename", "")
            if item_type not in ("Issue", "PullRequest"):
                continue

            # Check if item already has managed labels
            labels = content.get("labels", {}).get("nodes", [])
            label_names = [label.get("name", "") for label in labels]
            has_maintainers_label = WAITING_ON_MAINTAINERS_LABEL in label_names
            has_waiting_on_customer_label = WAITING_ON_CUSTOMER_LABEL in label_names
            has_deprecated_label = DEPRECATED_NEEDS_FOLLOWUP_LABEL in label_names

            # Only include open issues/PRs (also include closed items with labels to clean up)
            if content.get("state") != "OPEN" and not has_maintainers_label and not has_waiting_on_customer_label and not has_deprecated_label:
                continue

            # Get author info
            author_obj = content.get("author", {}) or {}
            author = author_obj.get("login", "")
            author_type = author_obj.get("__typename", "")
            author_is_bot = author_type == "Bot"
            created_at = content.get("createdAt", "")

            # Skip items authored by bots
            if author_is_bot:
                continue

            # Collect all non-bot activity with comment bodies
            comments = content.get("comments", {}).get("nodes", [])
            activity = []
            for comment in comments:
                commenter_obj = comment.get("author", {}) or {}
                commenter_type = commenter_obj.get("__typename", "")
                activity.append((
                    commenter_obj.get("login", ""),
                    commenter_type == "Bot",
                    comment.get("createdAt", ""),
                    comment.get("body", ""),
                ))

            # For PRs, also include review thread (inline) comments and review bodies
            if item_type == "PullRequest":
                for thread in content.get("reviewThreads", {}).get("nodes", []):
                    for comment in thread.get("comments", {}).get("nodes", []):
                        commenter_obj = comment.get("author", {}) or {}
                        commenter_type = commenter_obj.get("__typename", "")
                        activity.append((
                            commenter_obj.get("login", ""),
                            commenter_type == "Bot",
                            comment.get("createdAt", ""),
                            comment.get("body", ""),
                        ))

                # Include top-level review bodies (Approve, Request changes, Comment)
                for review in content.get("reviews", {}).get("nodes", []):
                    review_body = review.get("body", "")
                    if not review_body:
                        continue
                    reviewer_obj = review.get("author", {}) or {}
                    reviewer_type = reviewer_obj.get("__typename", "")
                    state = review.get("state", "")
                    prefix = f"[Review: {state}] " if state else ""
                    activity.append((
                        reviewer_obj.get("login", ""),
                        reviewer_type == "Bot",
                        review.get("submittedAt", ""),
                        prefix + review_body,
                    ))

            # Find recent non-bot comments (last 5, chronological order)
            last_commenter = author
            last_comment_date = created_at

            activity.sort(key=lambda x: x[2], reverse=True)
            non_bot_activity = [(login, date, body) for login, is_bot, date, body in activity if not is_bot]

            if non_bot_activity:
                last_commenter = non_bot_activity[0][0]
                last_comment_date = non_bot_activity[0][1]

            # Take last 5 non-bot comments in chronological order (oldest first)
            recent_comments = [
                {"author": login, "body": body}
                for login, date, body in reversed(non_bot_activity[:5])
            ]

            # Get target branch, draft status, and approval info for PRs
            target_branch = ""
            is_draft = False
            last_approval_date = ""
            if item_type == "PullRequest":
                target_branch = content.get("baseRefName", "")
                is_draft = content.get("isDraft", False)

                # Find the latest approval date
                for review in content.get("reviews", {}).get("nodes", []):
                    if review.get("state") == "APPROVED":
                        submitted = review.get("submittedAt", "")
                        if submitted > last_approval_date:
                            last_approval_date = submitted

            repo_name = content.get("repository", {}).get("name", "")
            issue_number = content.get("number")
            repo_org = get_repo_org(repo_name, org)
            url = f"https://github.com/{repo_org}/{repo_name}/issues/{issue_number}"
            if item_type == "PullRequest":
                url = f"https://github.com/{repo_org}/{repo_name}/pull/{issue_number}"

            item_dict = {
                "item_type": item_type,
                "issue_id": issue_number,
                "issue_title": content.get("title"),
                "repo_name": repo_name,
                "url": url,
                "issue_author": author,
                "author_is_bot": author_is_bot,
                "issue_created_date": created_at,
                "last_commenter": last_commenter,
                "last_comment_date": last_comment_date,
                "recent_comments": recent_comments,
                "has_maintainers_label": has_maintainers_label,
                "has_waiting_on_customer_label": has_waiting_on_customer_label,
                "has_deprecated_label": has_deprecated_label,
                "target_branch": target_branch,
                "is_draft": is_draft,
                "last_approval_date": last_approval_date,
            }

            # Classify with LLM — skip closed items
            if content.get("state") != "OPEN":
                item_dict["needs_attention"] = False
            else:
                classification = classify_with_llm(llm_client, llm_model, item_dict)
                item_dict["classification"] = classification

                # Only flag as needing attention if classified as waiting-on-maintainers
                # AND the last comment is more than 48 hours old
                comment_dt = datetime.fromisoformat(last_comment_date.replace("Z", "+00:00"))
                is_stale = comment_dt < datetime.now(timezone.utc) - timedelta(hours=48)
                item_dict["needs_attention"] = classification == "waiting-on-maintainers" and is_stale

                # Approved PRs not merged after 48 hours always need follow-up
                if item_type == "PullRequest" and last_approval_date:
                    approval_dt = datetime.fromisoformat(last_approval_date.replace("Z", "+00:00"))
                    approval_stale = approval_dt < datetime.now(timezone.utc) - timedelta(hours=48)
                    if approval_stale:
                        item_dict["needs_attention"] = True

                print(f"  {item_dict['url']}: {classification} (stale={is_stale}, approved_unmerged={'48h+' if item_type == 'PullRequest' and last_approval_date and item_dict['needs_attention'] else 'n/a'})")

            items_list.append(item_dict)

            if limit and len(items_list) >= limit:
                print(f"Reached limit of {limit} items.")
                break

        if limit and len(items_list) >= limit:
            break

        page_info = project.get("items", {}).get("pageInfo", {})

        if page_info.get("hasNextPage"):
            cursor = page_info.get("endCursor")
            page += 1
            print(f"  Fetching page {page}...")
        else:
            break

    issue_count = sum(1 for i in items_list if i["item_type"] == "Issue")
    pr_count = sum(1 for i in items_list if i["item_type"] == "PullRequest")
    print(f"Found {len(items_list)} open items ({issue_count} issues, {pr_count} PRs)")
    needs_attention_count = sum(1 for i in items_list if i["needs_attention"])
    print(f"Items needing attention (waiting-on-maintainers): {needs_attention_count}")
    has_label_count = sum(1 for i in items_list if i["has_maintainers_label"])
    print(f"Items with '{WAITING_ON_MAINTAINERS_LABEL}' label: {has_label_count}")
    deprecated_count = sum(1 for i in items_list if i.get("has_deprecated_label"))
    if deprecated_count:
        print(f"Items with deprecated '{DEPRECATED_NEEDS_FOLLOWUP_LABEL}' label to migrate: {deprecated_count}")
    return items_list


def write_debug_csv(items: list[dict], org: str, output_path: str):
    """Write planned label changes to a CSV file for debugging.

    Args:
        items: List of item dictionaries with classification results
        org: GitHub organization name
        output_path: Path to write the CSV file
    """
    fieldnames = [
        "url",
        "repo",
        "number",
        "type",
        "title",
        "author",
        "last_commenter",
        "classification",
        "has_maintainers_label",
        "maintainers_action",
        "has_waiting_on_customer_label",
        "waiting_on_customer_action",
        "has_deprecated_label",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for item in items:
            excluded = _is_excluded(item)
            classification = item.get("classification", "")

            # Determine waiting-on-maintainers action
            if item.get("needs_attention") and not item["has_maintainers_label"] and not excluded:
                maintainers_action = "add-label"
            elif item["has_maintainers_label"] and (not item.get("needs_attention") or excluded):
                maintainers_action = "remove-label"
            else:
                maintainers_action = "no-change"

            # Determine waiting-on-customer action (mutually exclusive with waiting-on-maintainers)
            wants_waiting = classification == "waiting-on-author" and not item.get("needs_attention") and not excluded
            if wants_waiting and not item["has_waiting_on_customer_label"]:
                waiting_action = "add-label"
            elif item["has_waiting_on_customer_label"] and not wants_waiting:
                waiting_action = "remove-label"
            else:
                waiting_action = "no-change"

            writer.writerow({
                "url": item["url"],
                "repo": f"{get_repo_org(item['repo_name'], org)}/{item['repo_name']}",
                "number": item["issue_id"],
                "type": item["item_type"],
                "title": item["issue_title"],
                "author": item["issue_author"],
                "last_commenter": item["last_commenter"],
                "classification": classification,
                "has_maintainers_label": item["has_maintainers_label"],
                "maintainers_action": maintainers_action,
                "has_waiting_on_customer_label": item["has_waiting_on_customer_label"],
                "waiting_on_customer_action": waiting_action,
                "has_deprecated_label": item.get("has_deprecated_label", False),
            })

    print(f"Debug CSV written to {output_path}")


def main():
    """Classify issues/PRs and manage waiting-on-maintainers/waiting-on-customer labels using LLM classification."""
    parser = argparse.ArgumentParser(
        description="Classify issues/PRs as waiting-on-author or waiting-on-maintainers using an LLM"
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
        help="Update waiting-on-maintainers and waiting-on-customer labels based on LLM classification",
    )
    parser.add_argument(
        "--debug",
        type=str,
        metavar="CSV_PATH",
        help="Write planned label changes to a CSV file at the given path",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of items to process (default: all)",
    )

    args = parser.parse_args()
    token = os.environ["GITHUB_TOKEN"]
    llm_model = os.environ["LLM_MODEL"]

    llm_client = openai.OpenAI()

    items = fetch_project_items(args.org, args.project_id, token, llm_client, llm_model, limit=args.limit)
    if args.debug:
        write_debug_csv(items, args.org, args.debug)
    if args.update_labels:
        update_labels(items, args.org, token)


if __name__ == "__main__":
    main()
