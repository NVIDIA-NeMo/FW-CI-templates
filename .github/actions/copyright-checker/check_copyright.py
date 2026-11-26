#!/usr/bin/env python3
"""
Script to check and optionally add NVIDIA copyright headers to files.
"""

import sys
import argparse
import codecs
import glob
import os
import shlex
import re  # Import the regular expression module
from pathlib import Path
from datetime import datetime

# Regex to check for in leading comment lines.
# Supports both legacy NVIDIA copyright headers and SPDX FileCopyrightText.
HEADER_REGEX = re.compile(r"^#\s*(?:Copyright|SPDX-FileCopyrightText:\s*Copyright)")

# Original format string, still used for the error message suggestion
EXPECTED_HEADER = """# Copyright (c) {} NVIDIA CORPORATION & AFFILIATES. All rights reserved."""


def split_changed_files(value):
    """Read legacy shell-quoted filenames, including Bash printf %q output."""

    def unquote(match, protect_globs):
        """Preserve quoted filename segments during word splitting and globbing."""
        part = match[0]
        if part.startswith("$'"):
            raw = os.fsencode(part[2:-1])
            raw = re.sub(
                rb"\\.",
                lambda esc: rb"\x1b" if esc[0] in (rb"\e", rb"\E") else esc[0],
                raw,
                flags=re.DOTALL,
            )
            literal = os.fsdecode(codecs.escape_decode(raw)[0])
        elif part.startswith('"'):
            literal = re.sub(
                r'\\([$`"\\\n])', lambda esc: esc[1].strip('\n'), part[1:-1]
            )
        elif part.startswith("'"):
            literal = part[1:-1]
        elif part == "\\\n":
            return ""
        else:
            literal = part[1:]
        return shlex.quote(glob.escape(literal) if protect_globs else literal)

    quoting = re.compile(
        r"""\\.|\$'(?:\\.|[^'\\])*'|'[^']*'|"(?:\\.|[^"\\])*" """,
        re.DOTALL | re.VERBOSE,
    )
    try:
        words = shlex.split(quoting.sub(lambda part: unquote(part, False), value))
        patterns = shlex.split(quoting.sub(lambda part: unquote(part, True), value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid changed-files quoting: {exc}") from exc

    paths = []
    for word, pattern in zip(words, patterns):
        matches = sorted(glob.glob(pattern)) if glob.has_magic(pattern) else []
        paths.extend(matches or [word])
    return paths


def has_correct_header(file_path):
    """Check if file's first line has the correct copyright header."""
    with open(file_path, 'r', encoding='utf-8') as f:
        # Read only the first line
        lines = f.readlines()

    for line in lines:
        if not line.startswith("#"):
            return False

        if HEADER_REGEX.search(line) is not None:
            return True


def main():
    parser = argparse.ArgumentParser(
        description='Check and add NVIDIA copyright headers to files.'
    )
    parser.add_argument(
        'files',
        nargs='*',
        help='Files to check/modify'
    )
    parser.add_argument(
        '--changed-files', type=split_changed_files, default=[], help=argparse.SUPPRESS
    )

    args = parser.parse_args()
    files = args.files + args.changed_files
    if not files:
        parser.error('provide at least one file to check')

    missing_headers = []

    for file_path in files:
        path = Path(file_path)

        if not path.exists():
            print(f"File not found: {file_path!r}")
            continue

        if not path.is_file():
            print(f"Not a file: {file_path!r}")
            continue

        if not has_correct_header(path):
            print(f"✗ Header missing: {file_path!r}")
            missing_headers.append(path)

    # Exit with error code if headers are missing and not added
    if missing_headers:
        print(f"\n{len(missing_headers)} file(s) missing copyright header.")
        print("\n")
        # The error message still suggests a specific header to add
        print("Add the header in those files with the following content:")
        print(EXPECTED_HEADER.format(str(datetime.now().year)))
        print("\n")
        print(
            "Disclaimer: This must done irrespective of the magnitude of the change "
            "or whether your are the file/module author."
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
