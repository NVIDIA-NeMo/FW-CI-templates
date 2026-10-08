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

## Test approval queues

`_test_approval_queue.yml` classifies the pull-request author, not the workflow
actor. `svcnemo-autobot` and `svcnvidia-nemo-ci` join the internal queue even when
absent from the human SSO export, matching `check-nvidia-sso`'s existing exact
service-account policy. Other authors still require one of `internal_org_roles`
in their SSO record; bot-like usernames and unrelated GitHub bots are not trusted.

This classification does not grant copy-pr-bot vetting or bypass environments,
branch filters, queue ordering, or concurrency limits. Callers must update their
pinned shared-workflow ref after the correction lands upstream.

Validate with PyYAML 6.0.3 installed:

```sh
python3 -m unittest discover -s .github/scripts -p 'test_test_approval_queue.py'
```
