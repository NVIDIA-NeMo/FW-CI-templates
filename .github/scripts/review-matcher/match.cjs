// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/** Match rule globs with the options used by step-security/changed-files. */
const fs = require("node:fs");
const micromatch = require("micromatch");
const { files, patterns } = JSON.parse(fs.readFileSync(0, "utf8"));
const matches = patterns.map((globs) =>
  micromatch(files, globs, { dot: true, windows: false, noext: true }),
);
process.stdout.write(JSON.stringify(matches));
