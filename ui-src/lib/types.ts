/**
 * Shapes the daemon actually sends. Verified against a running ShareServer
 * rather than inferred, because several of them are not what you would guess:
 * list endpoints are wrapped in an envelope, timestamps are ISO strings, and
 * `name` still carries the file extension.
 */

export interface Clip {
  slug: string;
  /** The filename, extension included. Use clipTitle() for display. */
  name: string;
  size: number;
  created_at: string;
  game: string | null;
  views: number;
  /** Keys were kept beside this clip for the viewer's keyboard overlay. */
  keys?: boolean;
  duration: number | null;
  width: number | null;
  height: number | null;
  vcodec: string | null;
  /** The H.264 preview used when this window cannot play the clip's codec. */
  preview?: 'none' | 'working' | 'ready';
  audio_tracks?: Array<{index: number; title: string; language: string; channels: number}>;
  /** Set when ffprobe could not read the file. The clip is left on disk. */
  unreadable: boolean;
  unreadable_reason: string;
  share_url: string;
  share_is_public: boolean;
  /** Already carries a content revision, so it can be used as a cache key. */
  video_url: string;
  thumb_url: string;
}

/**
 * A screenshot. Deliberately not a Clip with the video fields nulled: an image
 * has no duration, no codec and no share link, and every one of those absences
 * would have to be special-cased at the point of use.
 */
export interface Image {
  slug: string;
  /** The filename, extension included. Use imageTitle() for display. */
  name: string;
  size: number;
  created_at: string;
  game: string | null;
  width: number;
  height: number;
  /** Carries the file revision, so an annotation shows rather than the cache. */
  image_url: string;
  thumb_url: string | null;
}

/** A marked timestamp inside one clip. The id is a string of digits. */
export interface Highlight {
  id: string;
  time: number;
  label: string;
  color: string;
}

export interface Playlist {
  id: string;
  kind: 'auto' | 'custom' | string;
  name: string;
  game: string | null;
  emoji: string | null;
  color1: string | null;
  color2: string | null;
  clip_slugs: string[];
  created_at: string;
  edited: boolean;
}

export interface Status {
  running: boolean;
  version: string;
  /** A count, not the clips themselves. */
  clips: number;
  images?: number;
  local_url: string;
  public_url: string | null;
  base_url: string;
  public_is_tunnel: boolean;
  recording: boolean;
  backend: string;
  session_active: boolean;
  hotkeys_available: boolean;
  /** False while the recorder is down, paired with recorder_error (#156). */
  ready: boolean;
  recorder_error: string | null;
  cpu_fallback: boolean;
  /** Why the GPU encoder would not open, when Vice can tell (#247). */
  cpu_fallback_reason?: {
    kind: 'nvenc_api';
    driver: string;
    required: string;
    /** The Flatpak gpu-screen-recorder is installed, and which one ran. */
    flatpak?: boolean;
    source?: 'native' | 'flatpak' | null;
  } | null;
  gsr?: {source: 'native' | 'flatpak' | null; flatpak: boolean} | null;
  /** Set when Game Capture cannot pin a window here: GSR only does it on X11. */
  window_capture_unavailable?: 'wayland' | null;
  /** Why automatic session start is unavailable in the current session. */
  session_auto_start_error?: 'unsupported' | 'detection' | 'state' | null;
  codec_fallback: boolean;
  /** Where clips land. Absent when the drive could not be measured. */
  disk?: {free: number; total: number} | null;
  update?: UpdateInfo | null;
  key_overlay?: KeyOverlayState | null;
  /** Where Vice runs; nothing switches on it yet beyond the Flatpak setup. */
  platform?: PlatformInfo | null;
  voice?: VoiceState | null;
}

export interface VoiceState {
  enabled: boolean;
  active: boolean;
  error: string | null;
  phrases: string[];
}

export interface PlatformInfo {
  device: 'steamdeck' | 'desktop';
  os_id: string;
  variant: string;
  immutable: boolean;
  packaging: 'flatpak' | 'aur' | 'script' | 'nix' | 'unknown';
  /** "gamescope" in Steam Deck Game Mode, else the desktop's name. */
  session: string;
}

/** Installing the gpu-screen-recorder Flatpak from the Vice Flatpak. */
export interface GsrInstall {
  phase: 'preparing' | 'downloading' | 'installing' | 'done' | 'error';
  /** Overall percent, or null while flatpak has not said. */
  percent: number | null;
  error?: string | null;
  /** What to run by hand when the button could not do it. */
  command?: string | null;
}

/** The on-screen keyboard overlay's process, as the daemon sees it. */
export interface KeyOverlayState {
  running: boolean;
  error: 'no-layer-shell' | 'no-gtk' | 'no-display' | 'crashed' | null;
  moving: boolean;
}

export interface UpdateInfo {
  version: string;
  url?: string;
  /** Summarised release-note lines, one per bullet. */
  notes?: string[];
  /**
   * How this machine should update, worked out by the daemon: "aur",
   * "flatpak", "nix", "script" or "unknown". The command is empty for unknown.
   */
  install?: {method?: string; command?: string};
}

export interface Config {
  recording: Record<string, unknown> & {
    buffer_duration: number;
    clip_duration: number;
    fps: number;
  };
  hotkeys: Record<string, unknown> & {clip: string; clip_presets: unknown[]};
  output: Record<string, unknown> & {directory: string};
  sharing: Record<string, unknown> & {port: number; cloudflare_tunnel: boolean};
  discord: Record<string, unknown> & {enabled: boolean};
  updates: Record<string, unknown>;
  notifications: Record<string, unknown>;
  ui: Record<string, unknown>;
  overlay?: Record<string, unknown>;
  voice?: Record<string, unknown> & {
    enabled?: boolean;
    custom_phrase?: string;
    cooldown?: number;
    min_confidence?: number;
  };
}

/** Every message the daemon broadcasts. */
export type WsMessage =
  | {type: 'clip_saved'; clip: Clip}
  | {type: 'clip_deleted'; slug: string}
  | {type: 'image_saved'; image: Image}
  | {type: 'image_deleted'; slug: string}
  | {type: 'image_error'; error?: string}
  | {type: 'image_copy_failed'; error?: string}
  | {type: 'playlists_changed'; playlists: Playlist[]}
  | {type: 'clip_saving'}
  | {type: 'clip_error'; error?: string}
  | ({type: 'status'} & Partial<Status>)
  | {type: 'tunnel_url'; url: string}
  | {type: 'tunnel_error'; error?: string}
  | {type: 'share_links_changed'; links: Record<string, string>; share_is_public: boolean}
  | {type: 'session_start'; origin?: 'manual' | 'automatic'; auto_stop?: 'off' | 'armed' | 'unavailable'}
  | {type: 'session_auto_start_failed'}
  | {type: 'session_stop'}
  | {type: 'session_highlight'; time?: number}
  | {type: 'export_progress'; [key: string]: unknown}
  | {type: 'export_done'; [key: string]: unknown}
  | {type: 'export_error'; [key: string]: unknown}
  | {type: 'editor_project_changed'}
  | {type: 'daemon_upgrading'; version?: string}
  | ({type: 'update_available'} & UpdateInfo)
  | ({type: 'key_overlay'} & KeyOverlayState)
  | ({type: 'gsr_install'} & GsrInstall)
  | {type: 'matugen'; colors: Record<string, string> | null}
  | {type: 'config_changed'};

export type ViewName = 'home' | 'clips' | 'images' | 'editor' | 'settings' | 'about';

/** The filename without its extension, which is what every screen shows. */
export function clipTitle(clip: Clip): string {
  return clip.name.replace(/\.(mp4|mkv|mov|webm)$/i, '');
}

export function imageTitle(image: Image): string {
  return image.name.replace(/\.(png|jpe?g)$/i, '');
}

/**
 * Clips and images share one playlist membership list, so an image is stored
 * under a prefix. Mirrors playlists.IMAGE_PREFIX on the daemon; if the two
 * ever disagree, membership silently stops matching.
 */
export const IMAGE_PREFIX = 'img:';

export const imageSlug = (slug: string) => `${IMAGE_PREFIX}${slug}`;
