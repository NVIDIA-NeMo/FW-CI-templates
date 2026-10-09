# NeMo-FW-CI-templates

A repository to centrally manage workflows across the NeMo-FW library landscape.

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

## Test approval queue contributor classification

`_test_approval_queue.yml` uses the pull request author, not the workflow actor,
when assigning internal and external queue slots. The service accounts
`svcnvidia-nemo-ci` and `svcnemo-autobot` are internal, matching the existing
`check-nvidia-sso` policy. Other authors still require one of `internal_org_roles`
in the SSO users asset; unlisted authors remain external.

This classification does not bypass branch filters, queue concurrency limits,
the target deployment environment, or protection of the approval manager's own
`approval_environment`. A manager job waiting on that environment requires
maintainer action before it can process any queue. Callers pinned to an older
workflow revision must update their immutable pin after this fix is merged.

Validate the workflow's actual classification and queue filtering locally with
Python 3.12 and `PyYAML==6.0.3`:

```sh
python3 -m unittest discover -s .github/scripts -p "test_approval_queue.py"
```
