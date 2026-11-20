# NeMo-FW-CI-templates

A repository to centrally manage workflows across the NeMo-FW library landscape.

## Shared review suggestions

`codeowners_suggestions.py` resolves advisory `# suggest:` entries from a trusted
GitHub `.github/CODEOWNERS`. GitHub and GitLab use the same parser, micromatch
engine, last-match precedence, and per-file suppression of enforced duplicates.
The engine retains the GitHub workflow's `all_changed_files` behavior: renamed
files use their new paths, and deleted files are excluded. An enforced owner is
not automatically turned into a GitLab review suggestion.

```mermaid
flowchart LR
    Rules[GitHub suggestion rules] --> Resolver[Shared resolver]
    Diff[PR or MR changed paths] --> Resolver
    Resolver --> GH[GitHub review requests]
    Resolver --> GL[GitLab optional review pools]
    Teams[Live GitHub team membership] --> GL
    Identities[Audited SSO identity lookup] --> GL
```

The GitLab adapter creates named MR approval rules with `approvals_required: 0`.
These rules expose review pools without requiring approvals or assigning every
member as an individual reviewer. Team membership comes from GitHub on each run;
no mirrored GitLab groups or second path map are needed.

Identity resolution reuses `check-nvidia-sso/download_sso.py` and its audited
GitHub-login to corporate-email roster. The adapter then uses GitLab's exact-email
user search and verifies active Developer-or-higher project membership. On NVIDIA's
GitLab, use `--gitlab-username-from-email`: GitLab accounts use the audited
corporate email's account name, while private emails are not searchable. This
mode queries the exact GitLab username and still validates project membership.
It never derives an internal account name from the GitHub handle.

Provide the read-only audit credential as `GH_SSO_MANAGMENT_PAT`. The spelling
matches the existing credential. It is passed only to the downloader subprocess;
normal GitHub API calls continue to use the `gh` credentials. The temporary roster
is deleted when the download has been read.

For environments with an existing verified account map, `--identity-map` accepts
a JSON object instead of downloading the roster:

```json
{"github-login": "gitlab-username"}
```

Keep any explicit identity maps in trusted internal configuration. Missing mappings,
inactive accounts, accounts below Developer access, and empty review pools stop
the adapter before writes. The MR author is excluded. The adapter manages only
optional rules named `github-review-pool: @organization/team` (or `@user`),
reconciles membership, and removes stale pools with that prefix. It refuses to
modify a matching rule if someone makes it required.

Requirements: Python 3.12+, Node.js 20+, npm, and authenticated `gh` and `glab`.
The GitHub identity needs repository-content and team-membership read access;
the GitLab identity needs project-member read access and MR approval-rule write
access for apply mode. There is no need for GitHub write access in this adapter.

Run from a trusted checkout of FW-CI-templates:

```sh
npm ci --prefix .github/scripts/review-matcher --ignore-scripts --no-audit --no-fund
python3 .github/scripts/gitlab_review_pools.py \
  --gitlab-host "$GITLAB_HOST" --project "$GITLAB_PROJECT_ID" \
  --mr "$MR_IID" --target-branch "$REVIEW_TARGET_BRANCH" \
  --github-repository "$GITHUB_REPOSITORY" --github-ref main
```

This command only reads APIs and prints a plan with the MR head, immutable GitHub
rules revision, proposed mutations, and mapping problems. Add `--apply` to write
the optional pools. Apply rechecks the MR head and target before each mutation
and verifies convergence afterward. API failures can leave a partially applied
set of optional pools; rerunning reconciles them. GitLab does not provide an
atomic conditional update of MR approval rules, so concurrent pushes can require
another reconciliation.

For a project webhook that triggers a fixed protected branch, replace `--mr`
with `--event-file "$TRIGGER_PAYLOAD"`. The adapter accepts only ready, open MRs
in the configured target project and branch, on `open`, `reopen`, or `update`
events. Approval notifications and unrelated events are skipped before any API
or credential lookup. The MR is reread from the API so delayed webhook delivery
uses the current diff rather than the event's previous commit.

The webhook URL has the form
`https://gitlab.example.com/api/v4/projects/123/ref/internal/trigger/pipeline?token=<trigger-token>`.
Keep the URL and trigger token in GitLab's project settings, not in repository
files. The consumer's CI rules must accept only trigger pipelines on its fixed
protected branch, with the `review-pools` environment and a serial resource
group. This pipeline executes trusted branch code and the pinned templates
revision; it never checks out the MR source branch.

For automation, invoke the adapter from trusted infrastructure on MR creation,
updates, and transition out of draft, serializing runs per MR. Pin the tool checkout
to a reviewed commit and keep credentials outside the MR checkout. For GitLab CI,
use a protected, masked, hidden audit variable scoped to the trusted job's
environment; ordinary feature-branch MR jobs cannot consume a protected variable.
Draft and closed MRs are skipped. The adapter is advisory; its failure
must not become a required CI check. This repository does not install an MR event
trigger or enable this adapter on consumer projects.

Validate locally with:

```sh
python3 -m unittest discover -s .github/scripts -p 'test_codeowners_suggestions.py'
python3 -m unittest discover -s .github/scripts -p 'test_review_suggestions.py'
actionlint .github/workflows/_suggest_reviewers.yml .github/workflows/pre-flight.yml
```

## Guardwords checks

The reusable `_guardwords_check.yml` workflow scans added diff lines and optionally
fetches the PR description using its `pr-number` input. Pass the PR number from the
same verified metadata used to validate the mirror commit. For mirror-push callers,
expose the number from the PR metadata job:

```yaml
outputs:
  pr-number: ${{ fromJSON(steps.pr.outputs.pr-info).number }}
```

Then add this input to the reusable workflow call alongside its existing inputs:

```yaml
pr-number: ${{ fromJSON(needs.pr_info.outputs.pr-number) }}
```

Callers of this workflow revision must allow these read-only token permissions:

```yaml
permissions:
  contents: read
  pull-requests: read
```

The workflow fetches the description directly into a temporary file in the runner.
It verifies the PR number, base repository, and head SHA before writing the body,
and fails with a generic message if the fetch or verification fails. Raw description
text is never a workflow input, step environment value, output, or printed message.
Description matches report only `PR description:<line>`. The entire fetched body is
scanned, including Markdown and code blocks.

Omitting `pr-number` (or passing zero) preserves diff-only checking and makes no PR
API request. This does not change the local pre-commit hook, which scans staged
additions and skips when the private catalog is unavailable.

A description edit does not trigger a mirror-push workflow by itself. Rerun the
caller to fetch and check the current description.

Validate locally with:

```sh
python3 -m unittest discover -s .github/scripts -p 'test_guardwords.py'
python3 -m unittest discover -s .github/scripts -p 'test_fetch_pr_description.py'
```

## Code freeze authentication

Pass `app-id: ${{ vars.BOT_ID }}` and the `BOT_KEY` secret to `_code_freeze.yml`.
The caller supplies its own App identity: `NVIDIA/Megatron-LM` uses App `3610216`
(`nvidia-megatron-lm-release-bot`); NVIDIA-NeMo repositories use App `1605575`
(`nemo-automation-bot`). The shared workflow does not choose an App by owner.

Each write job mints a separate token scoped to the calling repository, with
`contents:write` and `pull_requests:write`. GitHub's label API accepts the latter
permission, so the App does not need `issues:write`. Release-branch creation also
requests `administration:write` for the existing protected-branch admin access.
Both Apps already grant these permissions. Validate branch protection before a
release; App authentication does not change the repository's protection rules.

Set `environment-name` to preserve the caller's release-branch job environment
(usually `main`, or `public` for callers that previously omitted `use-pat`).
The notification job retains its existing dry-run environment and behavior.

The legacy `use-pat` / `PAT` interface remains available during migration. Once
either `app-id` or `BOT_KEY` is supplied, both credentials and a successfully
minted token are required; the workflow fails instead of selecting legacy
credentials. The version-bump PR receives an explicit App token. Its checkout
does not persist credentials, avoiding duplicate Authorization headers.

Validate locally with:

```sh
python3 -m unittest discover -s .github/scripts -p 'test_code_freeze.py'
actionlint .github/workflows/_code_freeze.yml .github/workflows/pre-flight.yml
```

The shell integration tests require PyYAML 6.0.3. They use disposable local Git
repositories and synthetic tokens to cover rejected partial App configuration,
legacy authentication, dry-run branch pushes, and setuptools/hatch version bumps.

## CI failure notifications

Use [Notify CI failure](.github/actions/notify-ci-failure/README.md) in a final
workflow job to summarize failed dependencies in Slack. Each repository owns its
schedule/dispatch/branch policy and webhook; the shared action handles bounded
message formatting and reuses the existing Slack sender.
