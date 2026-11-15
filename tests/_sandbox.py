"""Point every store Vice writes under the user's home at a scratch folder.

Imported first by each test module that reaches the stores. Building a
ShareServer backfills playlists against its own clips folder and saves the
result, so a test that skipped a redirect pruned the real playlists.json
(and could touch real highlights, views and caches). Redirecting at import
covers every test, and test_sandbox.py fails when a new store is added
without a line here.
"""

import atexit
import shutil
import tempfile
from pathlib import Path

from vice import editor, keys, playlists, updates, session_auto_start

try:
    from vice import share
except ModuleNotFoundError:  # aiohttp missing: the share tests skip themselves
    share = None

ROOT = Path(tempfile.mkdtemp(prefix="vice-tests-"))
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)

REDIRECTS = {
    (session_auto_start, "STATE_PATH"): ROOT / "session_auto_start.json",
    (playlists, "PLAYLISTS_PATH"): ROOT / "playlists.json",
    (editor, "PROJECT_PATH"): ROOT / "editor_project.json",
    (keys, "KEYS_DIR"): ROOT / "keys",
    (updates, "CACHE_PATH"): ROOT / "update.json",
}
SHARE_REDIRECTS = {
    "THUMB_DIR": ROOT / "thumbs",
    "MATUGEN_PATH": ROOT / "matugen.json",
    "PROXY_DIR": ROOT / "proxies",
    "EXPORT_WORK_DIR": ROOT / "exports",
    "HIGHLIGHTS_DIR": ROOT / "highlights",
    "VIEWS_PATH": ROOT / "views.json",
    "CLIP_GAMES_PATH": ROOT / "clip_games.json",
    "SHARE_TOKENS_PATH": ROOT / "share_tokens.json",
    "APP_STATE_PATH": ROOT / "ui_state.json",
}

if share is not None:
    REDIRECTS.update({(share, name): path for name, path in SHARE_REDIRECTS.items()})

for (module, name), path in REDIRECTS.items():
    setattr(module, name, path)
