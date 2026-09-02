# Isolated Claude review components

These components separate immutable pull-request context, bounded analysis, and validated publication. Consumers pin this repository by commit and retain ownership of events, authorization, triggers, prompts, skills, concurrency, budgets, and rollout policy.

## Context contract

`review_components.py build-context` reads Git objects by captured commit ID. It keeps `BASE_SHA` (trusted current-base context), `MERGE_BASE_SHA` (three-dot diff origin), and `HEAD_SHA` (proposed snapshot) distinct. It writes a versioned, digest-bound manifest, normalized metadata, rename-aware status, bounded diff/hunks, trees, and bounded regular-file snapshots. It does not check out or run proposed code. Symlinks, submodules, special files, binary files, oversized files, and exhausted budgets are represented without execution or dereference.

## Retriever contract

`retrieve` exposes paginated metadata and changed-file/tree listings, bounded textual diff and changed-snapshot reads, bounded reads/searches over captured trusted `BASE_SHA` source, diff hunks, and optional base history. The context captures governing base instructions and skills before other source. It rejects absolute paths, traversal, unavailable/non-regular objects, and requests outside the immutable snapshot. Each request appends a machine-readable audit record and is subject to call, result, byte, and time budgets. The validator derives complete changed-file, diff, and governing-policy coverage from this audit instead of trusting model-reported counts. It has no shell, runner-file, environment, network, or mutation operation.

## Output and publication contract

`review-output-v1.schema.json` is a closed schema. `validate-output` additionally binds output to the manifest, rejects invalid revisions, paths, sides, lines, duplicate findings, incomplete coverage, and oversized results, and confirms inline locations are in the immutable diff.

`publish` is model-free. It obtains the reviewed Claude GitHub App identity through GitHub OIDC in the publisher job, masks the token, rechecks the pull-request revision, issues only fixed REST requests, and revokes the token. It fails closed and never falls back to another identity. The token is never transferred through an artifact or to analysis. A changed revision receives only a fixed incomplete status. Publication preflights GitHub size/location constraints and creates one COMMENT review request containing its body and inline comments; it never approves or requests changes. Stale revisions and context/analysis failures receive a fixed visible incomplete status.

## Reference workflows

- `_isolated_review_context.yml` constructs the public, immutable context artifact.
- `_isolated_review_analyze.yml` runs schema-constrained model analysis without GitHub/OIDC permission or publication tools. Its repository-owned Python client sends only the prompt, bounded audited tool results, schema, and model identifier to the configured HTTPS inference endpoint; it does not execute a third-party action or general-purpose model tool runtime.
- `_isolated_review_publish.yml` validates and publishes without model access or a proposed-code checkout.
- `_claude_review.yml` is the Megatron-Bridge-compatible manual/automatic composition with exact manual command parsing, acknowledgment, whole-run concurrency, and explicit budgets.

A consumer may call the components independently; repository-local orchestration does not need to call the reference composition.
