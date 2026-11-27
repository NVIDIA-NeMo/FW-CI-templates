# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Reconcile the community label on the GitHub item that caused this run."""

import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import closing
from functools import partial
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import quote

REPOSITORIES = {"NVIDIA-NeMo/Automodel": 987976055, "NVIDIA-NeMo/Gym": 1044554840}
EVENT_ACTIONS = {
    "issues": {"opened", "edited", "reopened", "closed"},
    "issue_comment": {"created", "edited", "deleted"},
}
ASSOCIATED = {"MEMBER", "COLLABORATOR", "OWNER"}
OTHER_ASSOCIATIONS = {
    "CONTRIBUTOR",
    "FIRST_TIMER",
    "FIRST_TIME_CONTRIBUTOR",
    "MANNEQUIN",
    "NONE",
}
LABEL = "community-request"
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?")
MEMBER_ROLE = re.compile(r"([A-Za-z0-9_-]+):(?:Member|Owner)")
COLLABORATOR_ROLE = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+:Collaborator")


def github_request(token, method, path, data=None):
    """Call the GitHub API without following redirects or disclosing response bodies."""
    if not token:
        raise RuntimeError("Missing GitHub credential")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nemo-community-label",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    with closing(HTTPSConnection("api.github.com", timeout=15)) as connection:
        connection.request(
            method, path, json.dumps(data) if data is not None else None, headers
        )
        with connection.getresponse() as response:
            if response.getheader("X-GitHub-SSO"):
                raise RuntimeError("GitHub reports incomplete SSO access")
            body = None
            if response.status == 200 and method != "HEAD":
                body = json.load(response)
            return response.status, body


def positive_id(value):
    """Require a positive GitHub database ID or item number."""
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid GitHub item or repository identity")
    return value


def item_identity(item, repository, repository_id):
    """Require canonical GitHub URLs and immutable identities for one issue or PR."""
    number = positive_id(item["number"])
    repo_url = f"https://api.github.com/repos/{repository}"
    if (
        item["repository_url"] != repo_url
        or item["url"] != f"{repo_url}/issues/{number}"
    ):
        raise ValueError(
            "GitHub item coordinates differ from the triggering repository"
        )
    is_pr = "pull_request" in item
    if is_pr and item["pull_request"]["url"] != f"{repo_url}/pulls/{number}":
        raise ValueError("Unexpected pull request identity")
    return {
        "repo_id": repository_id,
        "number": number,
        "item_id": positive_id(item["id"]),
        "author_id": positive_id(item["user"]["id"]),
        "pull_request": is_pr,
    }


def label_present(labels):
    """Check a complete GitHub label list for the fixed community label."""
    if not isinstance(labels, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str)
        for entry in labels
    ):
        raise ValueError("Invalid GitHub label list")
    return any(entry["name"].casefold() == LABEL for entry in labels)


def snapshot(event_name, event, repository, repository_id, repo_request):
    """Bind the triggering event to the item currently at its numeric repository path."""
    if event.get("action") not in EVENT_ACTIONS.get(event_name, ()):
        return None
    if (
        REPOSITORIES.get(repository) != positive_id(repository_id)
        or event["repository"]["full_name"] != repository
        or positive_id(event["repository"]["id"]) != repository_id
    ):
        raise ValueError("Unexpected repository identity")
    item = event["issue"]
    target = item_identity(item, repository, repository_id)
    if event_name == "issues" and target["pull_request"]:
        raise ValueError("Unexpected pull request on issues event")
    if event_name == "issue_comment" and event["comment"]["issue_url"] != item["url"]:
        raise ValueError("The comment belongs to a different item")
    path = f"/repositories/{repository_id}/issues/{target['number']}"
    status, current = repo_request("GET", path)
    if status != 200:
        raise RuntimeError(f"Could not read the triggering item (HTTP {status})")
    if item_identity(current, repository, repository_id) != target:
        raise ValueError("Current GitHub item differs from the triggering item")
    login = current["user"]["login"]
    if not isinstance(login, str) or not GITHUB_LOGIN.fullmatch(login):
        raise ValueError("Unknown GitHub author")
    association = current.get("author_association")
    if association not in ASSOCIATED | OTHER_ASSOCIATIONS:
        raise ValueError("Unknown GitHub author association")
    updated_at = current.get("updated_at")
    if not isinstance(updated_at, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", updated_at
    ):
        raise ValueError("Invalid GitHub item update timestamp")
    return {
        **target,
        "login": login,
        "association": association,
        "updated_at": updated_at,
    }, label_present(current["labels"])


def load_sso_roster():
    """Download the pinned audit asset with only the SSO read credential."""
    token = os.environ.get("SSO_TOKEN")
    if not token:
        raise RuntimeError("Missing GitHub audit credential")
    downloader = (
        Path(__file__).resolve().parents[1] / "check-nvidia-sso/download_sso.py"
    )
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(
            [sys.executable, "-I", str(downloader)],
            cwd=directory,
            env={"GH_TOKEN": token},
            check=True,
            timeout=90,
        )
        return json.loads((Path(directory) / "users_sso.json").read_bytes())


def roster_membership(roster, login):
    """Read the original author's organization roles from a complete SSO snapshot."""
    if not isinstance(roster, dict) or not roster:
        raise ValueError("Invalid GitHub SSO roster")
    matching_roles = None
    for account, record in roster.items():
        roles = record.get("org_roles") if isinstance(record, dict) else None
        if (
            not isinstance(account, str)
            or not GITHUB_LOGIN.fullmatch(account)
            or not isinstance(roles, list)
            or not all(isinstance(role, str) and role for role in roles)
        ):
            raise ValueError("Invalid GitHub SSO roster record")
        if account.casefold() == login.casefold():
            if matching_roles is not None:
                raise ValueError("Ambiguous GitHub SSO account")
            matching_roles = roles
    member = False
    for role in matching_roles or []:
        match = MEMBER_ROLE.fullmatch(role)
        if match:
            member = member or match[1].casefold() in {"nvidia", "nvidia-nemo"}
        elif not COLLABORATOR_ROLE.fullmatch(role):
            raise ValueError("Unknown GitHub SSO organization role")
    return member


def internal_author(item, repo_request, load_roster):
    """Recognize exempt authors, repository collaborators and recorded org members."""
    login = item["login"]
    if login.casefold() == "svcnemo-autobot" or item["association"] in ASSOCIATED:
        return True
    collection = f"/repositories/{item['repo_id']}/collaborators"
    status, _ = repo_request("GET", f"{collection}/{quote(login, safe='')}")
    if status == 204:
        return True
    if status != 404:
        raise RuntimeError(f"Cannot check repository collaborator (HTTP {status})")
    status, _ = repo_request("HEAD", f"{collection}?per_page=1")
    if status != 200:
        raise RuntimeError(
            f"Cannot verify repository collaborator visibility (HTTP {status})"
        )
    return roster_membership(load_roster(), login)


def classify(event_name, event, repository, repository_id, repo_request, load_roster):
    """Return a decision only when the current item's label needs to change."""
    result = snapshot(event_name, event, repository, repository_id, repo_request)
    if result is None:
        return None
    item, has_label = result
    desired = not internal_author(item, repo_request, load_roster)
    return {**item, "present": desired} if has_label != desired else None


def publish(event_name, event, repository, repository_id, repo_request, decision):
    """Apply a fixed label only after the event and the classified item still agree."""
    result = snapshot(event_name, event, repository, repository_id, repo_request)
    if result is None:
        return
    item, has_label = result
    decision = decision.copy()
    desired = decision.pop("present", None)
    classified_at = decision.pop("updated_at", None)
    updated_at = item.pop("updated_at")
    if type(desired) is not bool or decision != item:
        raise ValueError("Item identity changed since classification; rerun all jobs")
    if has_label == desired:
        return
    if classified_at != updated_at:
        raise ValueError("Item changed since classification; rerun all jobs")
    labels_path = f"/repositories/{repository_id}/issues/{item['number']}/labels"
    if desired:
        status, labels = repo_request("POST", labels_path, {"labels": [LABEL]})
    else:
        status, labels = repo_request("DELETE", f"{labels_path}/{LABEL}")
    if status != 200 or label_present(labels) != desired:
        raise RuntimeError(
            f"Community label update failed (HTTP {status}); rerun all jobs"
        )
    print(f"Reconciled {LABEL} on {repository}#{item['number']}")


def main():
    """Run the trusted template phase against the runner-provided event and repository."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    args = (
        os.environ["GITHUB_EVENT_NAME"],
        event,
        os.environ["GITHUB_REPOSITORY"],
        int(os.environ["GITHUB_REPOSITORY_ID"]),
        partial(github_request, os.environ["REPO_TOKEN"]),
    )
    phase = os.environ["PHASE"]
    if phase == "classify":
        decision = classify(*args, load_sso_roster)
        if decision:
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                print(
                    "decision=" + json.dumps(decision, separators=(",", ":")),
                    file=output,
                )
    elif phase in {"publish-issue", "publish-pr"}:
        if (phase == "publish-pr") != ("pull_request" in event["issue"]):
            raise ValueError("Publication job does not match the triggering item kind")
        publish(*args, json.loads(os.environ["DECISION"]))
    else:
        raise ValueError("Unknown community label phase")


if __name__ == "__main__":
    main()
