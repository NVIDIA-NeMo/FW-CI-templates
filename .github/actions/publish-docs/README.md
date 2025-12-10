<!--
Copyright (c) 2025, NVIDIA CORPORATION. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Publish to S3 Action

## Action Parameters

| Parameter Name | Description | Required | Default Value |
|----------------|-------------|----------|---------------|
| `akamai-access-token` | Akamai EdgeGrid access token | Yes | _(none)_ |
| `akamai-host` | Akamai API hostname | Yes | _(none)_ |
| `akamai-client-token` | Akamai EdgeGrid client token | Yes | _(none)_ |
| `akamai-client-secret` | Akamai EdgeGrid client secret | Yes | _(none)_ |
| `artifacts-name` | Name of the artifacts to publish. Use the name that you used with the actions/upload-artifact action, such as docs-html-${{ github.run_id }}. | Yes | _(none)_ |
| `artifacts-path` | Path to the HTML artifacts to publish | No | `docs/_build/html` |
| `dry-run` | Whether to execute the AWS and Akamai actions | No | `false` |
| `emails-csv` | Email addresses to send the notification to. Format as "me@me.com,you@you.com". | No | `"mmckiernan@nvidia.com"` |
| `project-type` | single-docset or multi-docset project | No | `single-docset` |
| `request-name` | Name of the Akamai flush request | Yes | _(none)_ |
| `run-on-version-tag-only` | By default, only run tags that match the pattern /.+-v[0-9]+.[0-9]+.[0-9]+/. Set to false to also run on merges to the default branch and also specify the rules for when to run. | No | `"true"` |
| `s3-target-root` | Root URL/path for S3 bucket | Yes | _(none)_ |
| `s3-target-path` | Target path within S3 bucket | Yes | _(none)_ |

Multi-docset projects can only publish on a versioned tag.

```yaml
on:
  push:
    tags:
      - gpu-operator-v[0-9]+.[0-9]+.[0-9]+*
      - container-toolkit-v[0-9]+.[0-9]+.[0-9]+*
      - ...
```

An example of a multi-docset project is cloud-native-docs that needs to publish GPU Operator and NVIDIA Container Toolkit, as shown in the preceding example.

Single-docset projects can publish as follows:

- A versioned tag publishes the specified version and latest, unless the tagged commit has a message that includes `/not-latest`.
  The purpose for `/not-latest` is for updating a prior release.

- Optional: A merge to the default branch can update latest.
  Specify the branch in the workflow file.

```yaml
on:
  push:
    branches:
      - main
```

## Secrets

When you specify the following required parameters, specify a secret in the form '${{ secrets.S3_TARGET_ROOT }}'.
Store these secrets in the project that uses the action.

| Parameter Name | Description | Required | Default Value |
|----------------|-------------|----------|---------------|
| `aws-access-key-id` | AWS access key ID | Yes | _(none)_|
| `aws-secret-access-key` | AWS secret access key | Yes | _(none)_ |
| `aws-region` | AWS region | Yes | _(none)_ |
| `aws-role-to-assume` | AWS role to assume | Yes | _(none)_ |
| `akamai-access-token` | Akamai EdgeGrid access token | Yes | _(none)_ |
| `akamai-host` | Akamai API hostname | Yes | _(none)_ |
| `akamai-client-token` | Akamai EdgeGrid client token | Yes | _(none)_ |
| `akamai-client-secret` | Akamai EdgeGrid client secret | Yes | _(none)_ |
| `s3-target-root` | Root URL/path for S3 bucket | Yes | _(none)_ |
| `s3-target-path` | Target path within S3 bucket | Yes | _(none)_ |
