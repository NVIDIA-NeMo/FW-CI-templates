// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/** Match rule globs as gitignore-style CODEOWNERS patterns: no extglobs, no brace expansion,
 * no negation, and a directory pattern never matches a same-named file. */
const fs = require("node:fs");
const micromatch = require("micromatch");
const { files, patterns } = JSON.parse(fs.readFileSync(0, "utf8"));
const matches = patterns.map((globs) =>
  micromatch(files, globs, { dot: true, windows: false, noext: true, nobrace: true, nonegate: true, strictSlashes: true }),
);
process.stdout.write(JSON.stringify(matches));
