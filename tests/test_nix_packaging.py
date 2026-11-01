"""Reproduces #283 ("Reveal in file manager" does nothing on NixOS).

On NixOS the daemon only sees the programs its wrapper puts on PATH, so a
package that leaves xdg-utils out has no xdg-open. These tests check both
halves: the Nix package lists xdg-utils, and Vice behaves as expected with and
without xdg-open on PATH. They need neither Nix nor a desktop.
"""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    try:
        import _sandbox  # noqa: F401  keeps every store out of the real home
    except ModuleNotFoundError as exc:
        if exc.name != "_sandbox":
            raise
        from tests import _sandbox  # noqa: F401
    import vice.share as _share
except ModuleNotFoundError:
    _share = None  # Vice's Python dependencies are missing; only the Nix checks run

PACKAGE_NIX = Path(__file__).resolve().parent.parent / "nix" / "package.nix"


class NixPackageListsXdgUtils(unittest.TestCase):
    """The wrapper is built from runtimeDeps, so what is not in that list is
    not on PATH on NixOS."""

    def setUp(self) -> None:
        self.text = PACKAGE_NIX.read_text()

    def test_xdg_utils_is_a_package_argument(self) -> None:
        args = self.text.split("}:", 1)[0]
        self.assertRegex(args, r"\bxdg-utils,")

    def test_xdg_utils_is_in_runtime_deps(self) -> None:
        match = re.search(r"runtimeDeps\s*=\s*\[(.*?)\]", self.text, re.S)
        self.assertIsNotNone(match, "runtimeDeps list not found")
        lines = [ln.split("#", 1)[0].strip() for ln in match.group(1).splitlines()]
        self.assertIn("xdg-utils", lines)

    def test_runtime_deps_are_put_on_the_wrapper_path(self) -> None:
        self.assertRegex(self.text, r"wrapProgram .*--prefix PATH : \$\{lib\.makeBinPath runtimeDeps\}")


@unittest.skipUnless(_share is not None, "aiohttp is not installed")
class XdgOpenOnANixLikePath(unittest.IsolatedAsyncioTestCase):
    """A NixOS service has no /usr/bin, only what the wrapper adds."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.empty_bin = self.root / "empty-bin"
        self.empty_bin.mkdir()
        self.record = self.root / "opened.txt"
        self.wrapper_bin = self.root / "xdg-utils-bin"
        self.wrapper_bin.mkdir()
        fake = self.wrapper_bin / "xdg-open"
        fake.write_text(f'#!/bin/sh\nprintf "%s" "$1" > "{self.record}"\n')
        fake.chmod(0o755)

    async def test_without_xdg_utils_the_failure_from_the_issue_is_logged(self) -> None:
        with mock.patch.dict("os.environ", {"PATH": str(self.empty_bin)}), \
                mock.patch("vice.runtime.user_systemd_env_snapshot", return_value={}), \
                self.assertLogs("vice.share", level="WARNING") as logs:
            await _share._xdg_open(self.root)
        out = "\n".join(logs.output)
        self.assertIn("No such file or directory: 'xdg-open'", out)
        self.assertFalse(self.record.exists())

    async def test_with_xdg_utils_on_the_wrapper_path_the_folder_opens(self) -> None:
        with mock.patch.dict("os.environ", {"PATH": str(self.wrapper_bin)}), \
                mock.patch("vice.runtime.user_systemd_env_snapshot", return_value={}):
            await _share._xdg_open(self.root)
        self.assertEqual(self.record.read_text(), str(self.root))


if __name__ == "__main__":
    unittest.main()
