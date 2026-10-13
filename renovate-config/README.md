# Shared Renovate presets

`python-uv.json` combines the shared update policy in `base.json` with the
`uv-overrides.json` detector and the `uv-lock-security.json` vulnerability tracker.
Consumers enable the profile with:

```json
{
  "extends": [
    "github>NVIDIA-NeMo/FW-CI-templates//renovate-config/python-uv#add-renovate-config-2"
  ],
  "baseBranchPatterns": ["$default"]
}
```

The profile groups ordinary minor and patch updates by manifest role, waits seven
days after release, requires Dependency Dashboard approval for major updates, and
leaves merging to maintainers. Repository-specific rules can refine the groups.

## Transitive vulnerability tracking

`uv-lock-security.json` exposes the exact versions of public PyPI packages from
root and nested `uv.lock` files to Renovate's OSV vulnerability summary. This
includes transitive packages and direct requirements whose manifest declarations
have no version. It preserves multiple locked versions of the same package and
excludes workspace, Git, URL, and non-PyPI registry sources.

The Dependency Dashboard's **Vulnerabilities** section lists matching advisories
and fixed versions when OSV provides them. Reports come from OSV, which includes
GitHub advisories; this is not a synchronization of the repository's Dependabot
alert states, dismissals, or alert counts. Reporting does not wait seven days.
The summary feature is experimental in Renovate.

This detector is read-only. It does not create update branches, edit `uv.lock`,
change manifest requirements or uv overrides, or add approval checkboxes for
transitive fixes. Normal PEP 621 update PRs can still regenerate the lockfile.
Keep `vulnerabilityAlerts.enabled` false and `osvVulnerabilityAlerts` false as in
the shared policy and Renovate's defaults; the OSV summary works independently
of those fix-PR settings. Enabling a security-update path requires separate
validation of the update rules and lockfile regeneration.

Renovate's Dashboard may describe advisory fixed versions as "possible Renovate
fixes." That generic wording does not mean this detector can open a fix PR.
Transitive fixes require a maintainer to regenerate the lockfile, or separate
automation with equivalent policy checks. The hosted app blocks custom
post-upgrade commands by default, so a lockfile-writing custom manager cannot be
enabled safely with repository configuration alone.

The Python profile already enables `custom.jsonata`. If using the tracker preset
independently with an `enabledManagers` list, include `custom.jsonata` in that
list. Overrides remain under maintainer control via `uv-overrides.json`.

Relevant Renovate documentation:

- [OSV Dashboard summary](https://docs.renovatebot.com/configuration-options/#dependencydashboardosvvulnerabilitysummary)
- [JSONata custom managers](https://docs.renovatebot.com/modules/manager/jsonata/)
- [Post-upgrade command restrictions](https://docs.renovatebot.com/configuration-options/#postupgradetasks)
- [Proposed native uv transitive remediation](https://github.com/renovatebot/renovate/pull/44262)
