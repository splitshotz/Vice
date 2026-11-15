"""Coverage for the window-id/geometry lookups active_window.py added for
window_capture: _active_window_id_x11, _window_geometry_by_id_x11, the three
_active_window_geometry_* adapters, and the public get_focused_window_id /
get_window_geometry / get_active_window_geometry wrappers. The pre-existing
focused-window detection adapters (_get_active_window_x11/hyprland/sway) are
exercised elsewhere (test_hotkey.py, test_discord_rpc.py) and are out of
scope here.
"""

import json
import unittest
from unittest import mock

from vice import active_window


class WindowIdX11Tests(unittest.TestCase):
    def test_returns_the_active_window_id(self) -> None:
        with mock.patch("vice.active_window._run", return_value="0x03a00003\n"):
            self.assertEqual(active_window._active_window_id_x11(), "0x03a00003")

    def test_empty_output_is_none(self) -> None:
        with mock.patch("vice.active_window._run", return_value=""):
            self.assertIsNone(active_window._active_window_id_x11())


class ActiveWindowIdentityX11Tests(unittest.TestCase):
    """The X11 adapter reports the id of the very window it read process and
    class from, so a caller pinning capture to a match can't drift onto
    whatever got focused a moment later."""

    def test_result_carries_the_window_it_described(self) -> None:
        outs = ["0x03a00003\n", "1234\n", 'WM_CLASS(STRING) = "testgame", "TestGame"\n']
        with mock.patch("vice.active_window._run", side_effect=outs), \
             mock.patch("vice.active_window._read_proc_comm", return_value="testgame"):
            win = active_window._get_active_window_x11()

        self.assertEqual(win, {
            "process": "testgame",
            "class": "TestGame",
            "pid": 1234,
            "window_id": "0x03a00003",
        })


class ActiveWindowIdentityKDETests(unittest.TestCase):
    def test_pid_and_class_are_read_from_one_stable_active_window_id(self) -> None:
        commands = []

        def run(command):
            commands.append(command)
            return {
                ("kdotool", "getactivewindow"): "42\n",
                ("kdotool", "getwindowpid", "42"): "1234\n",
                ("kdotool", "getwindowclassname", "42"): "TestGame\n",
            }.get(tuple(command), "")

        with mock.patch("vice.active_window._run", side_effect=run), \
             mock.patch("vice.active_window._read_proc_comm", return_value="testgame"):
            win = active_window._get_active_window_kde()

        self.assertEqual(win, {"process": "testgame", "class": "TestGame", "pid": 1234})
        self.assertEqual(commands, [
            ["kdotool", "getactivewindow"],
            ["kdotool", "getwindowpid", "42"],
            ["kdotool", "getwindowclassname", "42"],
        ])


class WindowGeometryByIdX11Tests(unittest.TestCase):
    def test_parses_width_and_height(self) -> None:
        out = "WIN=94\nX=10\nY=20\nWIDTH=1920\nHEIGHT=1080\nSCREEN=0\n"
        with mock.patch("vice.active_window._run", return_value=out):
            self.assertEqual(
                active_window._window_geometry_by_id_x11("0x1"), (1920, 1080)
            )

    def test_missing_dimensions_is_none(self) -> None:
        with mock.patch("vice.active_window._run", return_value="WIN=94\n"):
            self.assertIsNone(active_window._window_geometry_by_id_x11("0x1"))

    def test_zero_dimensions_is_none(self) -> None:
        out = "WIDTH=0\nHEIGHT=0\n"
        with mock.patch("vice.active_window._run", return_value=out):
            self.assertIsNone(active_window._window_geometry_by_id_x11("0x1"))


class GeometryAdapterTests(unittest.TestCase):
    def test_hyprland_reads_the_size_array(self) -> None:
        payload = '{"size": [1280, 720]}'
        with mock.patch("vice.active_window._run", return_value=payload):
            self.assertEqual(
                active_window._active_window_geometry_hyprland(), (1280, 720)
            )

    def test_hyprland_zero_size_is_none(self) -> None:
        payload = '{"size": [0, 0]}'
        with mock.patch("vice.active_window._run", return_value=payload):
            self.assertIsNone(active_window._active_window_geometry_hyprland())

    def test_hyprland_bad_json_is_none(self) -> None:
        with mock.patch("vice.active_window._run", return_value="not json"):
            self.assertIsNone(active_window._active_window_geometry_hyprland())

    def test_sway_reads_the_focused_leaf_rect(self) -> None:
        tree = {
            "focused": False,
            "nodes": [{"focused": True, "nodes": [], "floating_nodes": [],
                       "rect": {"width": 800, "height": 600}}],
        }
        with mock.patch("vice.active_window._run", return_value=json.dumps(tree)):
            self.assertEqual(active_window._active_window_geometry_sway(), (800, 600))

    def test_sway_no_focused_leaf_is_none(self) -> None:
        tree = '{"focused": false, "nodes": []}'
        with mock.patch("vice.active_window._run", return_value=tree):
            self.assertIsNone(active_window._active_window_geometry_sway())

    def test_x11_delegates_to_active_window_id_and_geometry_lookup(self) -> None:
        with mock.patch("vice.active_window._run", side_effect=["0x1\n", "WIDTH=640\nHEIGHT=480\n"]):
            self.assertEqual(active_window._active_window_geometry_x11(), (640, 480))

    def test_x11_no_active_window_is_none(self) -> None:
        with mock.patch("vice.active_window._run", return_value=""):
            self.assertIsNone(active_window._active_window_geometry_x11())


class GetFocusedWindowIdTests(unittest.TestCase):
    def test_x11_adapter_returns_the_id(self) -> None:
        with mock.patch("vice.active_window._ADAPTER", active_window._get_active_window_x11), \
             mock.patch("vice.active_window._active_window_id_x11", return_value="0x2"):
            self.assertEqual(active_window.get_focused_window_id(), "0x2")

    def test_non_x11_adapter_returns_none(self) -> None:
        with mock.patch("vice.active_window._ADAPTER", active_window._get_active_window_hyprland):
            self.assertIsNone(active_window.get_focused_window_id())

    def test_no_adapter_returns_none(self) -> None:
        with mock.patch("vice.active_window._ADAPTER", None):
            self.assertIsNone(active_window.get_focused_window_id())

    def test_raising_adapter_returns_none(self) -> None:
        with mock.patch("vice.active_window._ADAPTER", active_window._get_active_window_x11), \
             mock.patch("vice.active_window._active_window_id_x11", side_effect=OSError):
            self.assertIsNone(active_window.get_focused_window_id())


class GetWindowGeometryTests(unittest.TestCase):
    def test_x11_adapter_returns_geometry_for_id(self) -> None:
        with mock.patch("vice.active_window._ADAPTER", active_window._get_active_window_x11), \
             mock.patch("vice.active_window._window_geometry_by_id_x11", return_value=(1920, 1080)):
            self.assertEqual(active_window.get_window_geometry("0x1"), (1920, 1080))

    def test_non_x11_adapter_returns_none(self) -> None:
        with mock.patch("vice.active_window._ADAPTER", active_window._get_active_window_sway):
            self.assertIsNone(active_window.get_window_geometry("0x1"))

    def test_empty_window_id_returns_none(self) -> None:
        with mock.patch("vice.active_window._ADAPTER", active_window._get_active_window_x11):
            self.assertIsNone(active_window.get_window_geometry(""))

    def test_raising_lookup_returns_none(self) -> None:
        with mock.patch("vice.active_window._ADAPTER", active_window._get_active_window_x11), \
             mock.patch("vice.active_window._window_geometry_by_id_x11", side_effect=OSError):
            self.assertIsNone(active_window.get_window_geometry("0x1"))


class GetActiveWindowGeometryTests(unittest.TestCase):
    """_GEOMETRY_ADAPTERS binds real function objects at module load, so the
    dispatch itself is tested by overriding the mapping (mock.patch on the
    function names would not reach the frozen dict values)."""

    def test_dispatches_to_the_matching_adapter(self) -> None:
        fake = mock.Mock(return_value=(2560, 1440))
        with mock.patch("vice.active_window._ADAPTER", active_window._get_active_window_hyprland), \
             mock.patch.dict(active_window._GEOMETRY_ADAPTERS, {active_window._get_active_window_hyprland: fake}):
            self.assertEqual(active_window.get_active_window_geometry(), (2560, 1440))

    def test_unmapped_adapter_returns_none(self) -> None:
        with mock.patch("vice.active_window._ADAPTER", None):
            self.assertIsNone(active_window.get_active_window_geometry())

    def test_raising_adapter_returns_none(self) -> None:
        fake = mock.Mock(side_effect=OSError)
        with mock.patch("vice.active_window._ADAPTER", active_window._get_active_window_x11), \
             mock.patch.dict(active_window._GEOMETRY_ADAPTERS, {active_window._get_active_window_x11: fake}):
            self.assertIsNone(active_window.get_active_window_geometry())


if __name__ == "__main__":
    unittest.main()
