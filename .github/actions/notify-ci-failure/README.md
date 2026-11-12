# Notify CI failure

Summarize dependency results and send one Slack message using the existing
`send-slack-alert/send_slack_alert.sh` delivery implementation. No GitHub API
calls, token, caller checkout, logs, artifacts, or job outputs are used.

## Caller example

Add a final job to the workflow you want to monitor. Replace `setup`, `tests`,
and `lint` with **every** job that can fail, including preparation/build jobs.
`needs` contains direct dependencies only. Matrix/reusable-workflow results are
aggregated; individual failed matrix legs are available at the run link.

```yaml
notify-failure:
  needs: [setup, tests, lint]
  runs-on: ubuntu-latest
  # Optional, if the webhook is an environment secret:
  environment: main
  permissions: {}
  if: >-
    ${{ always() && !cancelled() && failure() &&
        (github.event_name == 'schedule' ||
         (github.event_name == 'workflow_dispatch' && inputs.send_notification)) &&
        github.ref_name == github.event.repository.default_branch }}
  steps:
    - name: Notify CI failure
      uses: NVIDIA-NeMo/FW-CI-templates/.github/actions/notify-ci-failure@<full-commit-sha>
      with:
        needs-json: ${{ toJSON(needs) }}
        webhook: ${{ secrets.SLACK_TEAM_CHANNEL_WEBHOOK }}
```

The `workflow_dispatch` example assumes a boolean `send_notification` input
(default `true`) is defined by the caller. Omit that branch of the condition for
scheduled-only alerts. Select release/other branches locally if needed; no
Gym-specific branch naming is built into this action. Base scheduling decisions
on `github.event_name`, not a setup job's output: that output is empty if setup
fails and dependent jobs are skipped.

The caller owns event/branch selection, workflow cancellation policy, the
webhook's Slack destination, and environment access. This action sends only when
at least one direct dependency has result `failure`; successful, skipped, and
cancelled jobs alone do not trigger a message. No PR notification is enabled by
the example. It sends per execution, not once per commit: reruns may alert again,
and the run link and summary identify the attempt. There is no automatic rerun
or cross-run deduplication.

Inputs are `needs-json` (the complete `toJSON(needs)` object) and `webhook`.
Only job IDs and results are included; `outputs` are ignored. Metadata and job
IDs are plain text, so they cannot inject Slack mentions or links. Large
summaries are bounded to Slack's limits, showing failures first. Invalid input
fails without echoing the input. `notified` is `false` when there were no failed
dependencies, and `true` only after successful delivery. Slack HTTP rejection
fails the notification step, leaving the original failed jobs visible.

Use a trusted, immutable FW-CI-templates commit for `uses`; do not check out or
execute a failed run's code/artifacts to notify. The action requires Python 3,
Bash and curl (available on GitHub-hosted Ubuntu runners); it needs no packages
beyond Python's standard library. Pinning the action also pins the sibling
shared sender script, so notification behavior cannot drift to a mutable ref.

## Tests

From the repository root:

```sh
python3 -m unittest discover -s .github/actions/notify-ci-failure -p "test_*.py"
python3 -m unittest discover -s .github/actions/send-slack-alert -p "test_*.py"
```

Tests execute the composite shell payload with a fake curl, covering successful
and failed delivery, no-failure suppression, setup failures/skipped consumers,
malformed input, escaping, run attempts, large summaries, and output exclusion.
The existing sender suite covers HTTP rejection with a local server.
