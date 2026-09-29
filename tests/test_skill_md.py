#!/usr/bin/env python3
"""Check SKILL.md against the Agent Skills specification (agentskills.io/specification)."""

import re
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "splitscreen"


def frontmatter(text: str) -> dict:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if match is None:
        raise AssertionError("SKILL.md must start with YAML frontmatter")
    return dict(re.findall(r"^([a-z][a-z-]*): (.+)$", match.group(1), re.M))


class SkillMdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.fields = frontmatter(self.text)

    def test_name_follows_the_spec_and_matches_the_directory(self) -> None:
        name = self.fields["name"]
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(name), 64)
        self.assertEqual(name, SKILL_DIR.name)

    def test_description_and_compatibility_lengths(self) -> None:
        self.assertTrue(0 < len(self.fields["description"]) <= 1024)
        self.assertTrue(0 < len(self.fields["compatibility"]) <= 500)

    def test_skill_stays_small_and_ships_its_script_and_license(self) -> None:
        self.assertLess(self.text.count("\n"), 500)
        self.assertTrue((SKILL_DIR / "scripts" / "splitscreen.py").is_file())
        self.assertTrue((SKILL_DIR / "LICENSE").is_file())


if __name__ == "__main__":
    unittest.main()
