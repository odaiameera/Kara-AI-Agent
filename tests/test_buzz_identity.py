from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import buzz_identity
from buzz_identity import BuzzIdentityError, CheckResult, CommandOutput

OWNER_PUBKEY = "a" * 64
AGENT_PUBKEY = "b" * 64
SIGNATURE = "c" * 128


def auth_tag(conditions: str = "") -> str:
    return json.dumps(["auth", OWNER_PUBKEY, conditions, SIGNATURE])


def ok(payload: object) -> CommandOutput:
    return CommandOutput(0, json.dumps(payload), "")


def relay_error(message: str) -> CommandOutput:
    return CommandOutput(3, "", json.dumps({"error": "auth_error", "message": message}))


class AuthTagParsingTests(unittest.TestCase):
    def test_parses_a_well_formed_tag(self) -> None:
        tag = buzz_identity.parse_auth_tag(auth_tag("created_at<1790000000"))
        self.assertEqual(tag.owner_pubkey, OWNER_PUBKEY)
        self.assertEqual(tag.conditions, "created_at<1790000000")

    def test_repr_never_contains_the_signature(self) -> None:
        tag = buzz_identity.parse_auth_tag(auth_tag())
        self.assertNotIn(SIGNATURE, repr(tag))
        self.assertIn("redacted", repr(tag))

    def test_rejects_non_json(self) -> None:
        with self.assertRaises(BuzzIdentityError):
            buzz_identity.parse_auth_tag("not json")

    def test_rejects_non_array(self) -> None:
        with self.assertRaises(BuzzIdentityError) as ctx:
            buzz_identity.parse_auth_tag(json.dumps({"auth": OWNER_PUBKEY}))
        self.assertIn("JSON array", str(ctx.exception))

    def test_rejects_wrong_element_count(self) -> None:
        with self.assertRaises(BuzzIdentityError) as ctx:
            buzz_identity.parse_auth_tag(json.dumps(["auth", OWNER_PUBKEY, ""]))
        self.assertIn("4 elements", str(ctx.exception))

    def test_rejects_wrong_label(self) -> None:
        with self.assertRaises(BuzzIdentityError) as ctx:
            buzz_identity.parse_auth_tag(json.dumps(["oa", OWNER_PUBKEY, "", SIGNATURE]))
        self.assertIn("label", str(ctx.exception))

    def test_rejects_bad_owner_pubkey(self) -> None:
        with self.assertRaises(BuzzIdentityError) as ctx:
            buzz_identity.parse_auth_tag(json.dumps(["auth", "zz", "", SIGNATURE]))
        self.assertIn("owner pubkey", str(ctx.exception))

    def test_rejects_bad_signature_length(self) -> None:
        with self.assertRaises(BuzzIdentityError) as ctx:
            buzz_identity.parse_auth_tag(json.dumps(["auth", OWNER_PUBKEY, "", "c" * 64]))
        self.assertIn("signature", str(ctx.exception))


class ConditionsTests(unittest.TestCase):
    def test_empty_conditions_are_allowed(self) -> None:
        buzz_identity.validate_conditions("")

    def test_multiple_clauses_are_allowed(self) -> None:
        buzz_identity.validate_conditions("created_at<1790000000&kind=9")

    def test_whitespace_is_rejected(self) -> None:
        with self.assertRaises(BuzzIdentityError):
            buzz_identity.validate_conditions("created_at < 1790000000")

    def test_empty_clause_is_rejected(self) -> None:
        for value in ("&kind=9", "kind=9&", "kind=9&&created_at<1"):
            with self.subTest(value=value):
                with self.assertRaises(BuzzIdentityError):
                    buzz_identity.validate_conditions(value)


class RedactionTests(unittest.TestCase):
    def test_reports_presence_not_value(self) -> None:
        with patch.dict("os.environ", {buzz_identity.ENV_PRIVATE_KEY: "nsec1secretvalue"}):
            described = buzz_identity.redact(buzz_identity.ENV_PRIVATE_KEY)
        self.assertNotIn("nsec1secretvalue", described)
        self.assertIn(f"{len('nsec1secretvalue')} chars", described)

    def test_reports_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(buzz_identity.redact(buzz_identity.ENV_PRIVATE_KEY), "not set")

    def test_environment_check_never_echoes_values(self) -> None:
        env = {
            buzz_identity.ENV_RELAY_URL: "wss://relay.example",
            buzz_identity.ENV_PRIVATE_KEY: "nsec1secretvalue",
            buzz_identity.ENV_AUTH_TAG: auth_tag(),
        }
        with patch.dict("os.environ", env, clear=True):
            result = buzz_identity.check_environment()
        self.assertTrue(result.ok)
        self.assertNotIn("nsec1secretvalue", result.detail)
        self.assertNotIn(SIGNATURE, result.detail)


class RunBuzzGuardTests(unittest.TestCase):
    def test_write_commands_are_refused_before_execution(self) -> None:
        for args in (
            ["messages", "send", "--content", "hi"],
            ["channels", "add-member", "--pubkey", AGENT_PUBKEY],
            ["moderation", "ban", AGENT_PUBKEY],
            ["agents", "archive", AGENT_PUBKEY],
        ):
            with self.subTest(args=args):
                with patch("subprocess.run") as runner:
                    with self.assertRaises(BuzzIdentityError):
                        buzz_identity.run_buzz(args)
                runner.assert_not_called()

    def test_read_commands_are_allowed(self) -> None:
        with patch("subprocess.run") as runner:
            runner.return_value.returncode = 0
            runner.return_value.stdout = "[]"
            runner.return_value.stderr = ""
            buzz_identity.run_buzz(["channels", "list"])
        runner.assert_called_once()


class CheckPipelineTests(unittest.TestCase):
    def _runner(self, responses: dict[tuple[str, ...], CommandOutput]):
        def run(args):
            return responses[tuple(args)]

        return run

    def test_auth_tag_gate_fails_closed_when_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = buzz_identity.check_auth_tag()
        self.assertFalse(result.ok)
        self.assertIn("relay_membership_required", result.remediation)

    def test_relay_membership_failure_is_named_as_such(self) -> None:
        runner = self._runner(
            {("channels", "list"): relay_error("relay error 403: relay_membership_required")}
        )
        result = buzz_identity.check_relay_membership(runner)
        self.assertFalse(result.ok)
        self.assertEqual(result.name, "relay-membership")
        self.assertIn("not enrolled", result.remediation)

    def test_identity_failure_returns_no_pubkey(self) -> None:
        runner = self._runner(
            {("users", "get"): relay_error("BUZZ_AUTH_TAG verification failed for pubkey ...")}
        )
        result, pubkey = buzz_identity.check_identity(runner)
        self.assertFalse(result.ok)
        self.assertEqual(pubkey, "")

    def test_identity_success_extracts_pubkey(self) -> None:
        runner = self._runner(
            {("users", "get"): ok([{"display_name": "Kara", "pubkey": AGENT_PUBKEY}])}
        )
        result, pubkey = buzz_identity.check_identity(runner)
        self.assertTrue(result.ok)
        self.assertEqual(pubkey, AGENT_PUBKEY)

    def test_channel_membership_detects_a_missing_member_row(self) -> None:
        channel = "19741b6e-85d1-5cd7-a593-fa0cf9392edf"
        runner = self._runner(
            {
                ("channels", "members", "--channel", channel): ok(
                    [{"pubkey": OWNER_PUBKEY, "role": "owner"}]
                )
            }
        )
        results = buzz_identity.check_channel_membership(runner, [channel], AGENT_PUBKEY)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("add-member", results[0].remediation)

    def test_channel_membership_passes_with_a_member_row(self) -> None:
        channel = "19741b6e-85d1-5cd7-a593-fa0cf9392edf"
        runner = self._runner(
            {
                ("channels", "members", "--channel", channel): ok(
                    [
                        {"pubkey": OWNER_PUBKEY, "role": "owner"},
                        {"pubkey": AGENT_PUBKEY, "role": "bot"},
                    ]
                )
            }
        )
        results = buzz_identity.check_channel_membership(runner, [channel], AGENT_PUBKEY)
        self.assertTrue(results[0].ok)
        self.assertIn("bot", results[0].detail)

    def test_channel_membership_is_skipped_when_unconfigured(self) -> None:
        results = buzz_identity.check_channel_membership(lambda args: ok([]), (), AGENT_PUBKEY)
        self.assertIsNone(results[0].ok)
        self.assertIn(buzz_identity.ENV_CHANNEL_IDS, results[0].detail)


class DiagnosisTests(unittest.TestCase):
    def test_reports_the_first_failing_gate(self) -> None:
        results = [
            CheckResult("environment", True, "ok"),
            CheckResult("relay-membership", False, "403", "enroll the identity"),
            CheckResult("channel-membership", False, "not a member", "add-member"),
        ]
        self.assertIn("enroll the identity", buzz_identity.diagnose(results))

    def test_reports_success(self) -> None:
        results = [CheckResult("environment", True, "ok"), CheckResult("identity", True, "ok")]
        self.assertIn("All gates pass", buzz_identity.diagnose(results))

    def test_flags_skipped_checks(self) -> None:
        results = [CheckResult("environment", True, "ok"), CheckResult("channel-membership", None, "skipped")]
        self.assertIn("skipped", buzz_identity.diagnose(results).lower())

    def test_report_renders_every_row(self) -> None:
        results = [
            CheckResult("environment", True, "ok"),
            CheckResult("identity", False, "boom", "fix it"),
        ]
        report = buzz_identity.format_report(results)
        self.assertIn("PASS", report)
        self.assertIn("FAIL", report)
        self.assertIn("fix it", report)


class ChannelIdParsingTests(unittest.TestCase):
    def test_parses_a_comma_separated_list(self) -> None:
        with patch.dict("os.environ", {buzz_identity.ENV_CHANNEL_IDS: " a , b ,, c "}):
            self.assertEqual(buzz_identity.configured_channel_ids(), ("a", "b", "c"))

    def test_unset_returns_empty(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(buzz_identity.configured_channel_ids(), ())


if __name__ == "__main__":
    unittest.main()
