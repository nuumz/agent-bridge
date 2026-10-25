"""What a peer inherits: the caller's environment minus its session identity; a resumed Claude session is forked."""
from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from bridgelib.adapters import ADAPTERS  # noqa: E402
from bridgelib.core import BRIDGE_CMDS, HOP_VAR, PRIVATE_ENV_VARS, peer_env  # noqa: E402


class PeerEnv(unittest.TestCase):
    def test_session_identity_is_dropped_credentials_and_hop_kept(self):
        ours = {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_BASE_URL": "https://proxy", "GH_TOKEN": "t",
                "CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_CODE_MESSAGING_TOKEN": "m", "CLAUDECODE": "1",
                "CLAUDE_SESSION_INGRESS_TOKEN_FILE": "/f"}
        with mock.patch.dict(os.environ, ours, clear=True):
            env = peer_env({"AGENT_BRIDGE_IDENTITY": "cursor#1"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_BASE_URL": "https://proxy", "GH_TOKEN": "t",
                               HOP_VAR: "1", "AGENT_BRIDGE_IDENTITY": "cursor#1"})

    def test_the_a2a_token_reaches_only_the_peer_that_keeps_it(self):
        ours = {"PATH": "/bin", "AGENT_BRIDGE_A2A_URL": "http://127.0.0.1:1", "AGENT_BRIDGE_A2A_TOKEN": "s3cret"}
        self.assertIn("AGENT_BRIDGE_A2A_TOKEN", PRIVATE_ENV_VARS)
        with mock.patch.dict(os.environ, ours, clear=True):
            plain = peer_env({"AGENT_BRIDGE_IDENTITY": "cursor#1"})
            kept = peer_env({"AGENT_BRIDGE_IDENTITY": "a2a#1"}, keep=ADAPTERS["a2a"].env_keep)
        self.assertNotIn("AGENT_BRIDGE_A2A_TOKEN", plain, "a prompt-injected local peer could print it")
        self.assertEqual(plain["AGENT_BRIDGE_A2A_URL"], "http://127.0.0.1:1")
        self.assertEqual(kept["AGENT_BRIDGE_A2A_TOKEN"], "s3cret")
        for name, agent in ADAPTERS.items():
            self.assertEqual(bool(agent.env_keep), name == "a2a", f"{name} has no business with the token")
        self.assertIn("bridge-a2a", BRIDGE_CMDS, "a second hop over the network is a bridge command too")


class ClaudeResume(unittest.TestCase):
    def opts(self, **kw) -> argparse.Namespace:
        base = dict(mode="ask", board=False, deny=[], model=None, add_dir=[], worktree=None, session=None, cont=False)
        return argparse.Namespace(**{**base, **kw})

    def test_resume_and_continue_fork_the_session(self):
        claude = ADAPTERS["claude"]
        fresh = claude.argv(self.opts(), "q")
        self.assertNotIn("--fork-session", fresh)
        resumed = claude.argv(self.opts(session="abc"), "q")
        self.assertEqual(resumed[resumed.index("--resume"):][:3], ["--resume", "abc", "--fork-session"])
        latest = claude.argv(self.opts(cont=True), "q")
        self.assertEqual(latest[latest.index("--continue"):][:2], ["--continue", "--fork-session"])
        recover = claude.resume_argv(self.opts(), "abc", "q")
        self.assertIn("--fork-session", recover)


if __name__ == "__main__":
    unittest.main()
