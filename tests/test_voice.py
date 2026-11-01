import asyncio
import json
import unittest
from unittest import mock

from vice.config import Config, VoiceConfig, normalize_voice
from vice.voice import (DEFAULT_PHRASES, VoiceListener, active_phrases,
                        build_grammar, capture_audio_cmd, match_trigger_phrase,
                        normalize_phrase)


class VoicePhraseTests(unittest.TestCase):
    def test_normalize_phrase(self) -> None:
        self.assertEqual(normalize_phrase("Vice, Clip That!"), "vice clip that")
        self.assertEqual(normalize_phrase("  Record,   clip   that!  "), "record clip that")
        self.assertEqual(normalize_phrase("Clip That"), "clip that")
        self.assertEqual(normalize_phrase("...?!"), "")
        self.assertEqual(normalize_phrase(""), "")

    def test_active_phrases_defaults(self) -> None:
        phrases = active_phrases()
        self.assertEqual(phrases, list(DEFAULT_PHRASES))
        self.assertIn("vice clip that", phrases)
        self.assertIn("clip that", phrases)

    def test_active_phrases_with_custom(self) -> None:
        phrases = active_phrases("Record that!")
        self.assertEqual(phrases, ["vice clip that", "clip that", "record that"])

    def test_active_phrases_deduplicates_default(self) -> None:
        phrases = active_phrases("Clip That")
        self.assertEqual(phrases, ["vice clip that", "clip that"])

    def test_build_grammar(self) -> None:
        phrases = ["vice clip that", "clip that", "record that"]
        grammar_json = build_grammar(phrases)
        parsed = json.loads(grammar_json)
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed, ["vice clip that", "clip that", "record that", "[unk]"])

    def test_build_grammar_from_string(self) -> None:
        grammar_json = build_grammar("Record that!")
        parsed = json.loads(grammar_json)
        self.assertEqual(parsed, ["vice clip that", "clip that", "record that", "[unk]"])

    def test_match_trigger_phrase_exact_and_fillers(self) -> None:
        phrases = ["vice clip that", "clip that", "record that"]
        self.assertEqual(match_trigger_phrase("clip that", phrases=phrases), "clip that")
        self.assertEqual(match_trigger_phrase("vice clip that", phrases=phrases), "vice clip that")
        self.assertEqual(match_trigger_phrase("hey vice clip that", phrases=phrases), "vice clip that")
        self.assertEqual(match_trigger_phrase("yo clip that", phrases=phrases), "clip that")
        self.assertEqual(match_trigger_phrase("clip that please", phrases=phrases), "clip that")
        self.assertEqual(match_trigger_phrase("can you clip that", phrases=phrases), "clip that")
        self.assertEqual(match_trigger_phrase("record that", phrases=phrases), "record that")

    def test_match_trigger_phrase_rejects_near_misses_and_conversations(self) -> None:
        phrases = ["vice clip that", "clip that", "record that"]
        # Near-misses / wrong words
        self.assertIsNone(match_trigger_phrase("click that", phrases=phrases))
        self.assertIsNone(match_trigger_phrase("talking about a clip", phrases=phrases))
        self.assertIsNone(match_trigger_phrase("did you see that clip", phrases=phrases))
        self.assertIsNone(match_trigger_phrase("clip", phrases=phrases))
        # Embedded conversational sentences
        self.assertIsNone(match_trigger_phrase("i was talking about a clip that i saw yesterday", phrases=phrases))
        self.assertIsNone(match_trigger_phrase("did you see that clip that was funny", phrases=phrases))

    def test_match_trigger_phrase_confidence_filtering(self) -> None:
        phrases = ["clip that"]
        low_conf = [{"word": "clip", "conf": 0.2}, {"word": "that", "conf": 0.3}]
        high_conf = [{"word": "clip", "conf": 0.75}, {"word": "that", "conf": 0.9}]
        self.assertIsNone(match_trigger_phrase("clip that", words_list=low_conf, phrases=phrases))
        self.assertEqual(match_trigger_phrase("clip that", words_list=high_conf, phrases=phrases), "clip that")


class VoiceConfigTests(unittest.TestCase):
    def test_default_voice_config(self) -> None:
        cfg = VoiceConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.custom_phrase, "")
        self.assertEqual(cfg.cooldown, 4.0)
        self.assertEqual(cfg.min_confidence, 0.50)

    def test_normalize_voice_clamping(self) -> None:
        raw_cfg = Config(voice=VoiceConfig(enabled=True, custom_phrase=" Record that! ", cooldown=-5.0, min_confidence=1.5))
        normalize_voice(raw_cfg)
        self.assertTrue(raw_cfg.voice.enabled)
        self.assertEqual(raw_cfg.voice.custom_phrase, "Record that!")
        self.assertEqual(raw_cfg.voice.cooldown, 1.0)
        self.assertEqual(raw_cfg.voice.min_confidence, 0.90)

    def test_normalize_voice_invalid_cooldown(self) -> None:
        raw_cfg = Config(voice=VoiceConfig(enabled=True, custom_phrase="Test", cooldown="invalid", min_confidence="bad"))  # type: ignore
        normalize_voice(raw_cfg)
        self.assertEqual(raw_cfg.voice.cooldown, 4.0)
        self.assertEqual(raw_cfg.voice.min_confidence, 0.50)


class VoiceListenerTests(unittest.TestCase):
    def test_capture_audio_cmd_selection(self) -> None:
        with mock.patch("shutil.which", side_effect=lambda cmd: "/usr/bin/" + cmd if cmd == "parec" else None):
            cmd = capture_audio_cmd()
            self.assertIsNotNone(cmd)
            self.assertIn("parec", cmd[0])

        with mock.patch("shutil.which", side_effect=lambda cmd: "/usr/bin/" + cmd if cmd == "ffmpeg" else None):
            cmd = capture_audio_cmd()
            self.assertIsNotNone(cmd)
            self.assertIn("ffmpeg", cmd[0])

        with mock.patch("shutil.which", side_effect=lambda cmd: "/usr/bin/" + cmd if cmd == "arecord" else None):
            cmd = capture_audio_cmd()
            self.assertIsNotNone(cmd)
            self.assertIn("arecord", cmd[0])

        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(capture_audio_cmd(), [])

    def test_state_reporting(self) -> None:
        listener = VoiceListener(enabled=True, custom_phrase="record that")
        state = listener.state()
        self.assertTrue(state["enabled"])
        self.assertFalse(state["active"])
        self.assertIsNone(state["error"])
        self.assertIn("record that", state["phrases"])

    def test_missing_vosk_reports_error(self) -> None:
        listener = VoiceListener(enabled=True)
        with mock.patch("vice.voice.is_vosk_available", return_value=False):
            asyncio.run(listener.start())
            self.assertIsNotNone(listener.error)
            self.assertIn("vosk", listener.error.lower())

    def test_trigger_callback_and_cooldown(self) -> None:
        triggered = []
        listener = VoiceListener(
            enabled=True,
            custom_phrase="record that",
            cooldown=2.0,
            on_trigger=lambda: triggered.append("clip"),
        )

        class DummyRecognizer:
            def __init__(self, outputs):
                self._outputs = list(outputs)

            def AcceptWaveform(self, chunk):
                return True

            def Result(self):
                if self._outputs:
                    return json.dumps({"text": self._outputs.pop(0)})
                return json.dumps({"text": ""})

            def Reset(self):
                pass

            def SetWords(self, enable):
                pass

        class DummyProcess:
            def __init__(self, num_chunks):
                self.stdout = mock.MagicMock()
                self._chunks = [b"\x00" * 3200] * num_chunks + [b""]
                self.stdout.read = lambda n: self._chunks.pop(0) if self._chunks else b""

            def poll(self):
                return 0 if not self._chunks else None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                pass

        dummy_proc = DummyProcess(num_chunks=3)
        dummy_rec = DummyRecognizer(["vice clip that", "vice clip that", "record that"])

        with mock.patch("subprocess.Popen", return_value=dummy_proc):
            with mock.patch.object(listener, "_resolve_model", return_value=object()):
                with mock.patch("vosk.KaldiRecognizer", return_value=dummy_rec):
                    with mock.patch("vice.voice.capture_audio_cmd", return_value=["parec"]):
                        listener._listen_loop()

        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0], "clip")

    def test_low_confidence_does_not_trigger(self) -> None:
        triggered = []
        listener = VoiceListener(
            enabled=True,
            on_trigger=lambda: triggered.append("clip"),
        )

        class LowConfRecognizer:
            def AcceptWaveform(self, chunk):
                return True

            def Result(self):
                return json.dumps({
                    "text": "clip that",
                    "result": [{"word": "clip", "conf": 0.3}, {"word": "that", "conf": 0.2}]
                })

            def Reset(self):
                pass

            def SetWords(self, enable):
                pass

        class DummyProcess:
            def __init__(self):
                self.stdout = mock.MagicMock()
                self._chunks = [b"\x00" * 3200, b""]
                self.stdout.read = lambda n: self._chunks.pop(0) if self._chunks else b""

            def poll(self):
                return 0 if not self._chunks else None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                pass

        dummy_proc = DummyProcess()
        dummy_rec = LowConfRecognizer()

        with mock.patch("subprocess.Popen", return_value=dummy_proc):
            with mock.patch.object(listener, "_resolve_model", return_value=object()):
                with mock.patch("vosk.KaldiRecognizer", return_value=dummy_rec):
                    with mock.patch("vice.voice.capture_audio_cmd", return_value=["parec"]):
                        listener._listen_loop()

        self.assertEqual(len(triggered), 0)
