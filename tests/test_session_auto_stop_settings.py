"""Config and UI wiring for the optional game-exit session stop."""

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


class SessionAutoStopConfigTests(unittest.TestCase):
    def test_default_is_disabled(self) -> None:
        self.assertFalse(Config().recording.session_stop_on_game_exit)

    def test_setting_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    config_mod.save(Config(recording=RecordingConfig(session_stop_on_game_exit=True)))
                    loaded = config_mod.load()

        self.assertTrue(loaded.recording.session_stop_on_game_exit)


class SessionAutoStopUiTests(unittest.TestCase):
    def test_recording_setting_is_drafted_and_saved(self) -> None:
        if not shutil.which("node") or not (REPO_ROOT / "node_modules/vite").exists():
            self.skipTest("Node and installed UI dependencies are required")
        # Build in memory so this exercises the real TypeScript conversion
        # functions without rewriting the shipped UI or requiring a browser.
        script = """
import assert from 'node:assert/strict';
import {build} from 'vite';
const result = await build({configFile: false, logLevel: 'silent', build: {
  write: false, lib: {entry: 'ui-src/lib/settingsDraft.ts', formats: ['es'], fileName: 'settings'},
}});
const code = result[0].output.find(item => item.type === 'chunk').code;
const {draftFromConfig, patchFromDraft} = await import(
  'data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
assert.equal(draftFromConfig({}).sessionStopOnGameExit, false);
for (const enabled of [false, true]) {
  const draft = draftFromConfig({recording: {session_stop_on_game_exit: enabled}});
  assert.equal(draft.sessionStopOnGameExit, enabled);
  assert.equal(patchFromDraft(draft).recording.session_stop_on_game_exit, enabled);
  draft.sessionStopOnGameExit = !enabled;
  assert.equal(patchFromDraft(draft).recording.session_stop_on_game_exit, !enabled);
}
"""
        subprocess.run(["node", "--input-type=module", "-"], input=script,
                       text=True, cwd=REPO_ROOT, check=True, capture_output=True, timeout=30)

    def test_session_start_auto_stop_modes_are_localized(self) -> None:
        types = (REPO_ROOT / "ui-src/lib/types.ts").read_text()
        store = (REPO_ROOT / "ui-src/state/store.tsx").read_text()
        locale = json.loads((REPO_ROOT / "ui-src/locales/en.json").read_text())
        self.assertIn("auto_stop?: 'off' | 'armed' | 'unavailable'", types)
        self.assertIn("msg.auto_stop === 'armed'", store)
        self.assertIn("msg.auto_stop === 'unavailable'", store)
        self.assertEqual(
            locale["events"]["sessionAutoStopUnavailable"],
            "Automatic stopping is unavailable for this session. Stop it manually.",
        )


if __name__ == "__main__":
    unittest.main()
