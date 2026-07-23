from __future__ import annotations

import unittest

import kara


class FileWorkToolRegistrationTests(unittest.TestCase):
    def test_file_office_sql_and_python_tools_are_registered_and_schema_visible(self) -> None:
        expected = {
            "file_info",
            "copy_file",
            "move_file",
            "replace_in_file",
            "read_office_file",
            "create_word_document",
            "append_word_text",
            "create_excel_workbook",
            "set_excel_cell",
            "create_powerpoint",
            "append_powerpoint_slide",
            "inspect_sqlite_database",
            "query_sqlite_database",
            "inspect_python_file",
            "validate_python_file",
            "run_python_tests",
            "schedule_reminder",
            "schedule_agent_job",
            "list_scheduled_jobs",
            "pause_scheduled_job",
            "resume_scheduled_job",
            "delete_scheduled_job",
            "run_scheduled_job_now",
        }
        schema_names = {item["function"]["name"] for item in kara.TOOL_SCHEMAS}

        self.assertTrue(expected.issubset(kara.TOOL_REGISTRY))
        self.assertTrue(expected.issubset(schema_names))


if __name__ == "__main__":
    unittest.main()
