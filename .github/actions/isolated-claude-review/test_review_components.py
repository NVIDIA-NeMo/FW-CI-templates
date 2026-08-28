# Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
# Licensed under the Apache License, Version 2.0.

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = Path(__file__).with_name("review_components.py")
SPEC = importlib.util.spec_from_file_location("review_components", MODULE_PATH)
review_components = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = review_components
SPEC.loader.exec_module(review_components)


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repository"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "kept.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        (self.repo / "deleted.txt").write_text("old\n", encoding="utf-8")
        (self.repo / "binary.bin").write_bytes(b"old\x00bytes")
        (self.repo / "link").symlink_to("kept.txt")
        (self.repo / "changed-link").symlink_to("kept.txt")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.repo / "kept.txt").write_text("one\nchanged\nthree\n", encoding="utf-8")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "binary.bin").write_bytes(b"new\x00bytes")
        (self.repo / "added.txt").write_text("new\n", encoding="utf-8")
        (self.repo / "changed-link").unlink()
        (self.repo / "changed-link").symlink_to("added.txt")
        self.git("mv", "kept.txt", "renamed.txt")
        self.git("add", "-A")
        self.git("commit", "-qm", "head")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.metadata = Path(self.temporary.name) / "metadata.json"
        self.metadata.write_text(json.dumps({
            "author": "contributor", "title": "change", "body": "body",
            "head_repository": "example/fork", "is_cross_repository": True,
            "changed_files": 5,
        }), encoding="utf-8")
        self.context = Path(self.temporary.name) / "context"
        self.build(self.context)

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.check_output(["git", "-C", str(self.repo), *arguments], text=True)

    def build(self, output, **overrides):
        values = dict(
            repository_dir=str(self.repo), repository="example/repository", pr_number=7,
            review_id="review-1", review_mode="manual", base_sha=self.base,
            merge_base_sha=self.base, head_sha=self.head, metadata=str(self.metadata),
            output=str(output), max_files=500, max_diff_bytes=4_000_000,
        )
        values.update(overrides)
        with redirect_stdout(io.StringIO()):
            review_components.build_context(SimpleNamespace(**values))

    def manifest(self):
        return review_components.validate_manifest(self.context)

    def output(self, **overrides):
        manifest = self.manifest()
        changed = json.loads((self.context / "changed-files.json").read_text())
        value = {
            "schema_version": review_components.SCHEMA_VERSION,
            "repository": manifest["repository"], "pull_request": manifest["pull_request"],
            "review_id": manifest["review_id"], "review_mode": manifest["review_mode"],
            "base_sha": manifest["base_sha"], "merge_base_sha": manifest["merge_base_sha"],
            "head_sha": manifest["head_sha"], "context_digest": manifest["context_digest"],
            "status": "complete",
            "coverage": {"changed_files_reviewed": len(changed), "changed_files_total": len(changed), "diff_complete": True, "notes": ""},
            "inline_findings": [], "general_findings": [], "summary": "No findings.",
            "clean_review": True,
        }
        value.update(overrides)
        return value, manifest, changed


class ContextTests(RepositoryFixture):
    def test_captures_revisions_metadata_and_special_objects(self):
        manifest = self.manifest()
        metadata = json.loads((self.context / "metadata.json").read_text())
        changed = json.loads((self.context / "changed-files.json").read_text())
        self.assertEqual((manifest["base_sha"], manifest["merge_base_sha"], manifest["head_sha"]), (self.base, self.base, self.head))
        self.assertTrue(metadata["is_cross_repository"])
        records = {record["new_path"] or record["old_path"]: record for record in changed}
        self.assertEqual(records["binary.bin"]["head"]["reason"], "binary")
        self.assertEqual(records["changed-link"]["base"]["reason"], "symlink")

    def test_rejects_incorrect_merge_base(self):
        with self.assertRaisesRegex(review_components.ReviewError, "MERGE_BASE_SHA"):
            self.build(Path(self.temporary.name) / "bad", base_sha=self.head)

    def test_rejects_large_context(self):
        with self.assertRaisesRegex(review_components.ReviewError, "limits"):
            self.build(Path(self.temporary.name) / "large", max_files=1)

    def test_context_tampering_is_rejected(self):
        (self.context / "review.diff").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(review_components.ReviewError, "digest"):
            review_components.validate_manifest(self.context)


class RetrieverTests(RepositoryFixture):
    def retrieve(self, **overrides):
        values = dict(context=str(self.context), audit=str(self.context / "audit.jsonl"), operation="changed-files", snapshot=None, path=None, query=None, offset=0, limit=100, byte_limit=65536)
        values.update(overrides)
        with mock.patch.object(sys, "stdout", mock.MagicMock()) as stdout:
            stdout.buffer = io.BytesIO()
            review_components.retriever(SimpleNamespace(**values))
            return stdout.buffer.getvalue()

    def test_changed_files_are_paginated_and_audited(self):
        value = json.loads(self.retrieve(limit=2))
        self.assertEqual(len(value["entries"]), 2)
        self.assertTrue((self.context / "audit.jsonl").is_file())

    def test_traversal_is_rejected(self):
        with self.assertRaises(review_components.ReviewError):
            self.retrieve(operation="read", snapshot="head", path="../outside")

    def test_symlink_and_binary_are_not_retrievable(self):
        for snapshot, path in (("base", "link"), ("head", "binary.bin")):
            with self.assertRaises(review_components.ReviewError):
                self.retrieve(operation="read", snapshot=snapshot, path=path)

    def test_search_is_literal_and_bounded(self):
        value = json.loads(self.retrieve(operation="search", snapshot="head", query="changed", limit=2))
        self.assertEqual(value["matches"][0]["path"], "renamed.txt")


class OutputTests(RepositoryFixture):
    def test_accepts_complete_clean_output(self):
        output, manifest, changed = self.output()
        self.assertIs(review_components.validate_output_document(output, manifest, changed), output)

    def test_rejects_unknown_field_and_incomplete_coverage(self):
        output, manifest, changed = self.output(unexpected=True)
        with self.assertRaises(review_components.ReviewError):
            review_components.validate_output_document(output, manifest, changed)
        output, manifest, changed = self.output()
        output["coverage"]["changed_files_reviewed"] = 0
        with self.assertRaises(review_components.ReviewError):
            review_components.validate_output_document(output, manifest, changed)

    def test_validates_deletion_side_and_lines(self):
        output, manifest, changed = self.output()
        output["inline_findings"] = [{"path": "deleted.txt", "side": "LEFT", "line": 1, "severity": "medium", "category": "correctness", "body": "Finding"}]
        output["clean_review"] = False
        review_components.validate_output_document(output, manifest, changed)
        output["inline_findings"][0]["line"] = 999
        with self.assertRaises(review_components.ReviewError):
            review_components.validate_output_document(output, manifest, changed)

    def test_rejects_duplicate_and_oversized_findings(self):
        output, manifest, changed = self.output()
        finding = {"path": "deleted.txt", "side": "LEFT", "line": 1, "severity": "medium", "category": "correctness", "body": "Finding"}
        output["inline_findings"] = [finding, finding]
        output["clean_review"] = False
        with self.assertRaises(review_components.ReviewError):
            review_components.validate_output_document(output, manifest, changed)


class PublisherContractTests(unittest.TestCase):
    def test_exchange_masks_token(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"token": "app-token"}).encode()
        with mock.patch.object(review_components.urllib.request, "urlopen", return_value=response) as urlopen, mock.patch("builtins.print") as printing:
            token = review_components.exchange_publisher_token("oidc")
        self.assertEqual(token, "app-token")
        self.assertEqual(json.loads(urlopen.call_args.args[0].data), {"permissions": {"contents": "read", "pull_requests": "write", "issues": "write"}})
        printing.assert_called_with("::add-mask::app-token")


if __name__ == "__main__":
    unittest.main()
