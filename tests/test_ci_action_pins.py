import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
ACTION_PATTERN = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
PIN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


class WorkflowActionPinTests(unittest.TestCase):
    def test_every_external_action_is_pinned_to_a_full_commit(self):
        discovered = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            discovered.extend(ACTION_PATTERN.findall(path.read_text(encoding="utf-8")))

        self.assertTrue(discovered)
        self.assertTrue(all(PIN_PATTERN.fullmatch(reference) for reference in discovered))
        self.assertEqual(
            set(discovered),
            {
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
                "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            },
        )


if __name__ == "__main__":
    unittest.main()
