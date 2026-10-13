# Shared Renovate presets

`python-uv.json` combines the shared update policy in `base.json` with the
`uv-overrides.json` detector.
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

The override detector recognizes simple versioned requirements in
`[tool.uv].override-dependencies` and leaves their declarations under maintainer
control. Normal PEP 621 updates can still regenerate `uv.lock`.
