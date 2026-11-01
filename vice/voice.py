"""Voice-activated clipping for Vice.

Listens on the configured microphone for trigger phrases such as "Vice, clip that"
or "Clip that" (plus an optional user-defined phrase) and triggers a clip save.
Runs completely locally and offline via Vosk and PulseAudio / PipeWire.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

log = logging.getLogger("vice.voice")

DEFAULT_PHRASES = ("vice clip that", "clip that")


def normalize_phrase(text: str) -> str:
    """Normalize spoken or configured phrases: lowercase, remove punctuation, collapse spaces."""
    cleaned = re.sub(r"[^\w\s]", " ", str(text or "")).lower()
    return " ".join(cleaned.split())


def active_phrases(custom: str = "") -> list[str]:
    """List of active recognition phrases, including defaults and custom override."""
    phrases = list(DEFAULT_PHRASES)
    if custom:
        norm = normalize_phrase(custom)
        if norm and norm not in phrases:
            phrases.append(norm)
    return phrases


def build_grammar(custom: str | list[str] = "") -> str:
    """Grammar JSON array for Vosk constrained recognition."""
    if isinstance(custom, (list, tuple, set)):
        phrases = list(custom)
    else:
        phrases = active_phrases(custom)
    return json.dumps(phrases + ["[unk]"])


def match_trigger_phrase(
    text: str,
    words_list: Optional[list[dict[str, Any]]] = None,
    phrases: Optional[list[str]] = None,
    min_confidence: float = 0.50,
) -> Optional[str]:
    """Check if recognized speech matches an intended wake command.

    Rejects:
    - Phonetic near-misses (such as 'click that' vs 'clip that')
    - Non-command phrases (such as 'talking about a clip' or 'did you see that clip')
    - Embedded conversational sentences (such as 'I was talking about a clip that I saw')
    - Low-confidence words
    """
    if phrases is None:
        phrases = list(DEFAULT_PHRASES)

    norm_text = normalize_phrase(text)
    if not norm_text:
        return None

    text_words = norm_text.split()
    if not text_words:
        return None

    sorted_phrases = sorted(phrases, key=lambda s: len(s.split()), reverse=True)

    for p in sorted_phrases:
        p_words = p.split()
        p_len = len(p_words)
        if not p_words or p_len > len(text_words):
            continue

        for idx in range(len(text_words) - p_len + 1):
            if text_words[idx : idx + p_len] == p_words:
                words_before = idx
                words_after = len(text_words) - (idx + p_len)

                # Intent guard: Voice wake commands are brief.
                # Allow small leading fillers ("hey", "yo", "can you") up to 2 words.
                # Allow small trailing fillers ("please", "now", "bro") up to 1 word.
                # Total utterance must not exceed phrase length + 3 words.
                if words_before > 2 or words_after > 1 or len(text_words) > p_len + 3:
                    continue

                # Check confidence if word list is present
                if words_list:
                    aligned = [normalize_phrase(w.get("word", "")) for w in words_list]
                    sub_match = False
                    for w_idx in range(len(aligned) - p_len + 1):
                        if aligned[w_idx : w_idx + p_len] == p_words:
                            confs = [words_list[w_idx + k].get("conf", 1.0) for k in range(p_len)]
                            if any(c < min_confidence for c in confs):
                                break
                            sub_match = True
                            break
                    if not sub_match:
                        continue

                return p

    return None


def is_vosk_available() -> bool:
    """Check if the vosk speech recognition package is installed."""
    try:
        import vosk  # noqa: F401
        return True
    except ImportError:
        return False


def capture_audio_cmd(mic_source: str = "default_input") -> list[str]:
    """Construct command to capture 16kHz 16-bit mono raw PCM from the microphone."""
    if shutil.which("parec"):
        cmd = ["parec", "--rate=16000", "--channels=1", "--format=s16le", "--latency-msec=50", "--raw"]
        if mic_source and mic_source != "default_input":
            dev = mic_source[len("device:"):] if mic_source.startswith("device:") else mic_source
            cmd += ["-d", dev]
        return cmd

    if shutil.which("pw-record"):
        cmd = ["pw-record", "--rate=16000", "--channels=1", "--format=s16", "-"]
        if mic_source and mic_source != "default_input":
            dev = mic_source[len("device:"):] if mic_source.startswith("device:") else mic_source
            cmd += ["--target", dev]
        return cmd

    if shutil.which("ffmpeg"):
        dev = "default"
        if mic_source and mic_source != "default_input":
            dev = mic_source[len("device:"):] if mic_source.startswith("device:") else mic_source
        return [
            "ffmpeg", "-loglevel", "quiet", "-f", "pulse", "-i", dev,
            "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
        ]

    if shutil.which("arecord"):
        return ["arecord", "-q", "-r", "16000", "-c", "1", "-f", "S16_LE", "-t", "raw"]

    return []


class VoiceListener:
    """Background voice listener that triggers a clip upon hearing wake phrases."""

    def __init__(
        self,
        on_trigger: Optional[Callable[..., Any]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        enabled: bool = False,
        custom_phrase: str = "",
        cooldown: float = 4.0,
        min_confidence: float = 0.50,
        mic_source: str = "default_input",
        model_path: str = "",
    ) -> None:
        self.on_trigger = on_trigger
        self._loop = loop
        self._enabled = enabled
        self._custom_phrase = custom_phrase
        self._cooldown = cooldown
        self._min_confidence = min_confidence
        self._mic_source = mic_source
        self._model_path = model_path
        self.error: Optional[str] = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self.last_triggered = 0.0

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def state(self) -> dict:
        return {
            "enabled": self._enabled,
            "active": self.active,
            "error": self.error,
            "phrases": active_phrases(self._custom_phrase),
        }

    async def sync(self, cfg, mic_source: str = "default_input") -> None:
        """Synchronize the listener with current config and audio settings."""
        enabled = bool(getattr(cfg, "enabled", False))
        custom = str(getattr(cfg, "custom_phrase", "") or "").strip()
        cooldown = float(getattr(cfg, "cooldown", 4.0) or 4.0)
        min_confidence = float(getattr(cfg, "min_confidence", 0.50) or 0.50)
        model_path = str(getattr(cfg, "model_path", "") or "").strip()

        needs_restart = (
            self.active
            and (
                self._custom_phrase != custom
                or self._mic_source != mic_source
                or self._model_path != model_path
                or not enabled
            )
        )

        self._enabled = enabled
        self._custom_phrase = custom
        self._cooldown = cooldown
        self._min_confidence = min_confidence
        self._mic_source = mic_source
        self._model_path = model_path

        if needs_restart:
            await self.stop()

        if enabled and not self.active:
            await self.start()

    async def start(self) -> None:
        """Start the background voice recognition thread."""
        if self.active:
            return

        self.error = None
        if not is_vosk_available():
            self.error = "vosk-not-installed"
            log.warning("Voice clipping is enabled, but 'vosk' is not installed.")
            return

        cmd = capture_audio_cmd(self._mic_source)
        if not cmd:
            self.error = "no-audio-capture-tool"
            log.warning("No audio capture tool (parec, pw-record, ffmpeg) found for voice clipping.")
            return

        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen_loop, name="ViceVoiceListener", daemon=True)
        self._thread.start()
        log.info("Voice clipping listener started (phrases: %s)", active_phrases(self._custom_phrase))

    async def stop(self) -> None:
        """Stop audio capture and the recognition worker thread."""
        self._stop_event.set()
        proc = self._proc
        self._proc = None
        if proc:
            try:
                proc.terminate()
            except OSError as exc:
                log.debug("Voice capture process termination note: %s", exc)

        thread = self._thread
        self._thread = None
        if thread and thread.is_alive():
            await asyncio.to_thread(thread.join, 2.0)
        log.info("Voice clipping listener stopped.")

    def _resolve_model(self):
        import vosk
        vosk.SetLogLevel(-1)

        if self._model_path and Path(self._model_path).is_dir():
            return vosk.Model(self._model_path)

        # Standard cache locations
        candidates = [
            Path.home() / ".cache" / "vosk" / "vosk-model-small-en-us-0.15",
            Path.home() / ".local" / "share" / "vice" / "models" / "vosk-model-small-en-us-0.15",
            Path("/usr/share/vosk/vosk-model-small-en-us-0.15"),
        ]
        for cand in candidates:
            if cand.is_dir():
                return vosk.Model(str(cand))

        # Built-in automatic loader
        return vosk.Model(model_name="vosk-model-small-en-us-0.15")

    def _listen_loop(self) -> None:
        """Worker thread loop reading PCM chunks and feeding the Kaldi recognizer."""
        try:
            import vosk
            model = self._resolve_model()
            rec = vosk.KaldiRecognizer(model, 16000)
            rec.SetWords(True)
        except Exception as exc:
            self.error = "model-init-failed"
            log.warning("Failed to initialize Vosk model for voice clipping: %s", exc)
            return

        phrases = active_phrases(self._custom_phrase)
        cmd = capture_audio_cmd(self._mic_source)

        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.error = "audio-spawn-failed"
            log.warning("Failed to spawn audio capture command (%s): %s", cmd[0] if cmd else "", exc)
            return

        chunk_size = 3200  # 0.1 s at 16000 Hz, 16-bit mono

        try:
            while not self._stop_event.is_set():
                if self._proc is None or self._proc.stdout is None:
                    break
                data = self._proc.stdout.read(chunk_size)
                if not data:
                    break

                matched_phrase = None
                if rec.AcceptWaveform(data):
                    try:
                        res = json.loads(rec.Result())
                        raw_text = res.get("text", "")
                        matched_phrase = match_trigger_phrase(
                            raw_text,
                            res.get("result", []),
                            phrases,
                            min_confidence=self._min_confidence,
                        )
                        if not matched_phrase and raw_text:
                            log.debug("Voice recognized utterance: %r (ignored)", raw_text)
                    except (ValueError, KeyError):
                        pass

                if matched_phrase:
                    now = time.monotonic()
                    if now - self.last_triggered >= self._cooldown:
                        self.last_triggered = now
                        rec.Reset()
                        log.info("Voice wake phrase matched: '%s' -> triggering clip", matched_phrase)
                        self._dispatch_trigger(matched_phrase)
                    else:
                        rec.Reset()
                        log.debug("Voice wake phrase '%s' matched during cooldown, ignoring", matched_phrase)
        except Exception as exc:
            log.warning("Voice listener encountered error: %s", exc)
        finally:
            if self._proc:
                try:
                    self._proc.terminate()
                except OSError:
                    pass
                self._proc = None

    def _dispatch_trigger(self, phrase: str = "") -> None:
        """Dispatch clip trigger callback back into the asyncio loop."""
        if not self.on_trigger:
            return

        try:
            if inspect.iscoroutinefunction(self.on_trigger):
                coro = self.on_trigger()
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(coro, self._loop)
                else:
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            loop.create_task(coro)
                        else:
                            asyncio.run(coro)
                    except RuntimeError as exc:
                        log.warning("Could not dispatch voice trigger callback: %s", exc)
            else:
                res = self.on_trigger()
                if asyncio.iscoroutine(res):
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(res, self._loop)
                    else:
                        asyncio.run(res)
        except Exception as exc:
            log.warning("Could not run voice trigger callback: %s", exc)
