"""Public documentation must work without private development material."""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = ["README.md", "README.en.md", "CONTRIBUTING.md", "examples/README.md",
    "tests/data/project-owned/README.md", "docs/design/design-spec.md", "docs/design/execution-record.md"]
PUBLIC_DOCS += ["docs/" + name + ".md" for name in (
    "README", "install", "capabilities", "comparison-workflow", "controlled-aut", "batch-workflow",
    "parameter-scan", "performance-workflow", "scheme-comparison", "deliverable",
    "field-set-and-seq-import", "glass-catalog")]


def markdown_text(path: Path) -> str:
    return re.sub(r"```[\s\S]*?```", "", path.read_text(encoding="utf-8"))


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    result = set()
    for heading in re.findall(r"^#{1,6}\s+(.+)$", markdown_text(path), re.M):
        name = re.sub(r"[^\w\-\s]", "", heading.lower()).replace(" ", "-")
        duplicate = seen.get(name, 0)
        seen[name] = duplicate + 1
        result.add(name if not duplicate else f"{name}-{duplicate}")
    return result


class PublicDocumentationTests(unittest.TestCase):
    def test_relative_links_and_fragments(self):
        for relative in PUBLIC_DOCS:
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            for destination in re.findall(r"\[[^\]]+\]\(([^)]+)\)", markdown_text(path)):
                if re.match(r"(?:https?|mailto):", destination):
                    continue
                target, _, fragment = unquote(destination.strip('<>')).partition('#')
                resolved = (path.parent / target).resolve() if target else path
                with self.subTest(document=relative, link=destination):
                    self.assertTrue(resolved.is_relative_to(ROOT), destination)
                    self.assertTrue(resolved.is_file(), destination)
                    self.assertFalse(any(part in {"evidence", "reports", "references", "tasks", "scripts"}
                                         for part in resolved.relative_to(ROOT).parts), destination)
                    if fragment:
                        self.assertIn(fragment, anchors(resolved))

    def test_no_maintainer_paths_or_private_commands(self):
        for relative in PUBLIC_DOCS:
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(document=relative):
                self.assertNotRegex(text, r"(?i)[CD]:[\\/](?:codev-mcp|CODEV102|Python313)|[A-Z]:[\\/]Users[\\/]")
                self.assertNotRegex(text, r"(?:python(?:\.exe)?\s+scripts[\\/]|docs/(?:evidence|reports)/)")


if __name__ == "__main__":
    unittest.main()
