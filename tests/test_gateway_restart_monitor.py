from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway import run as gateway_run


class GatewayRestartMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_code_change_during_startup_grace_remains_pending_until_restart(self) -> None:
        app = MagicMock()
        with patch.object(gateway_run, "_refresh_fingerprint") as refresh, patch.object(
            gateway_run.asyncio, "sleep", new=AsyncMock(side_effect=[None, None])
        ), patch.object(
            gateway_run.time, "time", side_effect=[100.0, 110.0, 140.0]
        ), patch.object(
            gateway_run.gw_restart, "code_updated", side_effect=[True, True]
        ), patch.object(
            gateway_run.gw_restart, "restart_requested", return_value=False
        ), patch.object(
            gateway_run.gw_restart, "claim_restart_leadership", return_value=False
        ), patch.object(
            gateway_run.gw_sessions, "shutdown_all"
        ), patch.object(
            gateway_run.os, "_exit", side_effect=SystemExit(0)
        ):
            with self.assertRaises(SystemExit):
                await gateway_run._restart_monitor(app)

        # Startup establishes one baseline. A code change observed during grace must
        # not overwrite that baseline, or the stale process will never restart.
        refresh.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
