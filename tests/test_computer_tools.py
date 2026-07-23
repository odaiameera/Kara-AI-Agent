from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tools import computer_tools


CHATGPT = {
    "pid": 10,
    "window_id": 20,
    "title": "ChatGPT",
    "app_name": "ChatGPT.exe",
}
BRAVE = {
    "pid": 30,
    "window_id": 40,
    "title": "ChatGPT - Brave",
    "app_name": "brave.exe",
}
EXPLORER = {
    "pid": 50,
    "window_id": 60,
    "title": "Downloads",
    "app_name": "explorer.exe",
}


class ComputerToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        computer_tools._pending.clear()
        computer_tools._last_targets.clear()

    def _request_token(
        self, action: str, target: dict, *, user_input: str = "do it", **kwargs
    ) -> str:
        computer_tools.set_computer_request_context("s1", user_input)
        with patch.object(computer_tools, "_resolve_target", return_value=dict(target)):
            payload = json.loads(computer_tools.computer_use(action, **kwargs))
        self.assertEqual(payload["status"], "approval_required")
        self.assertEqual(payload["target"], target)
        return payload["approval"]["token"]

    def test_read_only_app_listing_needs_no_approval(self) -> None:
        computer_tools.set_computer_request_context("s1", "what apps are open?")
        with patch.object(
            computer_tools,
            "_call_driver",
            return_value={"apps": [{"name": "Notepad", "pid": 10, "running": True}]},
        ):
            result = json.loads(computer_tools.computer_use("list_apps"))
        self.assertEqual(result["running_apps"][0]["name"], "Notepad")

    def test_approval_requires_exact_later_user_reply(self) -> None:
        token = self._request_token("click", CHATGPT, app="ChatGPT", element=4)

        # The model cannot reuse a token in the original user-authored turn.
        with patch.object(computer_tools, "_resolve_target", return_value=dict(CHATGPT)):
            same_turn = json.loads(
                computer_tools.computer_use(
                    "click", app="ChatGPT", element=4, approval_token=token
                )
            )
        self.assertEqual(same_turn["error"]["code"], "approval_not_confirmed")

        computer_tools.set_computer_request_context("s1", f"please approve {token}")
        with patch.object(computer_tools, "_resolve_target", return_value=dict(CHATGPT)):
            inexact = json.loads(
                computer_tools.computer_use(
                    "click", app="ChatGPT", element=4, approval_token=token
                )
            )
        self.assertEqual(inexact["error"]["code"], "approval_not_confirmed")

    def test_resolver_prefers_exact_chatgpt_app_over_brave_tab_title(self) -> None:
        windows = [
            {**CHATGPT, "is_on_screen": True, "z_index": 2},
            {**BRAVE, "is_on_screen": True, "z_index": 9},
        ]
        apps = [
            {"name": "ChatGPT", "pid": 10, "running": True, "active": False},
            {"name": "Brave", "pid": 30, "running": True, "active": True},
        ]
        with patch.object(computer_tools, "_windows", return_value=windows), patch.object(
            computer_tools, "_apps", return_value=apps
        ), patch.object(
            computer_tools,
            "_foreground_window",
            return_value={"pid": 30, "window_id": 40},
        ):
            target = computer_tools._resolve_target("ChatGPT", 0, 0)
        self.assertEqual(target, CHATGPT)

    def test_resolver_prefers_large_electron_window_over_small_helper(self) -> None:
        small = {
            **CHATGPT,
            "window_id": 21,
            "bounds": {"width": 356, "height": 320},
            "is_on_screen": True,
            "z_index": 7,
        }
        main = {
            **CHATGPT,
            "window_id": 20,
            "bounds": {"width": 1280, "height": 820},
            "is_on_screen": True,
            "z_index": 6,
        }
        apps = [{"name": "ChatGPT", "pid": 10, "running": True, "active": False}]
        with patch.object(
            computer_tools, "_windows", return_value=[small, main]
        ), patch.object(computer_tools, "_apps", return_value=apps), patch.object(
            computer_tools,
            "_foreground_window",
            return_value={"pid": 999, "window_id": 888},
        ):
            target = computer_tools._resolve_target("ChatGPT", 0, 0)
        self.assertEqual(target["window_id"], 20)

    def test_native_window_already_foreground_sends_key_without_activation(self) -> None:
        token = self._request_token("key", EXPLORER, app="Explorer", key="f5")
        computer_tools.set_computer_request_context("s1", f"approve {token}")
        with patch.object(computer_tools, "_resolve_target", return_value=dict(EXPLORER)), patch.object(
            computer_tools, "_verify_bound_target", return_value={"ok": True}
        ), patch.object(
            computer_tools,
            "_foreground_window",
            return_value={"pid": 50, "window_id": 60},
        ), patch.object(
            computer_tools, "_call_driver", return_value={"success": True}
        ) as driver:
            payload = json.loads(
                computer_tools.computer_use(
                    "key", app="Explorer", key="f5", approval_token=token
                )
            )
        self.assertTrue(payload["ok"])
        driver.assert_called_once()
        self.assertEqual(driver.call_args.args[0], "press_key")
        self.assertEqual(driver.call_args.args[1]["delivery_mode"], "foreground")

    def test_background_chatgpt_alt_f4_activates_then_uses_foreground_hotkey(self) -> None:
        token = self._request_token(
            "key", CHATGPT, app="ChatGPT", key="f4", modifiers=["alt"]
        )
        computer_tools.set_computer_request_context("s1", f"approve {token}")
        foregrounds = [
            {"pid": 999, "window_id": 888},
            {"pid": 10, "window_id": 20},
        ]

        def driver_call(tool: str, args: dict, timeout: float = 35.0) -> dict:
            if tool == "bring_to_front":
                return {"raised": True, "landed_on_target": True, "now_fg_hwnd": 20}
            if tool == "hotkey":
                return {"success": True}
            self.fail(f"Unexpected driver tool: {tool}")

        with patch.object(computer_tools, "_resolve_target", return_value=dict(CHATGPT)), patch.object(
            computer_tools, "_verify_bound_target", return_value={"ok": True}
        ), patch.object(
            computer_tools, "_foreground_window", side_effect=foregrounds
        ), patch.object(
            computer_tools, "_current_window_identity", return_value=dict(CHATGPT)
        ), patch.object(
            computer_tools.time, "sleep"
        ), patch.object(
            computer_tools, "_call_driver", side_effect=driver_call
        ) as driver:
            payload = json.loads(
                computer_tools.computer_use(
                    "key",
                    app="ChatGPT",
                    key="f4",
                    modifiers=["alt"],
                    approval_token=token,
                )
            )

        self.assertTrue(payload["ok"])
        self.assertEqual([call.args[0] for call in driver.call_args_list], ["bring_to_front", "hotkey"])
        bring_args = driver.call_args_list[0].args[1]
        self.assertEqual(bring_args, {"pid": 10, "window_id": 20})
        hotkey_args = driver.call_args_list[1].args[1]
        self.assertEqual(hotkey_args["keys"], ["alt", "f4"])
        self.assertEqual(hotkey_args["delivery_mode"], "foreground")
        self.assertEqual((hotkey_args["pid"], hotkey_args["window_id"]), (10, 20))

    def test_failure_to_foreground_sends_no_keyboard_input(self) -> None:
        token = self._request_token("key", CHATGPT, app="ChatGPT", key="f4", modifiers=["alt"])
        computer_tools.set_computer_request_context("s1", f"approve {token}")

        with patch.object(computer_tools, "_resolve_target", return_value=dict(CHATGPT)), patch.object(
            computer_tools, "_verify_bound_target", return_value={"ok": True}
        ), patch.object(
            computer_tools,
            "_foreground_window",
            side_effect=[{"pid": 999, "window_id": 888}, {"pid": 999, "window_id": 888}],
        ), patch.object(
            computer_tools, "_current_window_identity", return_value=dict(CHATGPT)
        ), patch.object(
            computer_tools.time, "sleep"
        ), patch.object(
            computer_tools,
            "_call_driver",
            return_value={"raised": True, "landed_on_target": False, "now_fg_hwnd": 888},
        ) as driver:
            payload = json.loads(
                computer_tools.computer_use(
                    "key",
                    app="ChatGPT",
                    key="f4",
                    modifiers=["alt"],
                    approval_token=token,
                )
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "foreground_activation_failed")
        self.assertEqual([call.args[0] for call in driver.call_args_list], ["bring_to_front"])

    def test_explicit_bring_to_front_is_approval_gated_verified_and_one_use(self) -> None:
        token = self._request_token("bring_to_front", CHATGPT, app="ChatGPT")
        computer_tools.set_computer_request_context("s1", f"approve {token}")
        foreground = {
            "ok": True,
            "verified": True,
            "activated": True,
            "final": {"pid": 10, "window_id": 20},
        }
        with patch.object(computer_tools, "_resolve_target", return_value=dict(CHATGPT)), patch.object(
            computer_tools, "_verify_bound_target", return_value={"ok": True}
        ), patch.object(
            computer_tools, "_ensure_foreground", return_value=foreground
        ) as ensure:
            payload = json.loads(
                computer_tools.computer_use(
                    "bring_to_front", app="ChatGPT", approval_token=token
                )
            )
        self.assertTrue(payload["ok"])
        ensure.assert_called_once_with(CHATGPT)

        # A completed mutating action consumes its token.
        computer_tools.set_computer_request_context("s1", f"approve {token}")
        with patch.object(computer_tools, "_resolve_target", return_value=dict(CHATGPT)), patch.object(
            computer_tools, "_ensure_foreground"
        ) as ensure_again:
            reused = json.loads(
                computer_tools.computer_use(
                    "bring_to_front", app="ChatGPT", approval_token=token
                )
            )
        self.assertEqual(reused["error"]["code"], "approval_invalid")
        ensure_again.assert_not_called()

    def test_changed_window_title_after_approval_is_rejected(self) -> None:
        token = self._request_token("key", CHATGPT, app="ChatGPT", key="f4", modifiers=["alt"])
        changed = {**CHATGPT, "title": "Sign in - ChatGPT"}
        computer_tools.set_computer_request_context("s1", f"approve {token}")
        with patch.object(
            computer_tools, "_current_window_identity", return_value=changed
        ), patch.object(
            computer_tools, "_call_driver"
        ) as driver:
            payload = json.loads(
                computer_tools.computer_use(
                    "key",
                    app="ChatGPT",
                    key="f4",
                    modifiers=["alt"],
                    approval_token=token,
                )
            )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "target_changed_after_approval")
        self.assertFalse(payload["action_sent"])
        driver.assert_not_called()

    def test_native_explorer_click_stays_background(self) -> None:
        token = self._request_token("click", EXPLORER, app="Explorer", element=3)
        computer_tools.set_computer_request_context("s1", f"approve {token}")
        with patch.object(computer_tools, "_resolve_target", return_value=dict(EXPLORER)), patch.object(
            computer_tools, "_verify_bound_target", return_value={"ok": True}
        ), patch.object(
            computer_tools, "_ensure_foreground"
        ) as ensure, patch.object(
            computer_tools, "_call_driver", return_value={"success": True}
        ) as driver:
            payload = json.loads(
                computer_tools.computer_use(
                    "click", app="Explorer", element=3, approval_token=token
                )
            )
        self.assertTrue(payload["ok"])
        ensure.assert_not_called()
        self.assertEqual(driver.call_args.args[0], "click")
        self.assertEqual(driver.call_args.args[1]["delivery_mode"], "background")

    def test_chromium_background_click_escalates_same_target_with_same_approval(self) -> None:
        token = self._request_token("click", CHATGPT, app="ChatGPT", element=4)
        computer_tools.set_computer_request_context("s1", f"approve {token}")
        foreground = {
            "ok": True,
            "verified": True,
            "activated": True,
            "final": {"pid": 10, "window_id": 20},
        }

        def driver_call(tool: str, args: dict, timeout: float = 35.0) -> dict:
            if len(driver.call_args_list) == 1:
                raise computer_tools.DriverCallError(
                    "background_unavailable",
                    {"code": "background_unavailable"},
                )
            return {"success": True}

        with patch.object(computer_tools, "_resolve_target", return_value=dict(CHATGPT)), patch.object(
            computer_tools, "_verify_bound_target", return_value={"ok": True}
        ), patch.object(
            computer_tools, "_ensure_foreground", return_value=foreground
        ) as ensure, patch.object(
            computer_tools, "_call_driver", side_effect=driver_call
        ) as driver:
            payload = json.loads(
                computer_tools.computer_use(
                    "click", app="ChatGPT", element=4, approval_token=token
                )
            )
        self.assertTrue(payload["ok"])
        ensure.assert_called_once_with(CHATGPT)
        self.assertEqual([call.args[0] for call in driver.call_args_list], ["click", "click"])
        self.assertEqual(driver.call_args_list[0].args[1]["delivery_mode"], "background")
        self.assertEqual(driver.call_args_list[1].args[1]["delivery_mode"], "foreground")
        self.assertEqual(driver.call_args_list[1].args[1]["window_id"], 20)


if __name__ == "__main__":
    unittest.main()
