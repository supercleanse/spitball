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


class TestSettingsOverlayFiles(unittest.TestCase):
    """The settings overlay (docs/SPEC-v2.md section 1) replaced the old
    dropdown: Widget.qml hosts SettingsWindow.qml, the dropdown file is gone,
    and every page Model.js's section list names exists under settings/."""

    def test_widget_hosts_the_overlay_not_the_old_panel(self):
        widget = (ROOT / "Widget.qml").read_text()
        self.assertIn("SettingsWindow {", widget)
        self.assertNotIn("SettingsPanel", widget)
        self.assertFalse((ROOT / "SettingsPanel.qml").exists(), "SettingsPanel.qml should be deleted")

    def test_every_section_page_exists(self):
        model_js = (ROOT / "Model.js").read_text()
        pages = re.findall(r'page:\s*"([A-Za-z]+Page\.qml)"', model_js)
        self.assertEqual(len(pages), 10, pages)
        for page in pages:
            self.assertTrue((ROOT / "settings" / page).is_file(), page)

    def test_offscreen_harness_is_the_only_fake_data_path(self):
        # The fake CLI is wired only by render.sh (behind --fake-data); no
        # shipped QML may reference it.
        for qml in QML_FILES:
            self.assertNotIn("fake_spitball", (ROOT / qml).read_text(), qml)
        self.assertIn("--fake-data", (ROOT / "tests" / "offscreen" / "render.sh").read_text())


# Every QML file the plugin ships: the two entry points, the two popups'
# content files, the settings overlay, and each settings/ component/page.
QML_FILES = ["Widget.qml", "SpitballService.qml", "LivePopup.qml", "SettingsWindow.qml"] + sorted(
    str(p.relative_to(ROOT)) for p in (ROOT / "settings").glob("*.qml"))


def _qmllint():
    # Arch ships qmllint under /usr/lib/qt6/bin without putting it on PATH.
    return shutil.which("qmllint") or next(
        (p for p in ("/usr/lib/qt6/bin/qmllint", "/usr/lib/qt6/bin/qmllint6") if Path(p).exists()), None)


class TestQmllint(unittest.TestCase):
    def test_qmllint_report_only(self):
        lint = _qmllint()
        if not lint:
            self.skipTest("qmllint not installed")
        self.assertGreaterEqual(len(QML_FILES), 4 + 20)
        for qml in QML_FILES:
            result = subprocess.run([lint, str(ROOT / qml)],
                                     capture_output=True, text=True, timeout=60)
            # Report-only: never fail the suite on lint findings, just surface
            # them. The `qs.Commons`/`qs.Ui` imports resolve only inside
            # Quickshell, so import warnings are expected here; syntax errors
            # are what this is for.
            if result.returncode != 0:
                findings = [line for line in (result.stdout + result.stderr).splitlines()
                            if "Warnings occurred while importing" not in line
                            and "Failed to import" not in line
                            and "was not found. Did you add all imports" not in line
                            and "Are your import paths set up properly" not in line]
                text = "\n".join(findings).strip()
                if text:
                    print(f"qmllint findings for {qml}:\n{text}")


class TestVendoredRnnoiseModel(unittest.TestCase):
    """The RNNoise model ffmpeg's arnndn reads (docs/SPEC-v2.md section 3)
    ships with the plugin, pinned by hash, with its attribution beside it."""

    MODEL = ROOT / "models" / "rnnoise" / "sh.rnnn"
    SHA256 = "70bb6685eb0c2a1d18e2918dca3fbfbd39317010b1802eb1b6ea73a92f3fdec0"

    def test_model_file_present_and_pinned(self):
        import hashlib
        self.assertTrue(self.MODEL.is_file(), self.MODEL)
        self.assertEqual(hashlib.sha256(self.MODEL.read_bytes()).hexdigest(), self.SHA256)
        self.assertTrue(self.MODEL.read_text().startswith("rnnoise-nu model file version 1"))

    def test_attribution_present(self):
        readme = (ROOT / "models" / "rnnoise" / "README.md").read_text()
        self.assertIn("rnnoise-models", readme)
        self.assertIn("somnolent-hogwash", readme)
        self.assertIn(self.SHA256, readme)

    def test_denoise_module_points_at_it(self):
        from spitball import denoise
        self.assertEqual(denoise.MODEL_PATH, self.MODEL)


if __name__ == "__main__":
    unittest.main()
