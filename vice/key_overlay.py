"""The on-screen keyboard overlay, run as its own process by the daemon.

The daemon already reads the keyboard for hotkeys, so it feeds this process
over stdin, one JSON object per line:

    {"t": "key", "k": "W", "d": true}
    {"t": "style", "accent": "#8ab9f3"}
    {"t": "layout", "name": "azerty"}
    {"t": "place", "position": "bottom-left", "x": 0, "y": 0, "scale": 1.0}
    {"t": "move", "on": true}

and hears back on stdout:

    {"t": "ready"}
    {"t": "moved", "x": 120, "y": 640}
    {"t": "error", "reason": "no-layer-shell"}

It is an ordinary window that gpu-screen-recorder captures like anything
else on screen, which is what makes it useful for streams (#209). Wayland
needs the layer-shell protocol to keep a window above a fullscreen game:
KDE, Hyprland, Sway and Niri have it, GNOME does not.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from importlib.resources import files as _pkg_files

from .keys import DEFAULT_KEY_LAYOUT, LAYOUT, layout_rows, normalize_key_layout

PRESETS = ("bottom-left", "bottom-right", "top-left", "top-right", "bottom-center")
EDGE = 32
UNIT = 44
GAP = 6
PAD = 12
# A press takes about 90 ms to land and 160 ms to let go, close to the spring
# the web UI's tiles use.
PRESS_MS = 90.0
RELEASE_MS = 160.0


def _accents() -> dict:
    try:
        return json.loads((_pkg_files("vice") / "data" / "accents.json").read_text())
    except (OSError, ValueError):
        return {}


def ramp_for(accent: str) -> dict:
    """The generated scheme whose primary is *accent*, blue otherwise."""
    schemes = _accents()
    for scheme in schemes.values():
        if scheme.get("base", "").lower() == (accent or "").lower():
            return scheme
    return schemes.get("blue", {})


def _rgb(hex_colour: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    value = (hex_colour or "#000000").lstrip("#")
    try:
        r, g, b = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        r = g = b = 0.0
    return r, g, b, alpha


def panel_size(scale: float) -> tuple[int, int]:
    unit, gap, pad = UNIT * scale, GAP * scale, PAD * scale
    width = 5.5 * unit + 4 * gap + 2 * pad
    height = len(LAYOUT) * unit + (len(LAYOUT) - 1) * gap + 2 * pad
    return math.ceil(width), math.ceil(height)


def _rounded(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def key_rects(scale: float, layout: str = DEFAULT_KEY_LAYOUT):
    """(key, label, x, y, w, h) for every key on the panel, labelled for
    keyboard *layout*."""
    unit, gap, pad = UNIT * scale, GAP * scale, PAD * scale
    y = pad
    for row in layout_rows(layout):
        x = pad
        for key, label, span in row:
            w = span * unit + (span - 1) * gap if span >= 1 else span * unit
            if key is not None:
                yield key, label, x, y, w, unit
            x += w + gap
        y += unit + gap


def _draw_cap(cr, scheme: dict, scale: float, label: str,
              x: float, y: float, w: float, h: float, t: float) -> None:
    """One key, *t* of the way into its press: rounder at rest, squarer and
    a little smaller when held, filled with the primary."""
    import cairo

    idle_fill = _rgb(scheme.get("secondaryContainer", "#324862"))
    idle_text = _rgb(scheme.get("onSecondaryContainer", "#d2e4ff"))
    held_fill = _rgb(scheme.get("base", "#8ab9f3"))
    held_text = _rgb(scheme.get("onBase", "#003259"))
    shrink = 0.06 * t
    kx, ky = x + w * shrink / 2, y + h * shrink / 2
    kw, kh = w * (1 - shrink), h * (1 - shrink)
    _rounded(cr, kx, ky, kw, kh, h * (0.32 - 0.16 * t))
    cr.set_source_rgba(*_mix(idle_fill, held_fill, t))
    cr.fill()
    if label:
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size((13 if len(label) > 1 else 16) * scale)
        ext = cr.text_extents(label)
        cr.set_source_rgba(*_mix(idle_text, held_text, t))
        cr.move_to(kx + (kw - ext.width) / 2 - ext.x_bearing,
                   ky + (kh - ext.height) / 2 - ext.y_bearing)
        cr.show_text(label)


def draw(cr, scheme: dict, scale: float, pressed: dict[str, float], moving: bool = False,
         layout: str = DEFAULT_KEY_LAYOUT) -> None:
    """Paint the panel. *pressed* maps a key to how far into its press it is,
    0 to 1. *layout* picks the labels, QWERTY or AZERTY.

    M3 Expressive: the panel is a surface container, idle keys the tonal
    secondary container, a held key the primary, and the shape answers the
    press as well as the colour.
    """
    import cairo

    width, height = panel_size(scale)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)

    _rounded(cr, 0, 0, width, height, 28 * scale)
    cr.set_source_rgba(*_rgb(scheme.get("surfaceLow", "#191b23"), 0.88))
    cr.fill_preserve()
    if moving:
        cr.set_line_width(2 * scale)
        cr.set_source_rgba(*_rgb(scheme.get("base", "#8ab9f3")))
        cr.stroke()
    else:
        cr.new_path()
    for key, label, x, y, w, h in key_rects(scale, layout):
        _draw_cap(cr, scheme, scale, label, x, y, w, h, pressed.get(key, 0.0))


def render_burn_assets(out_dir, accent: str, scale: float, layout: str = DEFAULT_KEY_LAYOUT) -> dict:
    """PNGs for burning the panel into an export: the idle panel, and each
    key held, cut to its own box so ffmpeg can lay it over the panel.

    Returns {"panel": path, "size": (w, h), "keys": {key: (path, x, y)}}.
    Drawn by the same code as the live overlay, so an export matches it.
    """
    import cairo
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    scheme = ramp_for(accent)
    width, height = panel_size(scale)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    draw(cairo.Context(surface), scheme, scale, {}, layout=layout)
    panel = out / "panel.png"
    surface.write_to_png(str(panel))
    keys = {}
    for key, label, x, y, w, h in key_rects(scale, layout):
        x0, y0 = math.floor(x), math.floor(y)
        cap = cairo.ImageSurface(cairo.FORMAT_ARGB32, math.ceil(x + w) - x0, math.ceil(y + h) - y0)
        cr = cairo.Context(cap)
        # The panel shows through the corners a held key gives up.
        cr.set_source_surface(surface, -x0, -y0)
        cr.paint()
        _draw_cap(cr, scheme, scale, label, x - x0, y - y0, w, h, 1.0)
        path = out / f"key-{key.lower()}.png"
        cap.write_to_png(str(path))
        keys[key] = (path, x0, y0)
    return {"panel": panel, "size": (width, height), "keys": keys}


class Overlay:
    def __init__(self) -> None:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, GLib, Gtk

        self.Gtk, self.Gdk, self.GLib = Gtk, Gdk, GLib
        self.scheme = ramp_for("")
        self.layout = DEFAULT_KEY_LAYOUT
        self.scale = 1.0
        self.position = "bottom-left"
        self.custom = (EDGE, EDGE)
        self.moving = False
        self.drag_from: tuple[float, float] | None = None
        self.down: dict[str, bool] = {}
        self.progress: dict[str, float] = {}
        self.last_tick = time.monotonic()
        self.animating = False

        self.wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        self.layer = None
        if self.wayland:
            try:
                gi.require_version("GtkLayerShell", "0.1")
                from gi.repository import GtkLayerShell
            except (ImportError, ValueError):
                GtkLayerShell = None
            if GtkLayerShell is None or not GtkLayerShell.is_supported():
                self.emit({"t": "error", "reason": "no-layer-shell"})
                raise SystemExit(3)
            self.layer = GtkLayerShell

        kind = Gtk.WindowType.TOPLEVEL if self.wayland else Gtk.WindowType.POPUP
        self.win = win = Gtk.Window(type=kind)
        win.set_title("Vice keys")
        win.set_app_paintable(True)
        win.set_decorated(False)
        win.set_accept_focus(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        visual = win.get_screen().get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)
        if self.layer:
            L = self.layer
            L.init_for_window(win)
            L.set_namespace(win, "vice-keys")
            L.set_layer(win, L.Layer.OVERLAY)
            L.set_keyboard_mode(win, L.KeyboardMode.NONE)
            L.set_exclusive_zone(win, -1)
        else:
            win.set_keep_above(True)

        area = Gtk.DrawingArea()
        area.connect("draw", self._on_draw)
        win.add(area)
        win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                       | Gdk.EventMask.POINTER_MOTION_MASK)
        win.connect("button-press-event", self._on_press)
        win.connect("button-release-event", self._on_release)
        win.connect("motion-notify-event", self._on_motion)
        win.connect("realize", lambda *_: self._apply_input_region())
        self.area = area
        self._resize()
        self._place()
        win.show_all()

        self._pending = b""
        os.set_blocking(sys.stdin.fileno(), False)
        channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
        GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP,
                          self._on_stdin)
        self.emit({"t": "ready"})

    # ── protocol ──────────────────────────────────────────────────────────

    @staticmethod
    def emit(message: dict) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def _on_stdin(self, _channel, _condition) -> bool:
        # Raw reads: a buffered readline pulls several lines off the pipe at
        # once and the watch does not fire again for the ones it kept, so
        # every message landed one message late.
        try:
            chunk = os.read(sys.stdin.fileno(), 65536)
        except BlockingIOError:
            return True
        if not chunk:
            self.Gtk.main_quit()
            return False
        self._pending += chunk
        *lines, self._pending = self._pending.split(b"\n")
        for line in lines:
            try:
                self._handle(json.loads(line))
            except ValueError:
                continue
            except Exception:
                # Returning anything but True removes the watch, and the
                # overlay would then never hear another key or its own exit.
                traceback.print_exc()
        return True

    def _handle(self, message: dict) -> None:
        kind = message.get("t")
        if kind == "key":
            self.down[str(message.get("k"))] = bool(message.get("d"))
            self._animate()
        elif kind == "style":
            self.scheme = ramp_for(str(message.get("accent", "")))
            self.area.queue_draw()
        elif kind == "layout":
            self.layout = normalize_key_layout(message.get("name"))
            self.area.queue_draw()
        elif kind == "place":
            self.scale = max(0.5, min(float(message.get("scale", 1.0) or 1.0), 3.0))
            position = str(message.get("position", "bottom-left"))
            self.position = position if position in PRESETS or position == "custom" else "bottom-left"
            self.custom = (int(message.get("x", EDGE) or 0), int(message.get("y", EDGE) or 0))
            self._resize()
            self._place()
        elif kind == "move":
            self.moving = bool(message.get("on"))
            self._apply_input_region()
            self.area.queue_draw()

    # ── geometry ──────────────────────────────────────────────────────────

    def _resize(self) -> None:
        width, height = panel_size(self.scale)
        self.area.set_size_request(width, height)
        self.win.resize(width, height)

    def _monitor_geometry(self):
        display = self.Gdk.Display.get_default()
        window = self.win.get_window()
        monitor = (display.get_monitor_at_window(window) if window is not None else None) \
            or display.get_primary_monitor() or display.get_monitor(0)
        return monitor.get_geometry()

    def _preset_xy(self) -> tuple[int, int]:
        """Top-left of the panel for the current preset, on its monitor."""
        geo = self._monitor_geometry()
        width, height = panel_size(self.scale)
        if self.position == "custom":
            return self.custom
        x = EDGE if "left" in self.position else geo.width - width - EDGE
        if self.position == "bottom-center":
            x = (geo.width - width) // 2
        y = EDGE if self.position.startswith("top") else geo.height - height - EDGE
        return x, y

    def _place(self) -> None:
        if self.layer:
            L = self.layer
            edges = {L.Edge.TOP: False, L.Edge.BOTTOM: False, L.Edge.LEFT: False, L.Edge.RIGHT: False}
            if self.position == "custom":
                edges[L.Edge.TOP] = edges[L.Edge.LEFT] = True
                L.set_margin(self.win, L.Edge.LEFT, self.custom[0])
                L.set_margin(self.win, L.Edge.TOP, self.custom[1])
            else:
                vertical = L.Edge.TOP if self.position.startswith("top") else L.Edge.BOTTOM
                edges[vertical] = True
                L.set_margin(self.win, vertical, EDGE)
                if self.position != "bottom-center":
                    side = L.Edge.LEFT if "left" in self.position else L.Edge.RIGHT
                    edges[side] = True
                    L.set_margin(self.win, side, EDGE)
            for edge, on in edges.items():
                L.set_anchor(self.win, edge, on)
            return
        geo = self._monitor_geometry()
        x, y = self._preset_xy()
        self.win.move(geo.x + x, geo.y + y)

    # ── input ─────────────────────────────────────────────────────────────

    def _apply_input_region(self) -> None:
        """Click-through except while being moved: the overlay sits over a
        game and must never take a click meant for it."""
        import cairo
        window = self.win.get_window()
        if window is None:
            return
        if self.moving:
            width, height = panel_size(self.scale)
            region = cairo.Region(cairo.RectangleInt(0, 0, width, height))
        else:
            region = cairo.Region()
        window.input_shape_combine_region(region, 0, 0)

    def _on_press(self, _widget, event) -> bool:
        if not self.moving or event.button != 1:
            return False
        if self.position != "custom":
            self.custom = self._preset_xy()
            self.position = "custom"
            self._place()
        self.drag_from = (event.x, event.y) if self.layer else (event.x_root, event.y_root)
        return True

    def _on_motion(self, _widget, event) -> bool:
        if not self.moving or self.drag_from is None:
            return False
        geo = self._monitor_geometry()
        width, height = panel_size(self.scale)
        if self.layer:
            # Surface coordinates: after each margin change the surface sits
            # under the pointer again, so the offset from the grab point is
            # this motion's distance.
            dx, dy = event.x - self.drag_from[0], event.y - self.drag_from[1]
        else:
            dx, dy = event.x_root - self.drag_from[0], event.y_root - self.drag_from[1]
            self.drag_from = (event.x_root, event.y_root)
        x = int(max(0, min(self.custom[0] + dx, geo.width - width)))
        y = int(max(0, min(self.custom[1] + dy, geo.height - height)))
        if (x, y) != self.custom:
            self.custom = (x, y)
            self._place()
        return True

    def _on_release(self, _widget, event) -> bool:
        if not self.moving or self.drag_from is None or event.button != 1:
            return False
        self.drag_from = None
        self.emit({"t": "moved", "x": self.custom[0], "y": self.custom[1]})
        return True

    # ── drawing ───────────────────────────────────────────────────────────

    def _animate(self) -> None:
        if not self.animating:
            self.animating = True
            self.last_tick = time.monotonic()
            self.GLib.timeout_add(16, self._tick)

    def _tick(self) -> bool:
        now = time.monotonic()
        elapsed = (now - self.last_tick) * 1000
        self.last_tick = now
        busy = False
        for key in set(self.down) | set(self.progress):
            target = 1.0 if self.down.get(key) else 0.0
            current = self.progress.get(key, 0.0)
            step = elapsed / (PRESS_MS if target > current else RELEASE_MS)
            nxt = min(target, current + step) if target > current else max(target, current - step)
            self.progress[key] = nxt
            busy = busy or nxt != target
        self.area.queue_draw()
        if not busy:
            self.animating = False
        return busy

    def _on_draw(self, _widget, cr) -> bool:
        eased = {k: 1 - (1 - v) ** 3 for k, v in self.progress.items() if v > 0}
        draw(cr, self.scheme, self.scale, eased, self.moving, self.layout)
        return True


def main() -> int:
    overlay = Overlay()
    overlay.Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
