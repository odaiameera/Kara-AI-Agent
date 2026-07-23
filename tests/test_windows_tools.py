from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tools import windows_tools


class WindowsSystemOverviewTests(unittest.TestCase):
    def test_system_overview_returns_bounded_structured_inventory(self) -> None:
        raw = {
            "ComputerName": "LAB-PC",
            "OsName": "Microsoft Windows 10 Pro",
            "OsVersion": "10.0.19045",
            "LastBootUpTime": "2026-07-23T08:00:00",
            "Manufacturer": "Example Corp",
            "Model": "Workstation",
            "TotalPhysicalMemory": 17179869184,
            "LogicalProcessors": 8,
        }
        with patch.object(windows_tools, "_run_powershell_json", return_value=raw):
            result = json.loads(windows_tools.system_overview())

        self.assertTrue(result["ok"])
        self.assertEqual(result["system"]["computer_name"], "LAB-PC")
        self.assertEqual(result["system"]["memory_bytes"], 17179869184)
        self.assertEqual(result["system"]["logical_processors"], 8)
        self.assertNotIn("environment", result)


class WindowsProcessTests(unittest.TestCase):
    def test_list_processes_filters_sorts_and_limits_without_shell_interpolation(self) -> None:
        rows = [
            {"Name": "python.exe", "ProcessId": 20, "ParentProcessId": 1, "WorkingSetSize": 500},
            {"Name": "pythonw.exe", "ProcessId": 30, "ParentProcessId": 1, "WorkingSetSize": 900},
            {"Name": "explorer.exe", "ProcessId": 40, "ParentProcessId": 1, "WorkingSetSize": 1000},
        ]
        with patch.object(windows_tools, "_run_powershell_json", return_value=rows) as runner:
            result = json.loads(
                windows_tools.list_processes(query="python'; Stop-Process -Id 1", sort_by="memory", max_results=1)
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertNotIn("Stop-Process", runner.call_args.args[0])

    def test_list_processes_returns_largest_matching_process_first(self) -> None:
        rows = [
            {"Name": "python.exe", "ProcessId": 20, "ParentProcessId": 1, "WorkingSetSize": 500},
            {"Name": "pythonw.exe", "ProcessId": 30, "ParentProcessId": 1, "WorkingSetSize": 900},
        ]
        with patch.object(windows_tools, "_run_powershell_json", return_value=rows):
            result = json.loads(windows_tools.list_processes(query="python", sort_by="memory", max_results=1))

        self.assertEqual(result["processes"][0]["pid"], 30)
        self.assertTrue(result["truncated"])


class WindowsServiceTests(unittest.TestCase):
    def test_list_services_filters_by_status_and_name(self) -> None:
        rows = [
            {"Name": "Spooler", "DisplayName": "Print Spooler", "State": "Running", "StartMode": "Auto", "ProcessId": 100},
            {"Name": "WSearch", "DisplayName": "Windows Search", "State": "Stopped", "StartMode": "Auto", "ProcessId": 0},
        ]
        with patch.object(windows_tools, "_run_powershell_json", return_value=rows):
            result = json.loads(windows_tools.list_services(query="spool", status="running", max_results=10))

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["services"][0]["name"], "Spooler")
        self.assertEqual(result["services"][0]["start_mode"], "Auto")


class WindowsScheduledTaskTests(unittest.TestCase):
    def test_list_scheduled_tasks_filters_state_and_reports_action_counts(self) -> None:
        rows = [
            {"TaskName": "KaraGateway", "TaskPath": "\\", "State": "Ready", "Principal": "Odai", "ActionCount": 1, "TriggerCount": 1},
            {"TaskName": "Maintenance", "TaskPath": "\\Lab\\", "State": "Disabled", "Principal": "SYSTEM", "ActionCount": 2, "TriggerCount": 1},
        ]
        with patch.object(windows_tools, "_run_powershell_json", return_value=rows):
            result = json.loads(
                windows_tools.list_scheduled_tasks(query="kara", state="ready", max_results=10)
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["tasks"][0]["name"], "KaraGateway")
        self.assertEqual(result["tasks"][0]["action_count"], 1)


class WindowsDiskTests(unittest.TestCase):
    def test_disk_usage_calculates_used_space_and_percent(self) -> None:
        rows = [
            {"DeviceID": "C:", "VolumeName": "Windows", "FileSystem": "NTFS", "Size": 1000, "FreeSpace": 250},
            {"DeviceID": "D:", "VolumeName": "Data", "FileSystem": "NTFS", "Size": 0, "FreeSpace": 0},
        ]
        with patch.object(windows_tools, "_run_powershell_json", return_value=rows):
            result = json.loads(windows_tools.disk_usage())

        self.assertTrue(result["ok"])
        self.assertEqual(result["disks"][0]["used_bytes"], 750)
        self.assertEqual(result["disks"][0]["free_percent"], 25.0)
        self.assertEqual(result["disks"][1]["free_percent"], 0.0)


class WindowsToolRegistrationTests(unittest.TestCase):
    def test_all_windows_inspection_tools_are_exposed_to_the_agent(self) -> None:
        from kara import TOOL_REGISTRY, TOOL_SCHEMAS

        expected = {
            "system_overview",
            "list_processes",
            "list_services",
            "list_scheduled_tasks",
            "disk_usage",
        }
        self.assertTrue(expected.issubset(TOOL_REGISTRY))
        schema_names = {item["function"]["name"] for item in TOOL_SCHEMAS}
        self.assertTrue(expected.issubset(schema_names))


if __name__ == "__main__":
    unittest.main()
