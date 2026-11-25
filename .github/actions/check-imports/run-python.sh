#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
working_directory=$1
shift

if command -v -- "$PYTHON_BINARY" >/dev/null; then
    python=("$PYTHON_BINARY")
else
    parser=$(command -v python3 || command -v python)
    python=()
    while IFS= read -r -d '' argument; do
        python+=("$argument")
    done < <("$parser" -I -c '
import os, shlex, sys
for argument in shlex.split(os.environ["PYTHON_BINARY"]):
    sys.stdout.buffer.write(os.fsencode(argument) + b"\0")
')
fi

if [[ -z "${python[0]:-}" ]]; then
    echo "::error::python-binary must identify a Python interpreter."
    exit 1
fi
if [[ "${python[0]}" == */* && "${python[0]}" != /* ]]; then
    python[0]="$PWD/${python[0]}"
fi
cd -- "$working_directory"
exec "${python[@]}" "$@"
