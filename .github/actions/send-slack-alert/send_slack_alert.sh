#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

: "${MESSAGE:?MESSAGE is required}"
: "${WEBHOOK:?WEBHOOK is required}"

curl --fail-with-body --show-error --silent \
    --request POST \
    --header "Content-type: application/json" \
    --data "$MESSAGE" \
    "$WEBHOOK"
