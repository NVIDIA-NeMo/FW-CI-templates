#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) {{cookiecutter.year}} NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

gpu_args=()
if [[ "$CPU_ONLY" == "false" ]]; then
  gpu_args=(--runtime=nvidia --gpus all)
fi

container="nemo_container_$GITHUB_RUN_ID"
docker container rm -f "$container" || true
docker run --rm -d --name "$container" "${gpu_args[@]}" \
  --shm-size=64g \
  --env TRANSFORMERS_OFFLINE=0 \
  --env HYDRA_FULL_ERROR=1 \
  --env HF_HOME=/home/TestData/HF_HOME \
  --env "RUN_ID=$GITHUB_RUN_ID" \
  --volume "$GITHUB_WORKSPACE/$PROJECT_DIRECTORY:/workspace" \
  --volume "$MNT_PATH/TestData:/home/TestData" \
  "nemoci.azurecr.io/$IMAGE:$GITHUB_RUN_ID" bash -c "sleep $((TIMEOUT_SECONDS + 60))"
