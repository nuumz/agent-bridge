"""Every command in bin/ is executable and answers --help with exit 0 under a temp HOME (CONTRIBUTING.md checklist;
LF endings are .gitattributes' job)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
COMMANDS = sorted(p for p in BIN.iterdir() if p.is_file() and not p.suffix and not p.name.startswith("."))


class CliContract(unittest.TestCase):
    def test_every_command_is_executable_and_answers_help(self):
        self.assertTrue(COMMANDS)
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home}
            for cmd in COMMANDS:
                with self.subTest(cmd=cmd.name):
                    self.assertTrue(os.access(cmd, os.X_OK), f"{cmd.name} lost its executable bit")
                    r = subprocess.run([str(cmd), "--help"], env=env, capture_output=True, timeout=30)
                    self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace")[-400:])


if __name__ == "__main__":
    unittest.main()
