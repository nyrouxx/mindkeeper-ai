import os
import unittest
from pathlib import Path
from unittest.mock import patch

from support import component

c = component("codex")


class CodexTests(unittest.TestCase):
    def test_environment_excludes_tokens_and_api_credentials(self):
        with patch.dict(
            os.environ,
            {
                "SUPERVISOR_TOKEN": "sensitive",
                "OPENAI_API_KEY": "secret",
                "CODEX_EXECUTOR_MCP_URL": "secret",
            },
        ):
            env = c.process_environment(Path("/private"))
        self.assertNotIn("SUPERVISOR_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_EXECUTOR_MCP_URL", env)
        self.assertEqual(env["CODEX_HOME"], "/private/codex-home")

    def test_tools_and_user_configuration_disabled(self):
        command = c.CodexParser("/private").command("schema", "output", "work")
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        for feature in (
            "shell_tool",
            "apps",
            "plugins",
            "hooks",
            "multi_agent",
            "goals",
            "computer_use",
        ):
            index = command.index(feature)
            self.assertEqual(command[index - 1], "--disable")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
