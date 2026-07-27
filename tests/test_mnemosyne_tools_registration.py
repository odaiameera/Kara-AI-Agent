from __future__ import annotations

import unittest

import kara


class MnemosyneToolRegistrationTests(unittest.TestCase):
    def test_mnemosyne_tools_are_registered_and_schema_visible(self) -> None:
        expected = {
            "mnemosyne_status",
            "mnemosyne_remember",
            "mnemosyne_recall",
            "mnemosyne_call_tool",
        }
        schema_names = {item["function"]["name"] for item in kara.TOOL_SCHEMAS}

        self.assertTrue(expected.issubset(kara.TOOL_REGISTRY))
        self.assertTrue(expected.issubset(schema_names))


if __name__ == "__main__":
    unittest.main()
