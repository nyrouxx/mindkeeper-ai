"""ChatGPT device login through documented Codex app-server RPC."""

import asyncio
import json

from .codex import CodexError, process_environment


class DeviceLogin:
    def __init__(self, root):
        self.root = root
        self.process = None

    async def send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    async def read(self):
        while line := await self.process.stdout.readline():
            try:
                return json.loads(line)
            except ValueError:
                continue
        raise CodexError("login_failed")

    async def response(self, uid):
        while True:
            message = await self.read()
            if message.get("id") == uid:
                if "error" in message:
                    raise CodexError("login_failed")
                return message["result"]

    async def start(self):
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(self.root / "bin/codex"),
                "--disable",
                "apps",
                "--disable",
                "plugins",
                "--disable",
                "hooks",
                "app-server",
                "--listen",
                "stdio://",
                env=process_environment(self.root),
                cwd=self.root,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            async with asyncio.timeout(45):
                await self.send(
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {
                                "name": "ha_personal_secretary",
                                "title": "HA Personal Secretary",
                                "version": "0.1.0",
                            },
                        },
                    }
                )
                await self.response(1)
                await self.send({"method": "initialized"})
                await self.send(
                    {
                        "id": 2,
                        "method": "account/login/start",
                        "params": {"type": "chatgptDeviceCode"},
                    }
                )
                challenge = await self.response(2)
                if challenge.get(
                    "verificationUrl"
                ) != "https://auth.openai.com/codex/device" or not challenge.get("userCode"):
                    raise CodexError("login_failed")
                return challenge
        except BaseException:
            await self.close()
            raise

    async def complete(self):
        try:
            async with asyncio.timeout(900):
                while True:
                    message = await self.read()
                    if message.get("method") == "account/login/completed":
                        if not message.get("params", {}).get("success"):
                            raise CodexError("login_failed")
                        return True
        finally:
            await self.close()

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
