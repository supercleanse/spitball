"""bin/spitball must work as a direct script, under `python3 -I` (isolated
mode -- no PYTHONPATH, no user site-packages), and through a symlink (the
real install is `~/.local/bin/spitball -> .../bin/spitball`)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "spitball"


class TestBinSpitballHelp(unittest.TestCase):
    def _run(self, args):
        return subprocess.run(args, capture_output=True, text=True, timeout=15, cwd=str(ROOT))

    def test_direct_invocation(self):
        result = self._run([sys.executable, str(BIN), "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: spitball", result.stdout)

    def test_isolated_mode(self):
        result = self._run([sys.executable, "-I", str(BIN), "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: spitball", result.stdout)

    def test_via_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "spitball"
            os.symlink(BIN, link)
            result = self._run([sys.executable, "-I", str(link), "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: spitball", result.stdout)

    def test_no_args_also_prints_usage(self):
        result = self._run([sys.executable, str(BIN)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: spitball", result.stdout)


if __name__ == "__main__":
    unittest.main()
