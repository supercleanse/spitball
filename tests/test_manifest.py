"""manifest.json / Widget.qml / SpitballService.qml consistency, and the
`omarchy plugin validate` + `qmllint` external checks (both skipped cleanly
when the tool isn't installed -- this suite must still pass on a machine
without Omarchy's CLI)."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "manifest.json").read_text())


class TestManifestRequiredFields(unittest.TestCase):
    def test_top_level_required_fields_present(self):
        for key in ("schemaVersion", "id", "name", "version", "author", "license",
                    "description", "kinds", "entryPoints"):
            self.assertIn(key, MANIFEST, f"manifest.json missing {key!r}")

    def test_bar_widget_block_required_fields(self):
        bw = MANIFEST.get("barWidget", {})
        for key in ("displayName", "description", "category", "aliases",
                    "allowMultiple", "defaultSection"):
            self.assertIn(key, bw, f"manifest.json barWidget missing {key!r}")

    def test_entry_point_files_exist(self):
        entry = MANIFEST["entryPoints"]
        self.assertIn("service", entry)
        self.assertIn("barWidget", entry)
        self.assertTrue((ROOT / entry["service"]).is_file(), entry["service"])
        self.assertTrue((ROOT / entry["barWidget"]).is_file(), entry["barWidget"])

    def test_kinds_include_service_and_bar_widget(self):
        self.assertIn("service", MANIFEST["kinds"])
        self.assertIn("bar-widget", MANIFEST["kinds"])


class TestIdConsistency(unittest.TestCase):
    def test_id_matches_widget_module_name(self):
        widget = (ROOT / "Widget.qml").read_text()
        m = re.search(r'moduleName:\s*"([^"]+)"', widget)
        self.assertIsNotNone(m, "Widget.qml has no moduleName")
        self.assertEqual(m.group(1), MANIFEST["id"])

    def test_id_matches_ipchandler_target(self):
        service = (ROOT / "SpitballService.qml").read_text()
        m = re.search(r'target:\s*"([^"]+)"', service)
        self.assertIsNotNone(m, "SpitballService.qml has no IpcHandler target")
        self.assertEqual(m.group(1), MANIFEST["id"])


class TestOmarchyPluginValidate(unittest.TestCase):
    def test_omarchy_plugin_validate_passes(self):
        if not shutil.which("omarchy"):
            self.skipTest("omarchy CLI not installed")
        result = subprocess.run(["omarchy", "plugin", "validate", str(ROOT)],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0,
                          f"stdout={result.stdout!r} stderr={result.stderr!r}")


class TestQmllint(unittest.TestCase):
    def test_qmllint_report_only(self):
        if not shutil.which("qmllint"):
            self.skipTest("qmllint not installed")
        for qml in ("Widget.qml", "SpitballService.qml"):
            result = subprocess.run(["qmllint", str(ROOT / qml)],
                                     capture_output=True, text=True, timeout=30)
            # Report-only: never fail the suite on lint findings, just surface them.
            if result.returncode != 0:
                print(f"qmllint findings for {qml}:\n{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
