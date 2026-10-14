# Shared Renovate presets

`python-uv.json` combines the shared update policy in `base.json` with the
`uv-overrides.json` detector.
Consumers enable the profile with:

```json
{
  "extends": [
    "github>NVIDIA-NeMo/FW-CI-templates//renovate-config/python-uv"
  ],
  "baseBranchPatterns": ["$default"]
}
```

The consumer reference above uses this repository's default branch. To test a
feature branch or pin a tag, append `#<branch-or-tag>` to that reference.

Inside `python-uv.json`, the relative imports `./base` and `./uv-overrides` load
sibling presets from the same repository and branch or tag as `python-uv.json`.
Only shared presets support relative imports; a consumer's own `renovate.json`
must use the full repository reference shown above. See Renovate's
[relative preset reference documentation](https://docs.renovatebot.com/config-presets/#relative-preset-references).

The profile groups ordinary minor and patch updates by manifest role, waits seven
days after release, requires Dependency Dashboard approval for major updates, and
leaves merging to maintainers. Repository-specific rules can refine the groups.

The override detector recognizes simple versioned requirements in
`[tool.uv].override-dependencies` and leaves their declarations under maintainer
control. Normal PEP 621 updates can still regenerate `uv.lock`.
