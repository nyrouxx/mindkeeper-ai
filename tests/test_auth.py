import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from support import component

a = component("auth")


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_ignores_unrelated_notifications(self):
        login = a.DeviceLogin(Path("/private"))
        login.read = AsyncMock(
            side_effect=[
                {"method": "account/updated", "params": {}},
                {"id": 2, "result": {"ok": True}},
            ]
        )
        self.assertEqual(await login.response(2), {"ok": True})

    async def test_login_rejection_is_not_success(self):
        login = a.DeviceLogin(Path("/private"))
        login.read = AsyncMock(
            return_value={
                "method": "account/login/completed",
                "params": {"success": False, "error": "private upstream detail"},
            }
        )
        login.close = AsyncMock()
        with self.assertRaises(a.CodexError) as caught:
            await login.complete()
        self.assertNotIn("private upstream detail", str(caught.exception))
        login.close.assert_awaited_once()

    async def test_cancellation_closes_login_process(self):
        login = a.DeviceLogin(Path("/private"))
        login.read = AsyncMock(side_effect=asyncio.CancelledError)
        login.close = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await login.complete()
        login.close.assert_awaited_once()
