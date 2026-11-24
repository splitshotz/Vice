import unittest
from pathlib import Path

try:
    import _sandbox
except ModuleNotFoundError:
    from tests import _sandbox

from vice import editor, keys, playlists, updates, uploads
from vice.runtime import actual_home_dir


class SandboxTests(unittest.TestCase):
    def test_no_store_points_at_the_real_home(self):
        """Every module-level path a test can write through must be
        redirected. A new store needs a line in _sandbox.REDIRECTS."""
        home = actual_home_dir()
        modules = {module for module, _ in _sandbox.REDIRECTS} | {editor, keys, playlists, updates, uploads}
        for module in modules:
            for name, value in vars(module).items():
                if not isinstance(value, Path) or not name.isupper():
                    continue
                # The Nix build sandbox puts the temp folder inside its home.
                if value.is_relative_to(_sandbox.ROOT):
                    continue
                self.assertFalse(
                    value.is_relative_to(home),
                    f"{module.__name__}.{name} still points at {value}",
                )

    def test_redirects_live_in_the_scratch_folder(self):
        for (module, name), path in _sandbox.REDIRECTS.items():
            self.assertEqual(getattr(module, name), path)
            self.assertTrue(path.is_relative_to(_sandbox.ROOT))
