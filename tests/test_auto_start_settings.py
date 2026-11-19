"""Config and UI wiring for optional game-detected session start."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vice import config as config_mod
from vice.config import Config, RecordingConfig


REPO_ROOT = Path(__file__).resolve().parents[1]


class AutoStartConfigTests(unittest.TestCase):
    def test_default_is_disabled_and_setting_persists(self) -> None:
        self.assertFalse(Config().recording.session_start_on_game_detect)
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    config_mod.save(
                        Config(recording=RecordingConfig(session_start_on_game_detect=True))
                    )
                    loaded = config_mod.load()
        self.assertTrue(loaded.recording.session_start_on_game_detect)


class AutoStartUiTests(unittest.TestCase):
    def test_setting_round_trip_and_recording_toggle_are_wired(self) -> None:
        if not shutil.which("node") or not (REPO_ROOT / "node_modules/vite").exists():
            self.skipTest("Node and installed UI dependencies are required")
        script = """
import assert from 'node:assert/strict';
import {build} from 'vite';
const result = await build({configFile: false, logLevel: 'silent', build: {
  write: false, lib: {entry: 'ui-src/lib/settingsDraft.ts', formats: ['es'], fileName: 'settings'},
}});
const code = result[0].output.find(item => item.type === 'chunk').code;
const {draftFromConfig, patchFromDraft} = await import(
  'data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
assert.equal(draftFromConfig({}).sessionStartOnGameDetect, false);
for (const enabled of [false, true]) {
  for (const stopping of [false, true]) {
    const draft = draftFromConfig({recording: {
      session_start_on_game_detect: enabled, session_stop_on_game_exit: stopping,
    }});
    assert.equal(draft.sessionStartOnGameDetect, enabled);
    assert.equal(patchFromDraft(draft).recording.session_start_on_game_detect, enabled);
    assert.equal(patchFromDraft(draft).recording.session_stop_on_game_exit, stopping);
    draft.sessionStartOnGameDetect = !enabled;
    assert.equal(patchFromDraft(draft).recording.session_start_on_game_detect, !enabled);
  }
}
"""
        subprocess.run(["node", "--input-type=module", "-"], input=script,
                       text=True, cwd=REPO_ROOT, check=True, capture_output=True, timeout=30)

    def test_status_notes_and_session_start_feedback_have_localized_copy(self) -> None:
        types = (REPO_ROOT / "ui-src/lib/types.ts").read_text()
        store = (REPO_ROOT / "ui-src/state/store.tsx").read_text()
        settings = (REPO_ROOT / "ui-src/screens/Settings.tsx").read_text()
        locale = json.loads((REPO_ROOT / "ui-src/locales/en.json").read_text())
        self.assertIn("'unsupported' | 'detection' | 'state' | null", types)
        self.assertIn("origin?: 'manual' | 'automatic'", types)
        self.assertIn("{type: 'session_auto_start_failed'}", types)
        self.assertIn("session_auto_start_failed", store)
        self.assertIn("sessionAutoStartError", settings)
        self.assertEqual(
            locale["settings"]["sessionAutoStartErrorUnsupported"],
            "Automatic recording is unavailable here.",
        )
        self.assertIn("sessionAutoStartErrorDetection", locale["settings"])
        self.assertIn("sessionAutoStartErrorState", locale["settings"])
        for key in ("sessionAutoStarted", "sessionAutoStartFailed", "sessionAutoStartFailedDetail"):
            self.assertIn(key, locale["events"])


if __name__ == "__main__":
    unittest.main()
