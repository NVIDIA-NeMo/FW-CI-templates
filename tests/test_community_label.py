# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Exercise issue-event binding and membership visibility before a label write."""

from copy import deepcopy
from pathlib import Path
from runpy import run_path
from unittest.mock import MagicMock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = run_path(str(ROOT / ".github/actions/community-label/community_label.py"))
classify = SCRIPT["classify"]
publish = SCRIPT["publish"]
GYM = "NVIDIA-NeMo/Gym"
GYM_ID = 1044554840
AUTOMODEL = "NVIDIA-NeMo/Automodel"
AUTOMODEL_ID = 987976055
LABEL = "community-request"
ORGS = ("NVIDIA-NeMo", "NVIDIA")
LOGIN = "issue-author"


def test_workflow_separates_credentials_and_permissions():
    """The reusable workflow accepts no target and exposes the identity credential only to classification."""
    workflow_path = ROOT / ".github/workflows/_community_label.yml"
    workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)
    contract = workflow["on"]["workflow_call"]
    assert "inputs" not in contract
    assert set(contract["secrets"]) == {"NVIDIA_MANAGEMENT_ORG_PAT"}
    assert workflow["permissions"] == {}
    jobs = workflow["jobs"]
    assert jobs["classify"]["permissions"] == {
        "issues": "read",
        "pull-requests": "read",
    }
    assert jobs["publish-issue"]["permissions"] == {"issues": "write"}
    assert jobs["publish-pr"]["permissions"] == {
        "issues": "read",
        "pull-requests": "write",
    }
    assert "secrets.NVIDIA_MANAGEMENT_ORG_PAT" in str(jobs["classify"]["steps"])
    for job in ("publish-issue", "publish-pr"):
        assert "secrets." not in str(jobs[job])
        assert jobs[job]["needs"] == "classify"
    for job in jobs.values():
        assert [step["uses"] for step in job["steps"]] == [
            "$/.github/actions/community-label"
        ]


@pytest.mark.parametrize("status", [200, 404])
def test_partial_sso_never_returns_membership_result(monkeypatch, status):
    """GitHub's partial-results header invalidates even an otherwise usable HTTP status."""
    request = SCRIPT["github_request"]
    connection = MagicMock()
    response = connection.getresponse.return_value.__enter__.return_value
    response.status = status
    response.getheader.return_value = "partial-results; organizations=123"
    monkeypatch.setitem(
        request.__globals__, "HTTPSConnection", lambda *a, **k: connection
    )
    with pytest.raises(RuntimeError, match="SSO"):
        request("fixture-read-only-token", "GET", f"/orgs/NVIDIA/memberships/{LOGIN}")
    response.getheader.assert_called_with("X-GitHub-SSO")
    response.read.assert_not_called()


@pytest.mark.parametrize(
    "scopes, permitted",
    [(None, True), ("repo, read:org", True), ("repo", False), ("", False)],
)
def test_private_visibility_requires_classic_org_scope(monkeypatch, scopes, permitted):
    """A classic PAT must prove an organization scope; fine-grained credentials omit this header."""
    request = SCRIPT["github_request"]
    connection = MagicMock()
    response = connection.getresponse.return_value.__enter__.return_value
    response.status = 200
    response.getheader.side_effect = {"X-OAuth-Scopes": scopes}.get
    response.read.return_value = b'{"state":"active"}'
    monkeypatch.setitem(
        request.__globals__, "HTTPSConnection", lambda *a, **k: connection
    )
    args = ("fixture-read-only-token", "GET", "/user/memberships/orgs/NVIDIA")
    if permitted:
        assert request(*args) == (200, {"state": "active"})
    else:
        with pytest.raises(RuntimeError):
            request(*args)


def issue_event(repository, repository_id, author=LOGIN):
    """Build a comment event whose commenter is an exempt account."""
    url = f"https://api.github.com/repos/{repository}"
    return {
        "action": "created",
        "repository": {"full_name": repository, "id": repository_id},
        "issue": {
            "url": f"{url}/issues/42",
            "repository_url": url,
            "number": 42,
            "id": 7654321,
            "updated_at": "2026-11-27T01:03:00Z",
            "user": {"login": author, "id": 123},
            "author_association": "NONE",
            "labels": [],
        },
        "comment": {
            "issue_url": f"{url}/issues/42",
            "user": {"login": "svcnemo-autobot"},
            "author_association": "OWNER",
        },
    }


class GitHub:
    """Record calls with distinct repository and private organization credentials."""

    def __init__(self, repository=GYM, repository_id=GYM_ID, author=LOGIN):
        """Set up an existing issue whose author has no known affiliation."""
        self.repository = repository
        self.repository_id = repository_id
        self.event = issue_event(repository, repository_id, author)
        self.item = deepcopy(self.event["issue"])
        self.item_path = f"/repositories/{repository_id}/issues/42"
        self.collaborators_path = f"/repositories/{repository_id}/collaborators"
        self.repo_routes = {
            ("GET", f"/repositories/{repository_id}"): (200, self.event["repository"]),
            ("GET", self.item_path): (200, self.item),
            ("GET", f"{self.collaborators_path}/{author}"): (404, None),
            ("HEAD", f"{self.collaborators_path}?per_page=1"): (200, None),
        }
        self.org_routes = {}
        for org in ORGS:
            self.org_routes["GET", f"/orgs/{org}/memberships/{author}"] = (404, None)
            self.org_routes["GET", f"/user/memberships/orgs/{org}"] = (
                200,
                {"state": "active", "organization": {"login": org}},
            )
        self.repo_calls = []
        self.org_calls = []
        self.writes = []

    def repo_request(self, method, path, data=None):
        """Permit only the enrolled issue, author, and fixed label endpoints."""
        self.repo_calls.append((method, path))
        if method in {"POST", "DELETE"}:
            self.writes.append((method, path, data))
            if method == "POST":
                assert (path, data) == (f"{self.item_path}/labels", {"labels": [LABEL]})
                return 200, [{"name": LABEL}]
            assert (path, data) == (f"{self.item_path}/labels/{LABEL}", None)
            return 200, []
        return self.repo_routes[method, path]

    def org_request(self, method, path, data=None):
        """Allow only membership reads using the audit credential."""
        self.org_calls.append((method, path))
        assert method == "GET" and data is None
        return self.org_routes[method, path]

    def classify(self, event_name="issue_comment"):
        """Classify with the read-only job's credentials."""
        return classify(
            event_name,
            self.event,
            self.repository,
            self.repository_id,
            self.repo_request,
            self.org_request,
        )

    def publish(self, decision, event_name="issue_comment"):
        """Run the repository writer with the unmodified original event."""
        publish(
            event_name,
            self.event,
            self.repository,
            self.repository_id,
            self.repo_request,
            decision,
        )

    def run(self, event_name="issue_comment"):
        """Call the same entry points in the same order as the two Actions jobs."""
        decision = self.classify(event_name)
        if decision is not None:
            self.publish(decision, event_name)


@pytest.mark.parametrize(
    "repository, repository_id", [(GYM, GYM_ID), (AUTOMODEL, AUTOMODEL_ID)]
)
def test_comment_reconciles_original_author(repository, repository_id):
    """Both repository token writes stay on the event's issue and require verified negatives."""
    github = GitHub(repository, repository_id)
    github.run()
    assert github.writes == [
        ("POST", f"{github.item_path}/labels", {"labels": [LABEL]})
    ]
    assert ("HEAD", f"{github.collaborators_path}?per_page=1") in github.repo_calls
    for org in ORGS:
        assert ("GET", f"/orgs/{org}/memberships/{LOGIN}") in github.org_calls
        assert ("GET", f"/user/memberships/orgs/{org}") in github.org_calls


@pytest.mark.parametrize(
    "which",
    [
        "event repository",
        "same number in another repo",
        "other number",
        "other issue ID",
    ],
)
def test_other_issue_never_receives_write(which):
    """Reject discrepancies in the event context and in the identity returned by GitHub."""
    github = GitHub()
    if which == "event repository":
        github.event = issue_event(AUTOMODEL, AUTOMODEL_ID)
    elif which == "same number in another repo":
        other = issue_event(AUTOMODEL, AUTOMODEL_ID)["issue"]
        github.item.update(url=other["url"], repository_url=other["repository_url"])
    elif which == "other number":
        github.item.update(number=43, url=github.item["url"].replace("42", "43"))
    else:
        github.item["id"] += 1
    with pytest.raises((ValueError, RuntimeError)):
        github.run()
    assert github.writes == []
    assert github.org_calls == []


@pytest.mark.parametrize("repository,number", [(AUTOMODEL, 42), (GYM, 43)])
def test_comment_must_belong_to_event_issue(repository, number):
    """A comment for another repository or issue cannot authorize even an item read."""
    github = GitHub()
    github.event["comment"]["issue_url"] = (
        f"https://api.github.com/repos/{repository}/issues/{number}"
    )
    with pytest.raises((ValueError, RuntimeError)):
        github.run()
    assert not github.repo_calls and not github.org_calls and not github.writes


@pytest.mark.parametrize(
    "event_name, action, is_pr",
    [
        ("issues", "deleted", False),
        ("issues", "transferred", False),
        ("issues", "labeled", False),
        ("pull_request_target", "opened", True),
        ("workflow_dispatch", "created", False),
    ],
)
def test_ineligible_events_skip_github(event_name, action, is_pr):
    """Only supported activity on a surviving item may reach GitHub."""
    github = GitHub()
    github.event["action"] = action
    if is_pr:
        github.event["issue"]["pull_request"] = {
            "url": "https://api.github.com/example"
        }
    github.run(event_name)
    assert not github.repo_calls and not github.org_calls and not github.writes


@pytest.mark.parametrize(
    "event_name,action",
    [
        ("issues", "opened"),
        ("issues", "edited"),
        ("issues", "closed"),
        ("issues", "reopened"),
        ("issue_comment", "created"),
        ("issue_comment", "edited"),
        ("issue_comment", "deleted"),
    ],
)
def test_supported_activity_reconciles_label(event_name, action):
    """Opening, editing or closing an issue and all top-level comment changes trigger triage."""
    github = GitHub()
    github.event["action"] = action
    if event_name == "issues":
        del github.event["comment"]
    github.run(event_name)
    assert github.writes == [
        ("POST", f"{github.item_path}/labels", {"labels": [LABEL]})
    ]


def test_unknown_author_association():
    """If GitHub omits the association, the writer receives no decision."""
    github = GitHub()
    github.item.pop("author_association")
    with pytest.raises((ValueError, RuntimeError)):
        github.run()
    assert not github.writes


@pytest.mark.parametrize(
    "association, author",
    [
        ("MEMBER", LOGIN),
        ("OWNER", LOGIN),
        ("COLLABORATOR", LOGIN),
        ("NONE", "svcnemo-autobot"),
    ],
)
def test_associated_author_removes_label_without_private_reads(association, author):
    """Each author exemption removes only the community label from the original issue."""
    github = GitHub(author=author)
    github.item.update(author_association=association, labels=[{"name": LABEL}])
    github.event["action"] = "reopened"
    github.run("issues")
    assert github.writes == [("DELETE", f"{github.item_path}/labels/{LABEL}", None)]
    assert github.org_calls == []


@pytest.mark.parametrize("source", ["repo", *ORGS])
def test_verified_members_skip_or_remove_label(source):
    """A positive from either live membership source classifies the original author as internal."""
    github = GitHub()
    if source == "repo":
        github.repo_routes["GET", f"{github.collaborators_path}/{LOGIN}"] = (204, None)
    else:
        github.org_routes["GET", f"/orgs/{source}/memberships/{LOGIN}"] = (
            200,
            {
                "state": "active",
                "organization": {"login": source},
                "user": {"login": LOGIN},
            },
        )
    github.run()
    assert github.writes == []
    github.item["labels"] = [{"name": LABEL}]
    github.run()
    assert github.writes == [("DELETE", f"{github.item_path}/labels/{LABEL}", None)]


@pytest.mark.parametrize(
    "source, response",
    [
        ("repo target", (403, None)),
        ("repo visibility", (404, None)),
        ("org target", (429, None)),
        ("org target", (200, {})),
        ("org visibility", (403, None)),
        ("org visibility", (200, {"state": "pending"})),
        ("org visibility", (200, {"other": True})),
    ],
)
def test_uncertain_membership_aborts_without_write(source, response):
    """Authorization errors, malformed responses and concealed memberships cannot prove absence."""
    github = GitHub()
    routes = {
        "repo target": (
            github.repo_routes,
            ("GET", f"{github.collaborators_path}/{LOGIN}"),
        ),
        "repo visibility": (
            github.repo_routes,
            ("HEAD", f"{github.collaborators_path}?per_page=1"),
        ),
        "org target": (github.org_routes, ("GET", f"/orgs/NVIDIA/memberships/{LOGIN}")),
        "org visibility": (github.org_routes, ("GET", "/user/memberships/orgs/NVIDIA")),
    }
    target, path = routes[source]
    target[path] = response
    with pytest.raises((ValueError, RuntimeError)):
        github.run()
    assert github.writes == []


def test_external_author_already_labeled():
    """A matching live label leaves the original issue untouched."""
    github = GitHub()
    github.item["labels"] = [{"name": "bug"}, {"name": LABEL}]
    github.run()
    assert github.writes == []


def test_top_level_pr_comment():
    """Classify the PR author's affiliation through the original issue-comment event."""
    github = GitHub()
    github.event["issue"]["pull_request"] = {
        "url": f"https://api.github.com/repos/{GYM}/pulls/42"
    }
    github.item.update(pull_request=github.event["issue"]["pull_request"])
    github.run()
    assert github.writes == [
        ("POST", f"{github.item_path}/labels", {"labels": [LABEL]})
    ]


@pytest.mark.parametrize("status", [301, 307, 410])
def test_transferred_or_gone_item_aborts(status):
    """An old item coordinate cannot redirect a label to the transfer destination."""
    github = GitHub()
    destination = f"https://api.github.com/repos/{AUTOMODEL}/issues/2468"
    github.repo_routes["GET", github.item_path] = (status, {"url": destination})
    try:
        github.run()
    except (ValueError, RuntimeError):
        pass
    assert github.writes == []


@pytest.mark.parametrize(
    "change", ["repository", "number", "id", "kind", "updated_at", "author"]
)
def test_writer_revalidates_target(change):
    """A decision from another target or a stale snapshot cannot cause a mutation."""
    github = GitHub()
    decision = github.classify()
    assert decision and not github.writes
    if change == "repository":
        github = GitHub(AUTOMODEL, AUTOMODEL_ID)
    elif change == "number":
        github.event["issue"]["number"] += 1
        github.event["issue"]["url"] = github.event["issue"]["url"].replace("42", "43")
    elif change == "id":
        github.item["id"] += 1
    elif change == "kind":
        github.item["pull_request"] = {
            "url": f"https://api.github.com/repos/{GYM}/pulls/42"
        }
    elif change == "updated_at":
        github.item["updated_at"] = "2026-11-27T01:04:00Z"
    else:
        github.item["user"] = {"login": "other-user", "id": 4321}
    try:
        github.publish(decision)
    except (ValueError, RuntimeError):
        pass
    assert github.writes == []


@pytest.mark.parametrize("already_present", [False, True])
def test_retry_after_lost_write_response(already_present):
    """Replaying a successful label mutation after a timeout is idempotent for both operations."""
    github = GitHub()
    if already_present:
        github.item.update(author_association="MEMBER", labels=[{"name": LABEL}])
    decision = github.classify()
    assert decision
    trusted_request = github.repo_request

    def timeout_after_write(method, path, data=None):
        """Commit the request at GitHub, then simulate the client's lost response."""
        result = trusted_request(method, path, data)
        if method in {"POST", "DELETE"}:
            github.item["labels"] = [] if already_present else [{"name": LABEL}]
            github.item["updated_at"] = "2026-11-27T01:04:00Z"
            raise TimeoutError("lost response")
        return result

    github.repo_request = timeout_after_write
    with pytest.raises(TimeoutError):
        github.publish(decision)
    github.repo_request = trusted_request
    github.publish(decision)
    assert len(github.writes) == 1


@pytest.mark.parametrize("already_present", [False, True])
def test_write_requires_response_to_confirm_desired_label(already_present):
    """A 200 label response that contradicts the requested change must fail the publishing job."""
    github = GitHub()
    if already_present:
        github.item.update(author_association="MEMBER", labels=[{"name": LABEL}])
    decision = github.classify()
    assert decision
    trusted_request = github.repo_request

    def unchanged_labels(method, path, data=None):
        """Return the unchanged server label list after a simulated mutation."""
        result = trusted_request(method, path, data)
        return (200, github.item["labels"]) if method in {"POST", "DELETE"} else result

    github.repo_request = unchanged_labels
    with pytest.raises(RuntimeError):
        github.publish(decision)
    assert len(github.writes) == 1
