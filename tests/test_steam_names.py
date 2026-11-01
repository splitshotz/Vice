import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice import active_window
from vice.config import Config, DiscordConfig
from vice.main import ViceDaemon

FLATPAK = ".var/app/com.valvesoftware.Steam/.local/share/Steam"


def _manifest(root: Path, app_id: str, name: str) -> None:
    apps = root / "steamapps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / f"appmanifest_{app_id}.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{app_id}"\n\t"name"\t\t"{name}"\n}}\n'
    )


def _libraries(root: Path, *paths: Path) -> None:
    apps = root / "steamapps"
    apps.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{p}"\n\t}}\n' for i, p in enumerate(paths)
    )
    (apps / "libraryfolders.vdf").write_text(f'"libraryfolders"\n{{\n{body}}}\n')


class _SteamFolderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.home = self.tmp / "home"
        self.steam = self.home / ".local/share/Steam"
        self.steam.mkdir(parents=True)
        patcher = mock.patch(
            "vice.active_window.actual_home_dir", return_value=self.home
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class SteamGameNameTests(_SteamFolderTestCase):
    def test_a_game_in_the_main_library_is_named(self) -> None:
        _manifest(self.steam, "1671210", "DELTARUNE")

        self.assertEqual(active_window.steam_game_name("1671210"), "DELTARUNE")

    def test_a_game_in_a_second_library_is_named(self) -> None:
        other = self.tmp / "games" / "SteamLibrary"
        _libraries(self.steam, self.steam, other)
        _manifest(other, "413150", "Stardew Valley")

        self.assertEqual(
            active_window.steam_game_name("413150"),
            "Stardew Valley",
        )

    def test_a_missing_manifest_gives_none(self) -> None:
        _manifest(self.steam, "1671210", "DELTARUNE")

        self.assertIsNone(active_window.steam_game_name("42"))

    def test_a_manifest_without_a_name_gives_none(self) -> None:
        apps = self.steam / "steamapps"
        apps.mkdir(parents=True)
        (apps / "appmanifest_7.acf").write_text('"AppState"\n{\n\t"appid"\t"7"\n}\n')

        self.assertIsNone(active_window.steam_game_name("7"))

    def test_an_unusable_id_gives_none(self) -> None:
        _manifest(self.steam, "1671210", "DELTARUNE")

        for bad in (None, "", "abc", "../1671210"):
            with self.subTest(app_id=bad):
                self.assertIsNone(active_window.steam_game_name(bad))

    def test_proton_and_runtime_entries_are_skipped(self) -> None:
        for app_id, name in (
            ("1493710", "Proton Experimental"),
            ("1628350", "Steam Linux Runtime 3.0 (sniper)"),
            ("228980", "Steamworks Common Redistributables"),
        ):
            _manifest(self.steam, app_id, name)
            with self.subTest(name=name):
                self.assertIsNone(active_window.steam_game_name(app_id))

    def test_the_flatpak_install_is_read(self) -> None:
        _manifest(self.home / FLATPAK, "1671210", "DELTARUNE")

        self.assertEqual(active_window.steam_game_name("1671210"), "DELTARUNE")

    def test_the_flatpak_library_list_is_read(self) -> None:
        flatpak = self.home / FLATPAK
        other = self.tmp / "drive" / "SteamLibrary"
        _libraries(flatpak, flatpak, other)
        _manifest(other, "1671210", "DELTARUNE")

        self.assertEqual(active_window.steam_game_name("1671210"), "DELTARUNE")

    def test_a_symlinked_root_is_only_checked_once(self) -> None:
        (self.home / ".steam").mkdir()
        (self.home / ".steam/steam").symlink_to(self.steam)

        roots = active_window._steam_roots()

        self.assertEqual(len({r.resolve() for r in roots}), len(roots))


class MatchGameSteamFallbackTests(unittest.TestCase):
    def _daemon(self) -> ViceDaemon:
        daemon = ViceDaemon.__new__(ViceDaemon)
        daemon.cfg = Config(discord=DiscordConfig())
        return daemon

    def test_the_list_still_wins_when_both_match(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value=None):
            with mock.patch("vice.main.steam_game_name", return_value="Other") as steam:
                got = daemon._match_game(
                    {"process": "tf_linux64", "class": "steam_app_440", "pid": 1}
                )

        self.assertEqual(got, "Team Fortress 2")
        steam.assert_not_called()

    def test_an_unlisted_steam_game_is_named_from_its_window_class(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value=None):
            with mock.patch("vice.main.steam_game_name", return_value="Some Game") as steam:
                got = daemon._match_game(
                    {"process": "game.exe", "class": "steam_app_999999999", "pid": 1}
                )

        self.assertEqual(got, "Some Game")
        steam.assert_called_once_with("999999999")

    def test_an_unlisted_steam_game_is_named_from_its_process_app_id(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value="999999999"):
            with mock.patch("vice.main.steam_game_name", return_value="Some Game") as steam:
                got = daemon._match_game({"process": "game", "class": "", "pid": 1})

        self.assertEqual(got, "Some Game")
        steam.assert_called_once_with("999999999")

    def test_a_window_with_no_steam_id_tags_nothing(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value=None):
            with mock.patch("vice.main.steam_game_name") as steam:
                got = daemon._match_game(
                    {"process": "firefox", "class": "firefox", "pid": 1}
                )

        self.assertIsNone(got)
        steam.assert_not_called()

    def test_a_tool_or_unknown_app_tags_nothing(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.read_steam_app_id", return_value=None):
            with mock.patch("vice.main.steam_game_name", return_value=None):
                got = daemon._match_game(
                    {"process": "x", "class": "steam_app_999999999", "pid": 1}
                )

        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
