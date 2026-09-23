# Check NVIDIA SSO membership

Downloads the current `users_sso.json` roster from the configured GitHub audit release, then
checks the requested login and its organization roles. Requires Python 3.11+ and `jq` (available
on GitHub-hosted Ubuntu runners); the downloader uses only the Python standard library.

## Asset rotation and retry

GitHub's release-by-tag response can retain an obsolete embedded asset ID after an asset is
replaced. Repeating `gh release download` then repeats the same 404. The downloader instead
resolves the release ID and reads its current paginated `/releases/{id}/assets` listing.

- Up to three attempts, with 1/2-second backoff; each attempt refreshes the listing and selected ID.
- Transport failures, interrupted response bodies, 404/408, rate limits and 5xx are retryable.
- Plain 401/403 and invalid JSON fail immediately. A failed lookup is not a membership verdict.
- A 60-second remaining-request budget bounds retry scheduling and individual socket timeouts;
  this is not hard cancellation of a progressively streaming response. Longer `Retry-After`
  instructions stop the attempt rather than causing immediate retries.
- Credential-bearing API URLs use configured repository coordinates and numeric IDs, not URLs
  from release metadata. Authorization is stripped from cross-host download redirects.
- The roster is written atomically only after validating a complete JSON object. A failure emits
  `sso_file_available=false`, `user_count=0`, and a nonzero step exit; membership checking does not run.

## Tests

```bash
python3 -m unittest discover -s .github/actions/check-nvidia-sso -p 'test_*.py'
```

The repository's `Test Pre-flight` workflow runs these tests on pull requests.
