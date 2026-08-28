# Isolated Claude review components

These components separate immutable pull-request context, bounded analysis, and validated publication. Consumers pin this repository by commit and retain ownership of events, authorization, triggers, prompts, skills, concurrency, budgets, and rollout policy.

## Context contract

`review_components.py build-context` reads Git objects by captured commit ID. It keeps `BASE_SHA` (trusted current-base context), `MERGE_BASE_SHA` (three-dot diff origin), and `HEAD_SHA` (proposed snapshot) distinct. It writes a versioned, digest-bound manifest, normalized metadata, rename-aware status, bounded diff/hunks, trees, and bounded regular-file snapshots. It does not check out or run proposed code. Symlinks, submodules, special files, binary files, oversized files, and exhausted budgets are represented without execution or dereference.

## Retriever contract

`retrieve` exposes only paginated metadata, changed-file/tree listing, bounded base/head reads and searches, diff hunks, and optional trusted-base history. It rejects absolute paths, traversal, unavailable or non-regular objects, and requests outside captured changed-file snapshots. Each request appends a machine-readable audit record and is subject to call, result, byte, and time budgets. It has no shell, runner-file, environment, network, or mutation operation.

## Output and publication contract

`review-output-v1.schema.json` is a closed schema. `validate-output` additionally binds output to the manifest, rejects invalid revisions, paths, sides, lines, duplicate findings, incomplete coverage, and oversized results, and confirms inline locations are in the immutable diff.

`publish` is model-free. It obtains the reviewed Claude GitHub App identity through GitHub OIDC in the publisher job, masks the token, rechecks the pull-request revision, issues only fixed REST requests, and revokes the token. It fails closed and never falls back to another identity. The token is never transferred through an artifact or to analysis. A changed revision receives only a fixed incomplete status. Publication creates COMMENT-style inline comments and one top-level result; it never approves or requests changes.

## Reference workflows

- `_isolated_review_context.yml` constructs the public, immutable context artifact.
- `_isolated_review_analyze.yml` runs schema-constrained model analysis without GitHub/OIDC permission or publication tools.
- `_isolated_review_publish.yml` validates and publishes without model access or a proposed-code checkout.
- `_claude_review.yml` is the Megatron-Bridge-compatible reference composition.

A consumer may call the components independently; repository-local orchestration does not need to call the reference composition.
