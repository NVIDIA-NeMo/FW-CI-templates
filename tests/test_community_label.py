# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Exercise issue-event binding and SSO roster classification before a label write."""

import json
import subprocess
import sys
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
    classify_step = jobs["classify"]["steps"][0]
    assert (
        classify_step["env"]["SSO_TOKEN"] == "${{ secrets.NVIDIA_MANAGEMENT_ORG_PAT }}"
    )
    action_path = ROOT / ".github/actions/community-label/action.yml"
    action = yaml.load(action_path.read_text(), Loader=yaml.BaseLoader)
    runner = action["runs"]["steps"][0]
    assert runner["env"]["REPO_TOKEN"] == "${{ github.token }}"
    assert runner["run"] == 'python3 -I "$GITHUB_ACTION_PATH/community_label.py"'
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
        request(
            "repository-token", "GET", f"/repositories/{GYM_ID}/collaborators/{LOGIN}"
        )
    response.getheader.assert_called_with("X-GitHub-SSO")
    response.read.assert_not_called()


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
    """Record repository calls and private SSO roster reads."""

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
        self.load_roster = MagicMock(
            return_value={"other-user": {"org_roles": ["NVIDIA:Member"]}}
        )
        self.repo_calls = []
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

    def classify(self, event_name="issue_comment"):
        """Classify with the read-only job's credentials."""
        return classify(
            event_name,
            self.event,
            self.repository,
            self.repository_id,
            self.repo_request,
            self.load_roster,
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
    github.load_roster.assert_called_once_with()


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
    github.load_roster.assert_not_called()


@pytest.mark.parametrize("repository,number", [(AUTOMODEL, 42), (GYM, 43)])
def test_comment_must_belong_to_event_issue(repository, number):
    """A comment for another repository or issue cannot authorize even an item read."""
    github = GitHub()
    github.event["comment"]["issue_url"] = (
        f"https://api.github.com/repos/{repository}/issues/{number}"
    )
    with pytest.raises((ValueError, RuntimeError)):
        github.run()
    assert not github.repo_calls and not github.writes
    github.load_roster.assert_not_called()


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
    assert not github.repo_calls and not github.writes
    github.load_roster.assert_not_called()


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
    github.load_roster.assert_not_called()


def test_repository_collaborator_removes_label_without_roster():
    """A repository collaborator does not require a private roster read."""
    github = GitHub()
    github.repo_routes["GET", f"{github.collaborators_path}/{LOGIN}"] = (204, None)
    github.item["labels"] = [{"name": LABEL}]
    github.run()
    assert github.writes == [("DELETE", f"{github.item_path}/labels/{LABEL}", None)]
    github.load_roster.assert_not_called()


@pytest.mark.parametrize("org", ORGS)
@pytest.mark.parametrize("role", ["Member", "Owner"])
def test_verified_members_skip_or_remove_label(org, role):
    """The issue author's role in either organization exempts every login casing."""
    github = GitHub()
    github.load_roster.return_value = {
        LOGIN.upper(): {"org_roles": ["Other:Member", f"{org}:{role}"]}
    }
    github.run()
    assert github.writes == []
    github.item["labels"] = [{"name": LABEL}]
    github.run()
    assert github.writes == [("DELETE", f"{github.item_path}/labels/{LABEL}", None)]


@pytest.mark.parametrize(
    "method, suffix, response",
    [
        ("GET", f"/{LOGIN}", (403, None)),
        ("HEAD", "?per_page=1", (404, None)),
    ],
)
def test_uncertain_collaborator_visibility_aborts_without_write(
    method, suffix, response
):
    """Missing repository collaborator visibility cannot prove absence."""
    github = GitHub()
    github.repo_routes[method, github.collaborators_path + suffix] = response
    with pytest.raises((ValueError, RuntimeError)):
        github.run()
    assert github.writes == []
    github.load_roster.assert_not_called()


@pytest.mark.parametrize(
    "roles",
    [[], ["Other:Member"], ["NVIDIA-Labs:Member"], ["NVIDIA/other-repo:Collaborator"]],
)
def test_roster_enrollment_without_target_membership_receives_label(roles):
    """An email or membership in another organization leaves the author external."""
    github = GitHub()
    github.load_roster.return_value = {
        LOGIN: {"nvidia_email": "example@nvidia.com", "org_roles": roles}
    }
    github.run()
    assert github.writes == [
        ("POST", f"{github.item_path}/labels", {"labels": [LABEL]})
    ]


@pytest.mark.parametrize("roles", [{}, {"org_roles": None}])
def test_sso_email_with_omitted_or_null_roles_is_external(roles):
    """The audited email field identifies an enrolled account with no assigned org roles."""
    github = GitHub()
    github.load_roster.return_value = {
        LOGIN: {"nvidia_email": "example@nvidia.com", **roles}
    }
    github.run()
    assert github.writes == [
        ("POST", f"{github.item_path}/labels", {"labels": [LABEL]})
    ]


@pytest.mark.parametrize("listed", [True, False])
def test_unrelated_legacy_records_allow_author_lookup(listed):
    """Optional or unfamiliar fields on other users cannot invalidate a known author or absence."""
    github = GitHub()
    roster = {
        "legacy.identifier": {"legacy_id": 123},
        "no-roles": {"nvidia_email": "example@nvidia.com"},
        "null-roles": {"org_roles": None},
        "other-user": {"org_roles": ["Other:Owner"]},
    }
    if listed:
        roster[LOGIN.upper()] = {"org_roles": ["NVIDIA-NeMo:Member"]}
        github.item["labels"] = [{"name": LABEL}]
    github.load_roster.return_value = roster
    github.run()
    if listed:
        assert github.writes == [("DELETE", f"{github.item_path}/labels/{LABEL}", None)]
    else:
        assert github.writes == [
            ("POST", f"{github.item_path}/labels", {"labels": [LABEL]})
        ]


@pytest.mark.parametrize(
    "roster",
    [
        None,
        [],
        {},
        {"message": "unexpected payload"},
        {f"{LOGIN} ": {"org_roles": ["NVIDIA:Member"]}},
        {"other": {"org_roles": [None]}},
        {"legacy": {}, "message": {"unexpected": "response"}},
        {"other": {"org_roles": []}, LOGIN: {"unknown": True}},
        {LOGIN: None},
        {LOGIN: {}},
        {LOGIN: {"org_roles": "NVIDIA:Member"}},
        {LOGIN: {"org_roles": [None]}},
        {LOGIN: {"org_roles": ["NVIDIA"]}},
        {LOGIN: {"org_roles": ["NVIDIA:"]}},
        {LOGIN: {"org_roles": ["NVIDIA:Member:Other"]}},
        {LOGIN: {"org_roles": ["NVIDIA-NeMo:Unknown"]}},
        {LOGIN: {"org_roles": []}, LOGIN.upper(): {"org_roles": ["NVIDIA:Member"]}},
    ],
)
def test_invalid_roster_aborts_without_write(roster):
    """Empty, malformed or ambiguous membership evidence cannot drive a label change."""
    github = GitHub()
    github.load_roster.return_value = roster
    with pytest.raises((ValueError, RuntimeError)):
        github.run()
    assert github.writes == []


@pytest.mark.parametrize("denied", [False, True])
def test_classifier_entry_point_uses_pinned_private_roster(
    tmp_path, monkeypatch, capsys, denied
):
    """Run the composite action's main path with an allowlisted downloader invocation."""
    github = GitHub()
    github.item["labels"] = [{"name": LABEL}]
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(github.event))
    output_file = tmp_path / "outputs"
    output_file.touch()
    action = ROOT / ".github/actions/community-label"
    for key, value in {
        "GITHUB_ACTION_PATH": str(action),
        "GITHUB_EVENT_NAME": "issue_comment",
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_REPOSITORY": GYM,
        "GITHUB_REPOSITORY_ID": str(GYM_ID),
        "GITHUB_OUTPUT": str(output_file),
        "REPO_TOKEN": "repository-token",
        "SSO_TOKEN": "private-roster-token",
        "SSO_FILENAME": "unexpected.json",
        "SSO_RELEASE_TAG": "unexpected",
        "SSO_REPOSITORY": "unexpected/audit",
        "PHASE": "classify",
    }.items():
        monkeypatch.setenv(key, value)
    directories = []

    def run_downloader(command, *, cwd, env, check, **options):
        """Require the hardened helper and isolate its credential and temporary roster."""
        downloader = action.parent / "check-nvidia-sso/download_sso.py"
        assert downloader.is_file() and Path(command[-1]).resolve() == downloader
        assert list(command[:-1]) == [sys.executable, "-I"]
        assert check and env["GH_TOKEN"] == "private-roster-token"
        assert "repository-token" not in env.values()
        assert env.get("SSO_REPOSITORY", "NVIDIA-GitHub-Management/github-audits") == (
            "NVIDIA-GitHub-Management/github-audits"
        )
        assert env.get("SSO_RELEASE_TAG", "v0.1.0") == "v0.1.0"
        assert env.get("SSO_FILENAME", "users_sso.json") == "users_sso.json"
        directory = Path(cwd)
        directories.append(directory)
        if denied:
            raise subprocess.CalledProcessError(1, command)
        roster = {LOGIN.upper(): {"org_roles": ["NVIDIA:Member"]}}
        (directory / "users_sso.json").write_text(json.dumps(roster))
        return subprocess.CompletedProcess(command, returncode=0)

    def repo_request(token, method, path, data=None):
        """Require the repository job credential on every item or collaborator request."""
        assert token == "repository-token"
        return github.repo_request(method, path, data)

    main = SCRIPT["main"]
    monkeypatch.setitem(main.__globals__, "github_request", repo_request)
    monkeypatch.setattr(subprocess, "run", run_downloader)
    if denied:
        with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
            main()
        assert output_file.read_text() == ""
    else:
        main()
        name, value = output_file.read_text().strip().split("=", 1)
        assert name == "decision"
        assert json.loads(value)["present"] is False
    assert github.writes == []
    assert len(directories) == 1 and not directories[0].exists()
    captured = capsys.readouterr()
    for private in (LOGIN, LOGIN.upper(), "repository-token", "private-roster-token"):
        assert private not in captured.out + captured.err


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
