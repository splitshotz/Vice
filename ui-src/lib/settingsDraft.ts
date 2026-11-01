import {formatLengthLong} from './format';
import {t} from './i18n';
import {normalizeKeyLayout} from './keyLayout';
import type {Config} from './types';

/**
 * The settings form as plain data.
 *
 * Everything is held as a draft and written in one patch, because the daemon
 * restarts the recorder on most of these and applying each keystroke would
 * restart it per character. The exceptions persist on the spot and are handled
 * by the screen: the clip hotkey and the microphone toggle, both of which are
 * also reachable from Home and have to agree with it immediately.
 */
export interface Draft {
  bufferDuration: number;
  clipDuration: number;
  fps: string;
  resolution: string;
  customResolution: string;
  container: string;
  encoder: string;
  colorDepth: string;
  backend: string;
  gsrSource: string;
  replayStorage: string;
  display: string;
  followMouse: boolean;
  windowCapture: boolean;
  hardwareDecode: boolean;

  captureAudio: boolean;
  captureMic: boolean;
  desktopSource: string;
  micSource: string;
  micMono: boolean;
  desktopVolume: number;
  /** Percent per separate track, keyed by source id. Missing means 100. */
  audioTrackVolumes: Record<string, number>;
  micVolume: number;
  notifyVolume: number;
  sounds: Record<SoundKey, string>;
  audioTracks: string[];
  mixFirstTrack: boolean;
  wfMicStrategy: string;

  clipKey: string;
  screenshotKey: string;
  keyOverlayKey: string;
  keyOverlay: boolean;
  keyOverlayPosition: string;
  /** Percent. */
  keyOverlayScale: number;
  keyOverlayInClips: boolean;
  keyOverlayLayout: string;
  clipPresets: ClipPreset[];
  hotkeyBlocklist: string;

  directory: string;
  imageDirectory: string;
  tagWithGame: boolean;
  autoPlaylist: boolean;
  clipNameTemplate: string;

  port: number;
  cloudflareTunnel: boolean;
  shareDiscordFile: boolean;

  discordEnabled: boolean;
  discordGameIcons: boolean;
  discordClientId: string;
  discordCustomGames: string;

  gsrArgs: string;
  checkForUpdates: boolean;
}

export interface ClipPreset {
  /** Stable across re-renders so a row keeps its identity while being edited. */
  uid: string;
  key: string;
  duration: number;
}

// The middle field is a locale key, resolved by SoundGrid at render time.
export const SOUND_FIELDS = [
  ['clip_sound', 'settings.soundClip', '~/sounds/clip.wav'],
  ['clip_failed_sound', 'settings.soundClipFailed', '~/sounds/failed.wav'],
  ['session_start_sound', 'settings.soundSessionStart', '~/sounds/start.wav'],
  ['session_end_sound', 'settings.soundSessionEnd', '~/sounds/stop.wav'],
  ['highlight_sound', 'settings.soundHighlight', '~/sounds/highlight.wav'],
  ['screenshot_sound', 'settings.soundScreenshot', '~/sounds/shutter.wav'],
] as const;

export type SoundKey = (typeof SOUND_FIELDS)[number][0];

export const RESOLUTION_PRESETS = [
  ['', 'Auto'],
  ['960x540', '960x540 (540p)'],
  ['1280x720', '1280x720 (720p)'],
  ['1920x1080', '1920x1080 (1080p)'],
  ['2560x1440', '2560x1440 (1440p)'],
  ['3840x2160', '3840x2160 (4K)'],
] as const;

let uidSeq = 0;
const nextUid = () => `preset-${++uidSeq}`;

export function draftFromConfig(config: Config): Draft {
  const r = (config.recording ?? {}) as Record<string, unknown>;
  const h = (config.hotkeys ?? {}) as Record<string, unknown>;
  const o = (config.output ?? {}) as Record<string, unknown>;
  const s = (config.sharing ?? {}) as Record<string, unknown>;
  const d = (config.discord ?? {}) as Record<string, unknown>;
  const u = (config.updates ?? {}) as Record<string, unknown>;
  const n = (config.notifications ?? {}) as Record<string, unknown>;
  const ui = (config.ui ?? {}) as Record<string, unknown>;
  const ov = (config.overlay ?? {}) as Record<string, unknown>;

  const resolution = str(r.resolution, '');
  const isPreset = RESOLUTION_PRESETS.some(([value]) => value === resolution);

  return {
    bufferDuration: num(r.buffer_duration, 120),
    clipDuration: num(r.clip_duration, 20),
    fps: String(num(r.fps, 60)),
    resolution: isPreset ? resolution : 'custom',
    customResolution: isPreset ? '' : resolution,
    container: str(r.container, 'mp4'),
    encoder: str(r.encoder, 'auto'),
    colorDepth: String(r.color_depth ?? '8'),
    backend: str(r.backend, 'auto'),
    gsrSource: str(r.gsr_source, 'auto'),
    replayStorage: str(r.gsr_replay_storage, 'auto'),
    display: str(r.display, ''),
    followMouse: Boolean(r.follow_mouse_display),
    windowCapture: Boolean(r.window_capture),
    hardwareDecode: Boolean(ui.hardware_video_decode),

    captureAudio: r.capture_audio !== false,
    captureMic: Boolean(r.capture_microphone),
    desktopSource: str(r.gsr_audio_source, 'default_output'),
    micSource: str(r.microphone_source, 'default_input'),
    micMono: Boolean(r.microphone_mono),
    desktopVolume: Math.round(num(r.desktop_volume, 1) * 100),
    micVolume: Math.round(num(r.microphone_volume, 1) * 100),
    notifyVolume: Math.round(num(n.sound_volume, 1) * 100),
    sounds: Object.fromEntries(
      SOUND_FIELDS.map(([key]) => [key, str(n[key], '')]),
    ) as Record<SoundKey, string>,
    audioTracks: Array.isArray(r.audio_tracks) ? r.audio_tracks.map(String) : [],
    audioTrackVolumes: Object.fromEntries(
      Object.entries(
        r.audio_track_volumes && typeof r.audio_track_volumes === 'object'
          ? (r.audio_track_volumes as Record<string, unknown>)
          : {},
      ).map(([id, v]) => [id, Math.round(num(v, 1) * 100)]),
    ),
    mixFirstTrack: Boolean(r.audio_tracks_mix_first),
    wfMicStrategy: str(r.wf_microphone_strategy, 'prompt'),

    clipKey: str(h.clip, 'KEY_F9'),
    // No default. An unset screenshot key means no screenshot key, and
    // inventing one here would bind a hotkey nobody asked for.
    screenshotKey: str(h.screenshot, ''),
    keyOverlayKey: str(h.key_overlay, ''),
    keyOverlay: Boolean(ov.keys),
    keyOverlayPosition: str(ov.keys_position, 'bottom-left'),
    keyOverlayScale: Math.round(num(ov.keys_scale, 1) * 100),
    keyOverlayInClips: Boolean(ov.keys_in_clips),
    keyOverlayLayout: normalizeKeyLayout(ov.keys_layout),
    clipPresets: (Array.isArray(h.clip_presets) ? h.clip_presets : []).map(raw => {
      const preset = (raw ?? {}) as Record<string, unknown>;
      return {uid: nextUid(), key: str(preset.key, ''), duration: num(preset.duration, 60)};
    }),
    hotkeyBlocklist: (Array.isArray(h.disable_while_focused) ? h.disable_while_focused : []).join('\n'),

    directory: str(o.directory, ''),
    imageDirectory: str(o.image_directory, ''),
    tagWithGame: o.tag_clips_with_game !== false,
    autoPlaylist: o.auto_playlist_by_game !== false,
    clipNameTemplate: str(o.clip_name_template, ''),

    port: num(s.port, 8765),
    cloudflareTunnel: s.cloudflare_tunnel !== false,
    shareDiscordFile: Boolean(s.share_discord_file),

    discordEnabled: Boolean(d.enabled),
    discordGameIcons: d.game_icons !== false,
    discordClientId: str(d.client_id_override, ''),
    discordCustomGames: (Array.isArray(d.custom_games) ? d.custom_games : [])
      .map(raw => {
        const game = (raw ?? {}) as {name?: string; matches?: string[]};
        return `${game.name ?? ''} | ${(game.matches ?? []).join(', ')}`;
      })
      .join('\n'),

    gsrArgs: str(r.gsr_args, ''),
    checkForUpdates: u.check_on_start !== false,
  };
}

export const newClipPreset = (): ClipPreset => ({uid: nextUid(), key: '', duration: 60});

/** null means auto, false means the field is not a resolution. */
export function resolvedResolution(draft: Draft): string | null | false {
  if (draft.resolution !== 'custom') return draft.resolution || null;
  const raw = draft.customResolution.trim().toLowerCase().replace('×', 'x');
  if (!raw) return null;
  return /^\d{2,5}x\d{2,5}$/.test(raw) ? raw : false;
}

/**
 * The buffer has to be at least as long as the longest clip any key can save,
 * otherwise that key produces a clip shorter than it asks for. The old form
 * corrected the slider silently on save; this returns the correction so the
 * screen can show it happening.
 */
export function requiredBuffer(draft: Draft): number {
  const longestClip = Math.max(
    draft.clipDuration,
    ...draft.clipPresets.map(p => Number(p.duration) || 0),
  );
  return Math.max(draft.bufferDuration, longestClip);
}

export function patchFromDraft(draft: Draft): Record<string, Record<string, unknown>> {
  const resolution = resolvedResolution(draft);
  return {
    recording: {
      buffer_duration: requiredBuffer(draft),
      clip_duration: draft.clipDuration,
      fps: Number(draft.fps),
      display: draft.display || null,
      follow_mouse_display: draft.followMouse,
      window_capture: draft.windowCapture,
      resolution: resolution === false ? null : resolution,
      container: draft.container,
      encoder: draft.encoder,
      color_depth: draft.colorDepth,
      backend: draft.backend,
      gsr_source: draft.gsrSource,
      capture_audio: draft.captureAudio,
      gsr_replay_storage: draft.replayStorage,
      capture_microphone: draft.captureMic,
      microphone_source: draft.micSource || 'default_input',
      microphone_mono: draft.micMono,
      desktop_volume: draft.desktopVolume / 100,
      microphone_volume: draft.micVolume / 100,
      wf_microphone_strategy: draft.wfMicStrategy,
      gsr_audio_source: draft.desktopSource || 'default_output',
      audio_tracks: [...draft.audioTracks],
      audio_track_volumes: Object.fromEntries(
        draft.audioTracks.map(id => [id, (draft.audioTrackVolumes[id] ?? 100) / 100]),
      ),
      audio_tracks_mix_first: draft.mixFirstTrack,
      gsr_args: draft.gsrArgs.trim(),
    },
    hotkeys: {
      clip: draft.clipKey,
      screenshot: draft.screenshotKey.trim(),
      key_overlay: draft.keyOverlayKey.trim(),
      clip_presets: draft.clipPresets
        .map(p => ({key: p.key.trim(), duration: Number(p.duration) || 60}))
        .filter(p => p.key || p.duration),
      disable_while_focused: splitLines(draft.hotkeyBlocklist),
    },
    output: {
      directory: draft.directory,
      image_directory: draft.imageDirectory,
      tag_clips_with_game: draft.tagWithGame,
      auto_playlist_by_game: draft.autoPlaylist,
      clip_name_template: draft.clipNameTemplate.trim(),
    },
    sharing: {
      port: Number(draft.port),
      cloudflare_tunnel: draft.cloudflareTunnel,
      share_discord_file: draft.shareDiscordFile,
    },
    // keys_x and keys_y are left out: they belong to the drag, and the
    // daemon writes them itself.
    overlay: {
      keys: draft.keyOverlay,
      keys_position: draft.keyOverlayPosition,
      keys_scale: draft.keyOverlayScale / 100,
      keys_in_clips: draft.keyOverlayInClips,
      keys_layout: draft.keyOverlayLayout,
    },
    updates: {check_on_start: draft.checkForUpdates},
    notifications: {
      sound_volume: draft.notifyVolume / 100,
      ...Object.fromEntries(
        SOUND_FIELDS.map(([key]) => [key, draft.sounds[key]?.trim() || null]),
      ),
    },
    ui: {hardware_video_decode: draft.hardwareDecode},
    discord: {
      enabled: draft.discordEnabled,
      game_icons: draft.discordGameIcons,
      client_id_override: draft.discordClientId.trim() || null,
      custom_games: parseCustomGames(draft.discordCustomGames),
    },
  };
}

/** Each line is `Display Name | match1, match2`. A line missing either is dropped. */
export function parseCustomGames(text: string): Array<{name: string; matches: string[]}> {
  const out: Array<{name: string; matches: string[]}> = [];
  for (const raw of (text || '').split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const [namePart, matchPart = ''] = line.split('|');
    const name = (namePart || '').trim();
    const matches = matchPart
      .split(',')
      .map(s => s.trim())
      .filter(Boolean);
    if (!name || matches.length === 0) continue;
    out.push({name, matches});
  }
  return out;
}

/**
 * Mirrors _render_clip_name in recorder.py, so the preview is not a guess.
 *
 * split/join rather than replaceAll: the bundle targets es2020 for the older
 * WebKit2GTK fallback, and this runs on every keystroke in the field.
 */
export function renderClipName(template: string, n: number, game: string, now: Date): string {
  const pad = (v: number) => String(v).padStart(2, '0');
  const sub = (text: string, token: string, value: string) => text.split(token).join(value);
  let out = sub(template, '$n', String(n));
  out = sub(out, '$date', `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`);
  out = sub(out, '$time', pad(now.getHours()) + pad(now.getMinutes()));
  out = sub(out, '$game', game || '');
  return out
    // eslint-disable-next-line no-control-regex
    .replace(/[/\\\x00-\x1f]/g, '')
    .replace(/[_-]{2,}/g, (m: string) => m[0])
    .replace(/^[_\-. ]+|[_\-. ]+$/g, '');
}

/**
 * A long buffer held in RAM is the setting most likely to hurt, and nothing
 * about the slider says so, so the note under it does the arithmetic.
 */
export function bufferNote(draft: Draft): {text: string; tone: 'plain' | 'warning'} {
  const inRam =
    draft.replayStorage === 'ram' || (draft.replayStorage === 'auto' && draft.bufferDuration <= 600);
  if (inRam && draft.bufferDuration > 600) {
    const gb = ((draft.bufferDuration * 1.5) / 1024).toFixed(1);
    return {
      text: t('settings.bufferRamWarning', {
        length: formatLengthLong(draft.bufferDuration),
        gb,
      }),
      tone: 'warning',
    };
  }
  if (!inRam) return {text: t('settings.bufferOnDisk'), tone: 'plain'};
  return {text: t('settings.bufferPlain'), tone: 'plain'};
}

/** Mirrors _classify_gsr_source in recorder.py. */
export function trackPartKind(id: string): 'monitor' | 'input' | 'app' | 'unknown' {
  const value = (id || '').trim();
  if (value === 'default_output') return 'monitor';
  if (value === 'default_input') return 'input';
  if (value.startsWith('app:') || value.startsWith('app-inverse:')) return 'app';
  if (value.startsWith('device:')) return value.endsWith('.monitor') ? 'monitor' : 'input';
  return 'unknown';
}

/**
 * With desktop audio off the recorder keeps only microphone sources, so a game
 * track vanishes and the user is left with just their voice and nothing
 * anywhere saying why (#137). Work out what would go, so the UI can say it.
 */
export function tracksLostWithoutDesktopAudio(tracks: string[]): {
  dropped: string[];
  trimmed: string[];
} {
  const dropped: string[] = [];
  const trimmed: string[] = [];
  for (const id of tracks) {
    const parts = String(id).split('|').filter(Boolean);
    const kept = parts.filter(p => trackPartKind(p) === 'input');
    if (!kept.length) dropped.push(id);
    else if (kept.length !== parts.length) trimmed.push(id);
  }
  return {dropped, trimmed};
}

const splitLines = (text: string) =>
  text
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean);

const num = (value: unknown, fallback: number) =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback;
const str = (value: unknown, fallback: string) =>
  typeof value === 'string' && value ? value : fallback;
