"""Static assertions over the web UI.

The UI is a React and TypeScript source tree under ui-src/ that builds to two
committed files, vice/ui/scripts/app.js and vice/ui/styles/app.css. These
tests read the source rather than the bundle wherever they can, because the
bundle is minified and a failure in it says nothing useful.

What is worth asserting here is narrow. Behaviour is covered by driving the
real app; these are the things that are invisible until a user on a particular
machine hits them, plus the guards that have to hold on every file.
"""

import json
import re
import shutil
import subprocess
import unittest
from functools import lru_cache
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_SRC = REPO_ROOT / "ui-src"
UI_DIR = REPO_ROOT / "vice" / "ui"
UI_INDEX = UI_DIR / "index.html"
BUNDLE_JS = UI_DIR / "scripts" / "app.js"
BUNDLE_CSS = UI_DIR / "styles" / "app.css"
README = REPO_ROOT / "README.md"
LOCALES = UI_SRC / "locales"

# Spelled as an escape so this file does not itself trip the sweep guard below.
EM_DASH = "\u2014"


@lru_cache(maxsize=1)
def _git_ignored() -> frozenset:
    """Paths git is ignoring, which by definition never ship.

    Falls back to ignoring nothing outside a checkout, so a source tarball
    scans more rather than less.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(REPO_ROOT / line for line in out.splitlines() if line)


@lru_cache(maxsize=1)
def english_copy() -> dict:
    """en.json, the source of truth for every user-facing string."""
    return json.loads((LOCALES / "en.json").read_text())


def _leaf_strings(node) -> list:
    if isinstance(node, str):
        return [node]
    out = []
    for value in node.values():
        out.extend(_leaf_strings(value))
    return out


def _leaf_paths(node, prefix: str = "") -> list:
    """Every key path in a locale tree. A plural object counts as one leaf."""
    if isinstance(node, str) or "other" in node:
        return [prefix]
    out = []
    for key, value in node.items():
        out.extend(_leaf_paths(value, f"{prefix}.{key}" if prefix else key))
    return out


def read_source(*suffixes: str) -> str:
    """Every hand-written UI source file, concatenated."""
    wanted = suffixes or (".ts", ".tsx", ".css")
    parts = []
    for path in sorted(UI_SRC.rglob("*")):
        if path.is_file() and path.suffix in wanted:
            parts.append(path.read_text())
    return "\n".join(parts)


class UISourcePresenceTests(unittest.TestCase):
    """The source tree exists and is what the bundle is built from."""

    def test_ui_source_tree_is_present(self) -> None:
        self.assertTrue(UI_SRC.is_dir(), "ui-src/ is missing")
        self.assertTrue((UI_SRC / "main.tsx").is_file())
        for screen in ("Home", "Clips", "Settings", "Editor", "About"):
            self.assertTrue(
                (UI_SRC / "screens" / f"{screen}.tsx").is_file(),
                f"{screen} screen is missing",
            )

    def test_built_bundle_is_committed(self) -> None:
        # Packaging installs these two files directly; nothing in the install
        # path runs a bundler, so a missing build breaks every user.
        self.assertTrue(BUNDLE_JS.is_file(), "the built app.js is not committed")
        self.assertTrue(BUNDLE_CSS.is_file(), "the built app.css is not committed")
        self.assertGreater(BUNDLE_JS.stat().st_size, 50_000)
        self.assertGreater(BUNDLE_CSS.stat().st_size, 20_000)

    def test_bundle_carries_the_current_copy(self) -> None:
        """Catches a source edit that was never rebuilt.

        The committed bundle is what ships, so copy that exists only in
        ui-src/ would be invisible to every user.
        """
        bundle = BUNDLE_JS.read_text()
        for phrase in (
            "Double-tap to start or stop a full recording",
            "Discord Rich Presence is on",
            "Everything saved",
            "Nothing at the playhead",
            "Danger zone",
            "Loops the selection",
            "not being reported right now",
        ):
            self.assertIn(phrase, bundle, f"the bundle is stale: {phrase!r} is missing")

    def test_bundle_carries_every_locale(self) -> None:
        """The same staleness check, for languages nobody here reads.

        The English phrases above only catch a forgotten build when English
        changed. A translator who edits their own JSON and does not rebuild
        ships a bundle that disagrees with the source, and no reviewer reading
        the diff would see it.
        """
        bundle = BUNDLE_JS.read_text()
        for path in sorted(LOCALES.glob("*.json")):
            strings = _leaf_strings(json.loads(path.read_text()))
            # Vite inlines the JSON, so a value reaches the bundle verbatim
            # unless it carries a character the emitter has to escape.
            checkable = [
                v for v in strings
                if v and len(v) >= 12 and not set(v) & set("`\\'\"") and "${" not in v
            ]
            self.assertGreater(
                len(checkable), 20, f"{path.name}: too few strings to check"
            )
            missing = [v for v in checkable if v not in bundle]
            self.assertEqual(
                missing,
                [],
                f"the bundle is stale for {path.name}: "
                f"{len(missing)} of {len(checkable)} strings are missing, "
                f"first is {missing[0]!r}" if missing else "",
            )

    def test_index_only_loads_the_two_built_assets(self) -> None:
        index = UI_INDEX.read_text()
        scripts = re.findall(r'<script[^>]*src="([^"]+)"', index)
        styles = re.findall(r'<link[^>]*href="([^"]+)"', index)
        self.assertEqual(scripts, ["/scripts/app.js?v=__VICE_VERSION__"])
        self.assertEqual(styles, ["/styles/app.css?v=__VICE_VERSION__"])

    def test_assets_carry_the_cache_busting_token(self) -> None:
        # share.py rewrites ?v=__VICE_VERSION__ to a per-build fingerprint and
        # then serves the assets immutable for a year. Without the token a
        # user keeps the previous build's UI after an upgrade.
        index = UI_INDEX.read_text()
        self.assertEqual(index.count("?v=__VICE_VERSION__"), 2)


class PlatformWorkaroundTests(unittest.TestCase):
    """Fixes for specific machines, each of which looks like dead code."""

    def test_dark_color_scheme_declared_for_native_dropdowns(self) -> None:
        self.assertIn(
            '<meta name="color-scheme" content="dark">', UI_INDEX.read_text()
        )

    def test_native_select_popups_get_system_colors(self) -> None:
        # Without these a native <select> popup renders white on white on
        # KDE Plasma 6 under Wayland (#85).
        css = read_source(".css")
        self.assertIn("select option", css)
        self.assertIn("MenuText", css)

    def test_saved_but_unlisted_values_survive_a_save(self) -> None:
        # Dropping an unlisted display to Auto wrote display=null on the next
        # save and destroyed a hand-set monitor, which is the only way to
        # reach one gpu-screen-recorder will not enumerate (#160).
        settings = (UI_SRC / "screens" / "Settings.tsx").read_text()
        self.assertEqual(
            settings.count("settings.savedOption"),
            3,
            "all three pickers must keep a saved value",
        )
        copy = english_copy()
        self.assertIn("(saved)", copy["settings"]["savedOption"])
        self.assertIn("not being reported right now", copy["settings"]["displayMissing"])

    def test_h265_clips_ask_for_an_h264_preview_proxy(self) -> None:
        playback = (UI_SRC / "lib" / "playback.ts").read_text()
        self.assertIn("proxy=1", playback)
        self.assertIn("hevc", playback)
        self.assertIn("HEVC_SUPPORTED", playback)

    def test_missing_h264_decoder_is_reported_not_hidden(self) -> None:
        # A WebEngine build without the codec drops the video track in
        # silence, leaving a grey rectangle and no explanation (#79).
        playback = (UI_SRC / "lib" / "playback.ts").read_text()
        self.assertIn("videoWidth === 0", playback)
        self.assertIn("viewer.noH264Decoder", playback)
        self.assertIn("no H.264 decoder", english_copy()["viewer"]["noH264Decoder"])

    def test_every_clip_path_segment_is_url_encoded(self) -> None:
        """Regression test for #138, lost in the 2.8.0 rebuild: a slug is a
        filename, so `#`, `?`, `%` and `+` all change what the URL means."""
        api = (UI_SRC / "lib" / "api.ts").read_text()
        self.assertIn("encodeURIComponent", api)
        # Any interpolation straight into a path is the bug.
        for raw in ("${slug}", "${id}", "${jobId}"):
            self.assertNotIn(
                f"/{raw}", api,
                f"{raw} reaches a URL path without encoding",
            )

    def test_playback_failures_are_logged_with_their_cause(self) -> None:
        """Without this the only record of a failed clip is a reporter's
        screenshot, which cannot tell DECODE from SRC_NOT_SUPPORTED."""
        playback = (UI_SRC / "lib" / "playback.ts").read_text()
        self.assertIn("nativeLog", playback)
        for name in ("DECODE", "SRC_NOT_SUPPORTED", "NO_VIDEO_TRACK"):
            self.assertIn(name, playback)
        env = (UI_SRC / "lib" / "env.ts").read_text()
        self.assertIn("log_debug", env)

    def test_card_previews_use_the_same_source_as_the_viewer(self) -> None:
        """An H.265 library previewed as a black card because the hover preview
        skipped the proxy the viewer asks for."""
        card = (UI_SRC / "components" / "ClipCard.tsx").read_text()
        self.assertIn("playbackUrl(clip)", card)
        self.assertNotIn("video.src = clip.video_url", card)

    def test_native_window_is_detected_before_pywebview_is_injected(self) -> None:
        # window.pywebview only exists after DOMContentLoaded, which is too
        # late to get the quit row right on the first paint.
        env = (UI_SRC / "lib" / "env.ts").read_text()
        self.assertIn("native", env)
        self.assertIn("'1'", env)
        self.assertIn("pywebview", env)

    def test_native_tray_labels_come_from_the_locale_system(self) -> None:
        env = (UI_SRC / "lib" / "env.ts").read_text()
        main = (UI_SRC / "main.tsx").read_text()
        self.assertIn("set_tray_labels", env)
        self.assertIn("tray.openVice", main)
        self.assertIn("tray.quitVice", main)
        tray = english_copy()["tray"]
        self.assertEqual(tray["openVice"], "Open Vice")
        self.assertEqual(tray["quitVice"], "Quit Vice")

    def test_clipboard_falls_back_when_the_async_api_is_unavailable(self) -> None:
        clipboard = (UI_SRC / "lib" / "clipboard.ts").read_text()
        self.assertIn("pywebview", clipboard)
        self.assertIn("execCommand", clipboard)


class EffectsProbeTests(unittest.TestCase):
    """The compositor probe's constants were tuned against real hardware."""

    def setUp(self) -> None:
        self.effects = (UI_SRC / "lib" / "effects.ts").read_text()

    def test_probe_constants_are_intact(self) -> None:
        self.assertIn("SLOW_FRAME_MS = 42", self.effects)
        self.assertIn("PROBE_SAMPLES = 48", self.effects)
        self.assertIn("PROBE_WARMUP = 4", self.effects)

    def test_probe_uses_the_median_not_the_mean(self) -> None:
        self.assertIn("gaps.sort", self.effects)
        self.assertIn("gaps.length >> 1", self.effects)

    def test_probe_measures_with_effects_on(self) -> None:
        # Probing while .perf-low is on measures the cheap UI, concludes the
        # machine is fast and turns everything back on, which is a mode that
        # flips on every launch.
        self.assertIn("classList.remove('perf-low')", self.effects)

    def test_all_three_modes_are_offered(self) -> None:
        self.assertIn("'auto', 'full', 'reduced'", self.effects)


class UICopyTests(unittest.TestCase):
    """What the interface says, read from the locale file it now lives in.

    These used to read the TSX source. Every user-facing string moved into
    ui-src/locales/en.json for translation, so that is where the assertions
    belong: the source only carries keys now.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = read_source(".tsx", ".ts")
        cls.readme = README.read_text()
        cls.copy = english_copy()
        cls.strings = "\n".join(_leaf_strings(cls.copy))

    def test_tutorial_reflects_current_workflows(self) -> None:
        tutorial = self.copy["tutorial"]
        self.assertIn("Double-tap to start or stop a full recording", tutorial["sessionHelp"])
        self.assertIn("highlight", tutorial["sessionHelp"])
        self.assertIn("trim the best moment", tutorial["reviewHelp"])
        self.assertIn("Vice keeps recording", tutorial["backgroundHelp"])
        self.assertIn("Discord Rich Presence is on", tutorial["backgroundHelp"])

    def test_discord_copy_does_not_say_off_by_default(self) -> None:
        # DiscordConfig.enabled defaults to True.
        copy = self.strings + "\n" + self.source + "\n" + self.readme
        self.assertIn("On by default", copy)
        self.assertNotIn("off by default", copy.lower())

    def test_durations_in_copy_come_from_the_config(self) -> None:
        # The lede used to hard-code 20 seconds and was wrong for anyone who
        # had changed it.
        home = (UI_SRC / "screens" / "Home.tsx").read_text()
        self.assertIn("clip_duration", home)
        self.assertIn("{formatDuration(clipDuration, true)}", home)

    def test_local_only_share_links_say_so(self) -> None:
        # A LAN address looks identical to a real share link right up until a
        # friend cannot open it (#105).
        share = (UI_SRC / "lib" / "share.ts").read_text()
        self.assertIn("card.linkCopiedLocal", share)
        card = self.copy["card"]
        self.assertIn("local only", card["linkCopiedLocal"])
        self.assertIn("cloudflared", card["linkCopiedLocalDetail"])

    def test_a_clip_with_no_detected_game_says_so(self) -> None:
        # The tag line used to render nothing, so the card quietly changed
        # height depending on whether detection had found anything.
        card = (UI_SRC / "components" / "ClipCard.tsx").read_text()
        self.assertIn("common.untagged", card)
        self.assertEqual(self.copy["common"]["untagged"], "Untagged")
        self.assertIn("data-untagged", card)

    def test_unreadable_clips_are_marked_and_left_alone(self) -> None:
        card = (UI_SRC / "components" / "ClipCard.tsx").read_text()
        self.assertIn("card.unreadable", card)
        self.assertEqual(self.copy["card"]["unreadable"], "Unreadable")
        self.assertIn("still on disk", self.copy["card"]["unreadableNote"])


class SettingsCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = (UI_SRC / "screens" / "Settings.tsx").read_text()
        cls.draft = (UI_SRC / "lib" / "settingsDraft.ts").read_text()

    def test_every_section_is_present(self) -> None:
        for section in (
            "recording",
            "audio",
            "hotkeys",
            "storage",
            "sharing",
            "discord",
            "appearance",
            "advanced",
        ):
            self.assertIn(f"'{section}'", self.settings, f"the {section} section is missing")

    def test_every_config_key_the_daemon_reads_is_written(self) -> None:
        for key in (
            "buffer_duration",
            "clip_duration",
            "fps",
            "display",
            "follow_mouse_display",
            "resolution",
            "container",
            "encoder",
            "color_depth",
            "backend",
            "capture_audio",
            "gsr_replay_storage",
            "capture_microphone",
            "microphone_source",
            "microphone_mono",
            "desktop_volume",
            "microphone_volume",
            "wf_microphone_strategy",
            "gsr_audio_source",
            "audio_tracks",
            "audio_tracks_mix_first",
            "gsr_args",
            "clip_presets",
            "disable_while_focused",
            "tag_clips_with_game",
            "auto_playlist_by_game",
            "clip_name_template",
            "image_directory",
            "cloudflare_tunnel",
            "check_on_start",
            "sound_volume",
            "hardware_video_decode",
            "client_id_override",
            "custom_games",
        ):
            self.assertIn(key, self.draft, f"{key} is never written back")

    def test_every_custom_sound_is_offered(self) -> None:
        for key in (
            "clip_sound",
            "clip_failed_sound",
            "session_start_sound",
            "session_end_sound",
            "highlight_sound",
            "screenshot_sound",
        ):
            self.assertIn(key, self.draft)

    def test_the_screenshot_key_can_be_set_and_cleared(self) -> None:
        self.assertIn("screenshotKey", self.draft)
        self.assertIn("screenshot: draft.screenshotKey", self.draft)
        # Unset is a legitimate value for this one, so there has to be a way
        # back to it. Every other key capture in Settings is always bound.
        self.assertIn("settings.clearKey", self.settings)

    def test_encoder_dropdown_offers_av1(self) -> None:
        self.assertIn("av1_nvenc", self.settings)
        self.assertIn("av1_vaapi", self.settings)

    def test_duration_sliders_reach_thirty_minutes(self) -> None:
        self.assertEqual(self.settings.count("max={1800}"), 2)

    def test_resolution_allows_a_custom_value(self) -> None:
        self.assertIn("'custom'", self.settings)
        self.assertIn(r"^\d{2,5}x\d{2,5}$", self.draft)

    def test_buffer_is_raised_to_cover_the_longest_clip_key(self) -> None:
        self.assertIn("requiredBuffer", self.draft)
        self.assertIn("clipPresets.map", self.draft)

    def test_clip_name_preview_mirrors_the_daemon(self) -> None:
        self.assertIn("$n", self.draft)
        self.assertIn("$date", self.draft)
        self.assertIn("$time", self.draft)
        self.assertIn("$game", self.draft)

    def test_audio_track_conflict_is_explained(self) -> None:
        # With desktop audio off the recorder keeps only microphone sources,
        # so a game track vanishes with nothing saying why (#137).
        tracks = (UI_SRC / "components" / "settings" / "AudioTracks.tsx").read_text()
        self.assertIn("tracksLostWithoutDesktopAudio", tracks)
        self.assertIn("audioTracks.droppedWarning", tracks)
        self.assertIn(
            "will not be recorded",
            english_copy()["audioTracks"]["droppedWarning"]["other"],
        )


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = (UI_SRC / "engine" / "editor.ts").read_text()
        cls.screen = (UI_SRC / "screens" / "Editor.tsx").read_text()

    def test_playback_pool_keeps_three_elements_per_track(self) -> None:
        # With two, the outgoing clip and the preloaded one fought over the
        # same element and reassigned its src every frame.
        self.assertIn("make(), make(), make()", self.engine)

    def test_clock_follows_the_master_video(self) -> None:
        # Chasing wall time forced a corrective seek every few frames.
        self.assertIn("master", self.engine)
        self.assertIn("m.cur.currentTime", self.engine)

    def test_warm_start_constants_are_intact(self) -> None:
        self.assertIn("ED_WARM_MS = 350", self.engine)
        self.assertIn("ED_PRELOAD = 2.0", self.engine)
        self.assertIn("ED_DRIFT = 0.15", self.engine)

    def test_all_six_transitions_are_offered(self) -> None:
        constants = (UI_SRC / "engine" / "editorConstants.ts").read_text()
        for fx in ("crossfade", "fadeblack", "fadewhite", "dipaccent", "blurdis", "slide"):
            self.assertIn(f"id: '{fx}'", constants)

    def test_transition_preview_matches_the_export_filters(self) -> None:
        self.assertIn("xfade hblur", self.engine)
        self.assertIn("xfade slideleft", self.engine)

    def test_editor_text_tool_keeps_all_three_fonts(self) -> None:
        constants = (UI_SRC / "engine" / "editorConstants.ts").read_text()
        for font in ("Geist", "Inter", "JetBrains Mono"):
            self.assertIn(font, constants)

    def test_export_offers_every_location(self) -> None:
        for location in ("library", "videos", "custom"):
            self.assertIn(f"'{location}'", self.screen)

    def test_leaving_the_editor_releases_the_decoders(self) -> None:
        self.assertIn("releasePool", self.engine)
        self.assertIn("removeAttribute('src')", self.engine)


class OfflineTests(unittest.TestCase):
    """The daemon is expected to work with no network at all."""

    def test_fonts_are_local(self) -> None:
        css = (UI_SRC / "styles" / "base.css").read_text()
        self.assertEqual(css.count("@font-face"), 4)
        for font in ("Figtree", "Geist", "Inter", "JetBrainsMono"):
            self.assertIn(f"/fonts/{font}.woff2", css)
        for font in ("Figtree", "Geist", "Inter", "JetBrainsMono"):
            self.assertTrue(
                (UI_DIR / "fonts" / f"{font}.woff2").is_file(),
                f"{font}.woff2 is not shipped",
            )

    def test_the_bundle_fetches_nothing_from_the_internet(self) -> None:
        css = BUNDLE_CSS.read_text()
        self.assertNotIn("@import url(http", css)
        self.assertNotIn("fonts.googleapis", css)
        self.assertNotIn("fonts.gstatic", css)
        self.assertNotIn("cdn.jsdelivr", css)
        js = BUNDLE_JS.read_text()
        for host in ("fonts.googleapis", "cdn.jsdelivr", "unpkg.com", "cdnjs."):
            self.assertNotIn(host, js, f"the bundle reaches out to {host}")


class DesignTokenTests(unittest.TestCase):
    """Every token referenced must exist, or the declaration silently dies."""

    # Set outside the theme provider, with a fallback: by a component's inline
    # style, or by the pre-paint bootstrap in index.html.
    LOCAL = {"chip", "filled", "boot-accent"}

    def test_no_css_variable_is_referenced_that_does_not_exist(self) -> None:
        bundle = BUNDLE_CSS.read_text()
        used = set()
        for path in UI_SRC.rglob("*"):
            if path.suffix not in {".css", ".ts", ".tsx"} or not path.is_file():
                continue
            used.update(re.findall(r"var\(--([a-z0-9-]+)", path.read_text()))

        missing = sorted(
            name
            for name in used
            if name not in self.LOCAL
            and not name.startswith(("vice-", "ed-"))
            and f"--{name}:" not in bundle
        )
        # A declaration using an undefined variable is dropped entirely, so a
        # typo removes a border or a shadow with no error anywhere.
        self.assertEqual(missing, [], f"undefined design tokens: {missing}")

    def test_locally_defined_variables_carry_a_fallback(self) -> None:
        for name in self.LOCAL:
            for path in UI_SRC.rglob("*.css"):
                for hit in re.findall(rf"var\(--{name}[^)]*\)", path.read_text()):
                    self.assertIn(",", hit, f"{hit} in {path.name} needs a fallback")


class BootThemeTests(unittest.TestCase):
    """The boot cover paints before the bundle, so it carries its own copy."""

    def test_inline_colours_match_the_generated_accents(self) -> None:
        index = UI_INDEX.read_text()
        accents = (UI_SRC / "theme" / "accents.ts").read_text()

        generated_bg = dict(re.findall(r"(\w+): \{[^}]*?bg: '(#\w{6})'", accents, re.S))
        generated_base = dict(re.findall(r"(\w+): \{\s*base: '(#\w{6})'", accents))
        self.assertEqual(len(generated_bg), 5, "expected five accents")

        for name, value in generated_bg.items():
            self.assertIn(
                f"{name}:'{value}'",
                index,
                f"index.html has a stale background for {name}; rerun npm run accents",
            )
        for name, value in generated_base.items():
            self.assertIn(f"{name}:'{value}'", index, f"index.html has a stale accent for {name}")

    def test_the_boot_cover_can_set_the_wordmark_before_the_bundle(self) -> None:
        # The cover paints before the bundle parses. A linked font would flash a
        # fallback on the one screen whose job is hiding load time, so the face
        # is inlined in the document head as a data URI.
        index = UI_INDEX.read_text()
        self.assertIn("@font-face", index, "the wordmark face must be declared in index.html")
        self.assertIn("data:font/woff2;base64,", index, "the face must be inlined, not linked")
        self.assertIn("Syne VICE", index)
        self.assertIn('class="boot-word wordmark"', index)

        css = read_source(".css")
        self.assertIn("'Syne VICE'", css, "the .wordmark class must use the inlined family")

    def test_the_wordmark_is_used_in_three_places_only(self) -> None:
        # It is a logo, not a heading style. Body and heading text stay Figtree.
        users = [
            p.name
            for p in UI_SRC.rglob("*.tsx")
            if "<Wordmark" in p.read_text() and p.name != "Wordmark.tsx"
        ]
        self.assertEqual(sorted(users), ["About.tsx", "SideNav.tsx"])

    def test_the_cover_shows_that_something_is_happening(self) -> None:
        index = UI_INDEX.read_text()
        self.assertIn("boot-orbit", index)
        self.assertIn("boot-bar", index)
        css = read_source(".css")
        self.assertIn("@keyframes boot-spin", css)
        self.assertIn("@keyframes boot-slide", css)

    def test_the_cover_is_reachable_without_javascript_running(self) -> None:
        # It is markup in index.html on purpose: a cover the bundle has to
        # render cannot cover the time before the bundle parses.
        index = UI_INDEX.read_text()
        boot = index[index.index('<div id="boot">'):]
        # The wordmark is set in uppercase, so match the name case-insensitively
        # rather than pinning how it happens to be cased.
        self.assertIn("vice", boot.lower())


class TypographyTests(unittest.TestCase):
    def test_nothing_is_set_below_eleven_pixels(self) -> None:
        offenders = []
        for path in sorted(UI_SRC.rglob("*.css")):
            for line_no, line in enumerate(path.read_text().splitlines(), 1):
                for size in re.findall(r"font-size:\s*([0-9.]+)px", line):
                    if float(size) < 11:
                        offenders.append(f"{path.name}:{line_no} ({size}px)")
        self.assertEqual(offenders, [], f"type below 11px: {offenders}")


class WebSocketCoverageTests(unittest.TestCase):
    """Every message the daemon broadcasts has to land somewhere."""

    def test_every_broadcast_type_is_handled(self) -> None:
        store = (UI_SRC / "state" / "store.tsx").read_text()
        editor = (UI_SRC / "screens" / "Editor.tsx").read_text()
        handled = store + "\n" + editor
        for message in (
            "clip_saved",
            "clip_deleted",
            "playlists_changed",
            "clip_saving",
            "clip_error",
            "status",
            "tunnel_url",
            "tunnel_error",
            "share_links_changed",
            "session_start",
            "session_stop",
            "session_highlight",
            "export_progress",
            "export_done",
            "export_error",
            "editor_project_changed",
            "update_available",
            "image_saved",
            "image_deleted",
            "image_error",
            "image_copy_failed",
            "daemon_upgrading",
        ):
            self.assertIn(message, handled, f"{message} is unhandled")


class NoEmDashesAnywhereTests(unittest.TestCase):
    """No em-dash may reach a shipped file. Andrew's rule, and a tell."""

    SKIP_DIRS = {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
    }
    SHIPPED_SUFFIXES = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".mts",
        ".mjs",
        ".css",
        ".html",
        ".md",
        ".sh",
        ".toml",
        ".json",
        ".service",
        ".desktop",
        ".install",
        ".rules",
    }
    # The two build outputs. Minified dependency strings are outside anyone's
    # control here, and the sources they come from are scanned instead.
    GENERATED = {BUNDLE_JS, BUNDLE_CSS}

    def _shipped_files(self):
        ignored = _git_ignored()
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in self.SKIP_DIRS for part in path.parts):
                continue
            if path in self.GENERATED or path in ignored:
                continue
            if path.suffix not in self.SHIPPED_SUFFIXES:
                continue
            yield path

    def test_no_shipped_file_contains_an_em_dash(self) -> None:
        offenders = []
        for path in self._shipped_files():
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if EM_DASH in text:
                line = next(
                    (i for i, ln in enumerate(text.splitlines(), 1) if EM_DASH in ln),
                    0,
                )
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}")
        self.assertEqual(offenders, [], f"em-dash found in: {', '.join(offenders)}")

    def test_the_guard_actually_looks_at_the_source(self) -> None:
        # A guard that silently stops scanning is worse than none.
        scanned = list(self._shipped_files())
        self.assertGreater(len(scanned), 60)
        names = {p.name for p in scanned}
        self.assertIn("main.tsx", names)
        self.assertIn("Settings.tsx", names)
        self.assertIn("editor.ts", names)
        self.assertIn("share.py", names)
        self.assertIn("README.md", names)


class TranslationTests(unittest.TestCase):
    """Guards on the locale files.

    The failure mode worth catching is not a bad translation, it is English
    moving on and the other languages drifting behind it in silence.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.english = english_copy()
        cls.english_keys = set(_leaf_paths(cls.english))

    def _locales(self):
        for path in sorted(LOCALES.glob("*.json")):
            if path.stem != "en":
                yield path.stem, json.loads(path.read_text())

    def test_english_is_the_complete_source(self) -> None:
        self.assertGreater(len(self.english_keys), 300)
        self.assertTrue((LOCALES / "index.ts").is_file())

    def test_every_locale_file_is_imported_in_index(self) -> None:
        index_ts = (LOCALES / "index.ts").read_text()
        for path in LOCALES.glob("*.json"):
            self.assertIn(
                f"{path.stem}.json",
                index_ts,
                f"{path.name} exists in ui-src/locales/ but is not imported in index.ts",
            )

    def test_no_locale_carries_a_key_english_has_dropped(self) -> None:
        # An extra key means English was renamed and this file was not, so the
        # string is dead weight and whatever replaced it is untranslated.
        for name, tree in self._locales():
            extra = sorted(set(_leaf_paths(tree)) - self.english_keys)
            self.assertEqual(extra, [], f"{name}.json has keys en.json does not: {extra[:5]}")

    def test_a_missing_key_is_allowed_and_falls_back(self) -> None:
        # The property that makes a half-finished translation shippable, so it
        # is worth pinning: nothing in i18n.ts may treat a miss as an error.
        i18n = (UI_SRC / "lib" / "i18n.ts").read_text()
        self.assertIn("LOCALES[FALLBACK]", i18n)
        self.assertIn("return key", i18n)

    def test_plural_keys_carry_the_forms_english_needs(self) -> None:
        # A plural written as a flat string is right in English and wrong in
        # every language with more than two forms.
        def walk(node, prefix=""):
            if not isinstance(node, dict):
                return
            if "other" in node:
                self.assertIn("one", node, f"{prefix} has no singular form")
                self.assertTrue(
                    all(
                        form in ("zero", "one", "two", "few", "many", "other")
                        for form in node
                    ),
                    f"{prefix} has a form that is not a CLDR plural category: {sorted(node)}",
                )
                return
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else key)

        walk(self.english)

    def test_the_ui_has_a_language_picker(self) -> None:
        settings = (UI_SRC / "screens" / "Settings.tsx").read_text()
        self.assertIn("availableLocales", settings)
        self.assertIn("setLocale", settings)
        # Before the first render, or a screen paints English and then swaps.
        self.assertIn("initLocale()", (UI_SRC / "main.tsx").read_text())

    def test_translating_is_documented(self) -> None:
        doc = REPO_ROOT / "docs" / "TRANSLATING.md"
        self.assertTrue(doc.is_file(), "docs/TRANSLATING.md is missing")
        text = doc.read_text()
        self.assertIn("i18n:new", text)
        self.assertIn("i18n:check", text)
        self.assertIn("TRANSLATING.md", README.read_text())


if __name__ == "__main__":
    unittest.main()


class CustomAccentTests(unittest.TestCase):
    """A custom accent has to be the same design system as the five presets,
    not a colour pasted over them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.derive = (UI_SRC / "theme" / "deriveAccent.ts").read_text()
        cls.theme = (UI_SRC / "theme" / "viceTheme.ts").read_text()
        cls.index = UI_INDEX.read_text()

    def test_the_runtime_derivation_uses_the_generators_settings(self) -> None:
        # Every one of these is a trap the generator's comments call out by
        # name, and a custom accent built without them is a different scheme.
        for setting in (
            "Variant.EXPRESSIVE",
            "specVersion: '2021'",
            "contrastLevel: 0",
            "isDark: true",
        ):
            self.assertIn(setting, self.derive, f"{setting} is missing from the runtime scheme")

        # Expressive derives primary from sourceHue + 240, so seeding it
        # without an explicit primary palette turns a blue pick green.
        self.assertIn("primaryPalette: TonalPalette.fromHueAndChroma", self.derive)
        self.assertNotIn("new SchemeExpressive", self.derive)

    def test_the_chromas_match_the_generator(self) -> None:
        generator = (REPO_ROOT / "tools" / "derive-accents.mjs").read_text()
        for source in (generator, self.derive):
            self.assertIn(
                "primary: 40, secondary: 24, tertiary: 32, neutral: 8, neutralVariant: 12",
                source,
            )
        for shift in ("NEUTRAL_HUE_SHIFT = 15", "TERTIARY_HUE_SHIFT = 60"):
            self.assertIn(shift, generator)
            self.assertIn(shift, self.derive)

    def test_the_default_primary_tone_matches_the_generator(self) -> None:
        generator = (REPO_ROOT / "tools" / "derive-accents.mjs").read_text()
        self.assertIn("DEFAULT_PRIMARY_TONE = 80", generator)
        self.assertIn("DEFAULT_PRIMARY_TONE = 80", self.derive)

    def test_the_same_contrast_bar_is_enforced_at_runtime(self) -> None:
        # The generator refuses to write a scheme below AA. The runtime cannot
        # refuse silently, so it lifts the accent and reports what is left.
        self.assertIn("4.5", self.derive)
        self.assertIn("is pure black", self.derive)
        self.assertIn("rampFailures", self.derive)

    def test_the_boot_cover_can_paint_a_custom_accent(self) -> None:
        # The cover paints before the bundle parses and cannot derive anything,
        # so the two colours it needs are cached when the user confirms.
        self.assertIn("vice-custom-bg", self.index)
        self.assertIn("vice-custom-base", self.index)
        choice = (UI_SRC / "lib" / "accentChoice.ts").read_text()
        self.assertIn("vice-custom-bg", choice)
        self.assertIn("vice-custom-base", choice)
        # And it still falls back when they are missing or corrupt.
        self.assertIn("if (!bg)", self.index)

    def test_a_custom_accent_without_a_seed_falls_back(self) -> None:
        # A cleared localStorage leaves 'custom' selected with nothing behind
        # it, and a window painted in no accent at all is not an option.
        self.assertIn("accent === 'custom' ? 'blue' : accent", self.theme)

    def test_the_custom_theme_is_cached_by_seed(self) -> None:
        # <Theme> compares by identity, so rebuilding per render would swap the
        # theme object every frame and repaint the whole tree.
        self.assertIn("cachedCustom", self.theme)


class AccentParityTests(unittest.TestCase):
    """The runtime derivation and the build-time generator must agree.

    Both keep producing plausible colours when they drift, so nothing else
    would catch it. This runs the five shipped seeds through the runtime code
    and diffs every role against what the generator actually wrote.

    Skipped without node_modules, which is how CI runs: the Python suite is the
    only thing CI installs, and the check belongs to whoever changes the theme.
    """

    def test_runtime_derivation_matches_the_generator(self) -> None:
        if not (REPO_ROOT / "node_modules" / "vite").exists():
            self.skipTest("node_modules is not installed")
        if not shutil.which("npx"):
            self.skipTest("npx is not on PATH")
        result = subprocess.run(
            ["node", "tools/verify-custom-accent.mjs"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(
            result.returncode, 0,
            "deriveAccent.ts has drifted from tools/derive-accents.mjs:\n"
            + result.stdout + result.stderr,
        )
