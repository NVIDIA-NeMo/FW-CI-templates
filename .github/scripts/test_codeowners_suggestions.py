#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest

from codeowners_suggestions import (
    find_redundant_rules,
    parse_codeowners,
    pattern_covers,
    resolve_last_match_per_file,
    to_glob,
)

# These assert the exact glob string(s) to_glob emits, which is what actually
# gets handed to step-security/changed-files' `files_yaml:` input. Verified
# separately against a real minimatch install to confirm these globs produce
# the same match/no-match results the JS port's own regex engine did.


class ToGlobTests(unittest.TestCase):
    def test_anchored_file_pattern_exact_path_no_wildcarding(self):
        self.assertEqual(to_glob("/nemo_gym/skills.py"), ["nemo_gym/skills.py"])

    def test_anchored_directory_pattern_single_dir_star_star_glob(self):
        self.assertEqual(to_glob("/nemo_gym/sandbox/"), ["nemo_gym/sandbox/**"])

    def test_unanchored_directory_pattern_matches_any_depth_via_two_globs(self):
        self.assertEqual(to_glob("docker/"), ["docker/**", "**/docker/**"])

    def test_unanchored_bare_filename_matches_any_depth_via_two_globs_no_dir_suffix(self):
        self.assertEqual(to_glob("package.json"), ["package.json", "**/package.json"])

    def test_patterns_already_containing_star_star_pass_through_unchanged(self):
        self.assertEqual(to_glob("/a/**/b.py"), ["a/**/b.py"])
        self.assertEqual(to_glob("**/docs/"), ["**/docs/**"])
        self.assertEqual(to_glob("docs/**"), ["docs/**"])

    def test_bare_star_single_star_star_glob_matches_everything(self):
        self.assertEqual(to_glob("*"), ["**"])


class ParseCodeownersTests(unittest.TestCase):
    def test_separates_enforced_lines_from_suggest_lines(self):
        result = parse_codeowners(
            "\n".join(
                [
                    "docker/ @nvidia-nemo/automation",
                    "# a plain comment, not a suggestion",
                    "# suggest: docker/ @anwithk",
                ]
            )
        )
        self.assertEqual(
            result["enforced_rules"],
            [{"pattern": "docker/", "owners": ["@nvidia-nemo/automation"]}],
        )
        self.assertEqual(
            result["suggest_rules"],
            [{"pattern": "docker/", "owners": ["@anwithk"]}],
        )


class ResolveLastMatchPerFileTests(unittest.TestCase):
    def test_works_for_individual_users_and_teams_alike(self):
        rules_with_matches = [
            {"pattern": "/docker/", "owners": ["@anwithk"], "matched_files": ["docker/Dockerfile"]},
            {
                "pattern": "/nemo_gym/sandbox/",
                "owners": ["@nvidia-nemo/gym_core"],
                "matched_files": ["nemo_gym/sandbox/aws.py"],
            },
        ]
        winners = resolve_last_match_per_file(rules_with_matches)
        self.assertEqual(winners["docker/Dockerfile"]["owners"][0], "@anwithk")
        self.assertEqual(winners["nemo_gym/sandbox/aws.py"]["owners"][0], "@nvidia-nemo/gym_core")

    def test_last_matching_rule_wins_per_codeowners_semantics(self):
        rules_with_matches = [
            # Listed first, matches every file (like a "*" backstop rule).
            {"pattern": "*", "owners": ["@nvidia-nemo/gym_architects"], "matched_files": ["nemo_gym/registry.py"]},
            # Listed second (later in the file) -- must win for the file it also matches.
            {
                "pattern": "/nemo_gym/registry.py",
                "owners": ["@nvidia-nemo/gym_core"],
                "matched_files": ["nemo_gym/registry.py"],
            },
        ]
        winners = resolve_last_match_per_file(rules_with_matches)
        self.assertEqual(winners["nemo_gym/registry.py"]["owners"][0], "@nvidia-nemo/gym_core")


class PatternCoversTests(unittest.TestCase):
    def test_directory_covers_itself_and_nested_paths_not_siblings(self):
        self.assertTrue(pattern_covers("/docker/", "/docker/"))
        self.assertTrue(pattern_covers("/docker/", "/docker/scripts/"))
        self.assertFalse(pattern_covers("/docker/", "/docker_extra/"))

    def test_star_covers_everything_nothing_covers_star_but_itself(self):
        self.assertTrue(pattern_covers("*", "/docker/"))
        self.assertFalse(pattern_covers("/docker/", "*"))

    def test_patterns_with_inner_wildcards_are_unknown_never_claimed_as_covering(self):
        self.assertFalse(pattern_covers("/nemo_gym/*.py", "/nemo_gym/registry.py"))


class FindRedundantRulesTests(unittest.TestCase):
    def test_nested_suggest_with_identical_owner_to_enforced_parent_is_flagged(self):
        enforced_rules = [{"pattern": "/docker/", "owners": ["@nvidia-nemo/automation"]}]
        suggest_rules = [{"pattern": "/docker/scripts/", "owners": ["@nvidia-nemo/automation"]}]

        redundant = find_redundant_rules(enforced_rules, suggest_rules)
        self.assertEqual(len(redundant), 1)
        self.assertEqual(redundant[0]["rule"]["pattern"], "/docker/scripts/")
        self.assertEqual(redundant[0]["covered_by"]["pattern"], "/docker/")

    def test_nested_suggest_with_different_owner_is_deliberate_override_not_flagged(self):
        enforced_rules = [{"pattern": "/docker/", "owners": ["@nvidia-nemo/automation"]}]
        suggest_rules = [{"pattern": "/docker/scripts/", "owners": ["@anwithk"]}]

        self.assertEqual(find_redundant_rules(enforced_rules, suggest_rules), [])

    def test_exact_duplicate_pattern_within_same_rule_kind_is_flagged(self):
        suggest_rules = [
            {"pattern": "/docker/", "owners": ["@anwithk"]},
            {"pattern": "/docker/", "owners": ["@anwithk"]},
        ]
        redundant = find_redundant_rules([], suggest_rules)
        self.assertEqual(len(redundant), 1)
        self.assertEqual(redundant[0]["reason"], "exact duplicate pattern")

    def test_unrelated_rules_for_disjoint_paths_are_not_flagged(self):
        enforced_rules = [{"pattern": "/docker/", "owners": ["@nvidia-nemo/automation"]}]
        suggest_rules = [{"pattern": "/nemo_gym/sandbox/", "owners": ["@nvidia-nemo/gym_core"]}]
        self.assertEqual(find_redundant_rules(enforced_rules, suggest_rules), [])


if __name__ == "__main__":
    unittest.main()
