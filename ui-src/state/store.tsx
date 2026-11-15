import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from 'react';

import {api} from '../lib/api';
import {connectWs} from '../lib/ws';
import {formatDuration, hotkeyLabel} from '../lib/format';
import {H264_SUPPORTED} from '../lib/env';
import {ACCENT_NAMES, DEFAULT_ACCENT} from '../theme/accents';
import type {AccentChoice} from '../theme/viceTheme';
import {clipTitle, imageSlug, imageTitle, type KeyOverlayState} from '../lib/types';
import type {
  Clip,
  Config,
  GsrInstall,
  Image,
  Playlist,
  Status,
  UpdateInfo,
  ViewName,
  WsMessage,
} from '../lib/types';
import {t} from '../lib/i18n';

/**
 * What the status island is showing. `ambient` is the standing state of the
 * recorder; `event` is a transient that takes over for a few seconds and then
 * hands back. Keeping them separate is what lets a clip-saved notice appear
 * without losing track of the fact that a session is still running.
 */
export type IslandTone = 'neutral' | 'accent' | 'live' | 'error';

export interface IslandEvent {
  id: number;
  kind: 'saving' | 'saved' | 'error' | 'info';
  title: string;
  detail?: string;
  tone: IslandTone;
  /** How long it holds the island before the ambient state returns. */
  holdMs: number;
}

export const CUSTOM_ACCENT_KEY = 'vice-custom-accent';

export type BannerId = 'recorder' | 'cpu' | 'codec-gpu' | 'codec-h264';

interface State {
  ready: boolean;
  loadError: string | null;
  config: Config | null;
  clips: Clip[];
  images: Image[];
  playlists: Playlist[];
  status: Status;
  tunnelUrl: string | null;
  update: UpdateInfo | null;
  /** Slugs that arrived this session, for the "new" treatment in the grid. */
  recentNew: string[];
  view: ViewName;
  currentPlaylistId: string | null;
  searchQuery: string;
  accent: AccentChoice;
  /** The seed for a custom accent, as #rrggbb. Null when none is saved. */
  customAccent: string | null;
  /** Colours matugen wrote for Vice, when it has (#295). */
  matugen: Record<string, string> | null;
  event: IslandEvent | null;
  sessionStartedAt: number | null;
  dismissed: BannerId[];
  /** Bumped whenever the editor should reload its project from the daemon. */
  editorProjectRevision: number;
  gsrInstall: GsrInstall | null;
}

const INITIAL_STATUS: Status = {
  running: false,
  version: '',
  clips: 0,
  local_url: '',
  public_url: null,
  base_url: '',
  public_is_tunnel: false,
  recording: false,
  backend: 'auto',
  session_active: false,
  hotkeys_available: true,
  ready: true,
  recorder_error: null,
  cpu_fallback: false,
  codec_fallback: false,
};

const initialState: State = {
  ready: false,
  loadError: null,
  config: null,
  clips: [],
  images: [],
  playlists: [],
  status: INITIAL_STATUS,
  tunnelUrl: null,
  update: null,
  recentNew: [],
  view: 'home',
  currentPlaylistId: null,
  searchQuery: '',
  accent: DEFAULT_ACCENT,
  customAccent: null,
  matugen: null,
  event: null,
  sessionStartedAt: null,
  dismissed: [],
  editorProjectRevision: 0,
  gsrInstall: null,
};

type Action =
  | {
      type: 'loaded';
      config: Config;
      clips: Clip[];
      images: Image[];
      playlists: Playlist[];
      status: Status;
    }
  | {type: 'loadFailed'; error: string}
  | {type: 'ws'; msg: WsMessage}
  | {type: 'setView'; view: ViewName; playlistId?: string | null}
  | {type: 'setSearch'; query: string}
  | {type: 'setAccent'; accent: AccentChoice; custom?: string | null}
  | {type: 'setMatugen'; colors: Record<string, string> | null}
  | {type: 'setConfig'; config: Config}
  | {type: 'mergeConfig'; patch: Record<string, Record<string, unknown>>}
  | {type: 'setClips'; clips: Clip[]}
  | {type: 'setImages'; images: Image[]}
  | {type: 'setPlaylists'; playlists: Playlist[]}
  | {type: 'event'; event: Omit<IslandEvent, 'id'>}
  | {type: 'clearEvent'; id: number}
  | {type: 'dismiss'; banner: BannerId}
  | {type: 'clearUpdate'}
  | {type: 'gsrInstall'; value: GsrInstall | null};

let eventSeq = 0;

function withEvent(state: State, event: Omit<IslandEvent, 'id'>): State {
  return {...state, event: {...event, id: ++eventSeq}};
}

function reduce(state: State, action: Action): State {
  switch (action.type) {
    case 'loaded':
      return {
        ...state,
        ready: true,
        loadError: null,
        config: action.config,
        clips: action.clips,
        images: action.images,
        playlists: action.playlists,
        status: action.status,
        tunnelUrl: action.status.public_url ?? null,
        update: action.status.update ?? null,
        sessionStartedAt: action.status.session_active ? Date.now() : null,
      };

    case 'loadFailed':
      // Still mark ready: a window stuck on the boot cover cannot tell anyone
      // what went wrong, which is the failure mode #156 was about.
      return {...state, ready: true, loadError: action.error};

    case 'setView':
      return {
        ...state,
        view: action.view,
        currentPlaylistId:
          action.playlistId !== undefined ? action.playlistId : action.view === 'clips' ? state.currentPlaylistId : null,
      };

    case 'setSearch':
      return {...state, searchQuery: action.query};

    case 'setAccent':
      return {
        ...state,
        accent: action.accent,
        // A new seed only arrives with a custom pick; switching back to a
        // named swatch keeps the last custom colour so returning to it does
        // not mean picking it again.
        customAccent: action.custom !== undefined ? action.custom : state.customAccent,
      };

    case 'setMatugen':
      return {...state, matugen: action.colors};

    case 'setConfig':
      return {...state, config: action.config};

    case 'mergeConfig': {
      if (!state.config) return state;
      const config = {...state.config} as unknown as Record<string, Record<string, unknown>>;
      for (const [section, values] of Object.entries(action.patch)) {
        config[section] = {...(config[section] ?? {}), ...values};
      }
      return {...state, config: config as unknown as Config};
    }

    case 'setClips':
      return {...state, clips: action.clips};

    case 'setImages':
      return {...state, images: action.images};

    case 'setPlaylists':
      return {...state, playlists: action.playlists};

    case 'event':
      return withEvent(state, action.event);

    case 'clearEvent':
      return state.event?.id === action.id ? {...state, event: null} : state;

    case 'dismiss':
      return state.dismissed.includes(action.banner)
        ? state
        : {...state, dismissed: [...state.dismissed, action.banner]};

    case 'clearUpdate':
      return {...state, update: null};

    case 'gsrInstall':
      return {...state, gsrInstall: action.value};

    case 'ws':
      return reduceWs(state, action.msg);

    default:
      return state;
  }
}

function reduceWs(state: State, msg: WsMessage): State {
  switch (msg.type) {
    case 'clip_saved': {
      const existing = state.clips.some(c => c.slug === msg.clip.slug);
      const clips = existing
        ? state.clips.map(c => (c.slug === msg.clip.slug ? {...c, ...msg.clip} : c))
        : [msg.clip, ...state.clips];
      const next = {
        ...state,
        clips,
        recentNew: existing ? state.recentNew : [...state.recentNew, msg.clip.slug],
      };
      // An update to a clip already on screen is not news; a new one is.
      return existing
        ? next
        : withEvent(next, {
            kind: 'saved',
            title: t('events.clipSaved'),
            detail: [clipTitle(msg.clip), msg.clip.game].filter(Boolean).join(' · '),
            tone: 'accent',
            holdMs: 4000,
          });
    }

    case 'clip_deleted':
      return {
        ...state,
        clips: state.clips.filter(c => c.slug !== msg.slug),
        recentNew: state.recentNew.filter(s => s !== msg.slug),
      };

    case 'image_saved': {
      const existing = state.images.some(i => i.slug === msg.image.slug);
      const images = existing
        ? state.images.map(i => (i.slug === msg.image.slug ? {...i, ...msg.image} : i))
        : [msg.image, ...state.images];
      const next = {...state, images};
      // An annotation or a rename updates a picture already on screen. Only a
      // genuinely new one is worth taking the island over.
      return existing
        ? next
        : withEvent(next, {
            kind: 'saved',
            title: t('events.imageSaved'),
            detail: [imageTitle(msg.image), msg.image.game].filter(Boolean).join(' · '),
            tone: 'accent',
            holdMs: 4000,
          });
    }

    case 'image_deleted':
      return {...state, images: state.images.filter(i => i.slug !== msg.slug)};

    case 'image_error':
      return withEvent(state, {
        kind: 'error',
        title: t('events.errScreenshot'),
        detail: msg.error || undefined,
        tone: 'error',
        holdMs: 9000,
      });

    case 'image_copy_failed':
      // The picture saved, so this is a warning rather than a failure. Worth
      // saying: the user is about to paste and would get whatever was there.
      return withEvent(state, {
        kind: 'info',
        title: t('events.screenshotNotCopied'),
        detail: msg.error || undefined,
        tone: 'neutral',
        holdMs: 6000,
      });

    case 'playlists_changed':
      return {...state, playlists: msg.playlists ?? []};

    case 'clip_saving':
      return withEvent(state, {
        kind: 'saving',
        title: t('events.savingClip'),
        tone: 'neutral',
        // Long buffers on slow disks take a while, and clip_saved supersedes
        // this anyway, so it is allowed to sit there.
        holdMs: 30000,
      });

    case 'clip_error':
      return withEvent(state, {
        kind: 'error',
        title: t('events.errSaveClip'),
        detail: msg.error || t('events.recorderSilent'),
        tone: 'error',
        holdMs: 9000,
      });

    case 'status': {
      const {type: _ignored, ...partial} = msg;
      // Merge first, then read. A status broadcast may carry only the fields
      // that changed, and deciding from the partial would stop the session
      // clock every time one arrived without session_active in it.
      const merged = {...state.status, ...partial};
      return {
        ...state,
        status: merged,
        sessionStartedAt: merged.session_active ? state.sessionStartedAt ?? Date.now() : null,
        update: partial.update ?? state.update,
      };
    }

    case 'matugen':
      return {...state, matugen: (msg.colors as Record<string, string> | null) ?? null};

    case 'gsr_install': {
      const {type: _ignored, ...progress} = msg;
      return {...state, gsrInstall: progress as GsrInstall};
    }

    case 'key_overlay': {
      const {type: _ignored, ...overlay} = msg;
      return {...state, status: {...state.status, key_overlay: overlay as KeyOverlayState}};
    }

    case 'tunnel_url':
      return withEvent(
        {...state, tunnelUrl: msg.url},
        {kind: 'info', title: t('events.publicLinkReady'), detail: msg.url, tone: 'accent', holdMs: 6000},
      );

    case 'tunnel_error':
      return withEvent(
        {...state, tunnelUrl: null},
        {
          kind: 'error',
          title: t('events.noPublicLink'),
          detail: msg.error || t('events.tunnelUnavailable'),
          tone: 'error',
          holdMs: 9000,
        },
      );

    case 'share_links_changed':
      return {
        ...state,
        clips: state.clips.map(clip => {
          const shareUrl = msg.links[clip.slug];
          return shareUrl === undefined
            ? clip
            : {...clip, share_url: shareUrl, share_is_public: msg.share_is_public};
        }),
      };

    case 'session_start': {
      const sessionStartDetail =
        msg.auto_stop === 'armed'
          ? t('events.sessionAutoStopArmed')
          : msg.auto_stop === 'unavailable'
            ? t('events.sessionAutoStopUnavailable')
            : t('events.doubleTapToStop');
      return withEvent(
        {
          ...state,
          status: {...state.status, recording: true, session_active: true},
          sessionStartedAt: Date.now(),
        },
        {
          kind: 'info',
          title: msg.origin === 'automatic' ? t('events.sessionAutoStarted') : t('events.sessionRecording'),
          detail: sessionStartDetail,
          tone: 'live',
          holdMs: 5000,
        },
      );

    }

    case 'session_auto_start_failed':
      return withEvent(state, {
        kind: 'error',
        title: t('events.sessionAutoStartFailed'),
        detail: t('events.sessionAutoStartFailedDetail'),
        tone: 'error',
        holdMs: 8000,
      });

    case 'session_stop':
      return withEvent(
        {...state, status: {...state.status, session_active: false}, sessionStartedAt: null},
        {kind: 'saved', title: t('events.sessionSaved'), tone: 'accent', holdMs: 4000},
      );

    case 'session_highlight':
      return withEvent(state, {
        kind: 'info',
        title: t('events.highlightMarked'),
        detail: typeof msg.time === 'number' ? formatDuration(msg.time) : undefined,
        tone: 'accent',
        holdMs: 3000,
      });

    case 'update_available': {
      const {type: _ignored, ...info} = msg;
      return {...state, update: info};
    }

    case 'editor_project_changed':
      return {...state, editorProjectRevision: state.editorProjectRevision + 1};

    case 'daemon_upgrading':
      // The socket drops a moment after this and retries every 3 seconds, so
      // the window comes back on its own. Saying so is what stops the gap
      // reading as a crash.
      return withEvent(state, {
        kind: 'info',
        title: t('events.updating', {version: msg.version ?? ''}),
        detail: t('events.updatingDetail'),
        tone: 'accent',
        holdMs: 12000,
      });

    // Export progress belongs to the editor, which subscribes separately.
    case 'export_progress':
    case 'export_done':
    case 'export_error':
      return state;

    default:
      return state;
  }
}

interface Store {
  state: State;
  dispatch: React.Dispatch<Action>;
  /** Clips filtered by the sidebar search and the open playlist. */
  visibleClips: Clip[];
  visibleImages: Image[];
  hotkey: string;
  refreshClips: () => Promise<void>;
  refreshImages: () => Promise<void>;
  refreshPlaylists: () => Promise<void>;
  notify: (event: Omit<IslandEvent, 'id'>) => void;
  saveConfig: (
    patch: Record<string, Record<string, unknown>>,
  ) => Promise<{applied?: boolean; warning?: string; restart_required?: boolean}>;
}

const StoreContext = createContext<Store | null>(null);

export function StoreProvider({children}: {children: ReactNode}) {
  const [state, dispatch] = useReducer(reduce, initialState);

  // First load. Everything the shell needs before it can show a screen.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [config, clips, images, playlists, status] = await Promise.all([
          api.getConfig(),
          api.clips(),
          api.images(),
          api.playlists(),
          api.status(),
        ]);
        if (!cancelled) dispatch({type: 'loaded', config, clips, images, playlists, status});
      } catch (err) {
        if (!cancelled) dispatch({type: 'loadFailed', error: (err as Error).message});
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    void api
      .matugen()
      .then(r => dispatch({type: 'setMatugen', colors: r.colors}))
      .catch(err => console.debug('Reading matugen colours failed', err));
  }, []);

  // The accent is a per-install preference, not a synced setting.
  useEffect(() => {
    let custom: string | null = null;
    try {
      const raw = localStorage.getItem(CUSTOM_ACCENT_KEY);
      custom = raw && /^#[0-9a-f]{6}$/i.test(raw) ? raw.toLowerCase() : null;
    } catch (err) {
      console.debug('Reading the custom accent failed', err);
    }
    const saved = localStorage.getItem('vice-theme');
    const named = saved && (ACCENT_NAMES as string[]).includes(saved);
    // A saved 'custom' with no seed behind it is not a usable accent, so it
    // falls back rather than opening the window in a half-applied theme.
    // Matugen's colours arrive from the daemon a moment later; blue stands in.
    if (named || (saved === 'custom' && custom) || saved === 'matugen') {
      dispatch({type: 'setAccent', accent: saved as AccentChoice, custom});
    } else if (custom) {
      dispatch({type: 'setAccent', accent: DEFAULT_ACCENT, custom});
    }
  }, []);

  // Bumped by every picture event, so a list fetched before one can tell it is
  // out of date. Listing probes every file and takes about a second on a real
  // library, long enough to delete a picture while a stale copy is in flight.
  const imageEvents = useRef(0);
  useEffect(() => connectWs(msg => {
    if (msg.type === 'image_saved' || msg.type === 'image_deleted') imageEvents.current += 1;
    // The daemon changed the config itself (the overlay was dragged, or its
    // key toggled it), so the copy here is stale.
    if (msg.type === 'config_changed') {
      void api
        .getConfig()
        .then(config => dispatch({type: 'setConfig', config}))
        .catch(err => console.debug('Reloading the config failed', err));
    }
    dispatch({type: 'ws', msg});
  }), []);

  // Transient island events expire on their own.
  useEffect(() => {
    if (!state.event) return;
    const {id, holdMs} = state.event;
    const timer = window.setTimeout(() => dispatch({type: 'clearEvent', id}), holdMs);
    return () => window.clearTimeout(timer);
  }, [state.event]);

  // Say it once, on the machines where it is true: clips will never play here.
  useEffect(() => {
    if (state.ready && !H264_SUPPORTED) {
      dispatch({
        type: 'event',
        event: {
          kind: 'error',
          title: t('events.cannotPlay'),
          detail: t('events.noH264'),
          tone: 'error',
          holdMs: 10000,
        },
      });
    }
  }, [state.ready]);

  const refreshClips = useCallback(async () => {
    dispatch({type: 'setClips', clips: await api.clips()});
  }, []);

  const refreshImages = useCallback(async () => {
    const seen = imageEvents.current;
    const images = await api.images();
    // The events that arrived meanwhile are newer than this list and already applied.
    if (imageEvents.current === seen) dispatch({type: 'setImages', images});
  }, []);

  const refreshPlaylists = useCallback(async () => {
    dispatch({type: 'setPlaylists', playlists: await api.playlists()});
  }, []);

  const notify = useCallback((event: Omit<IslandEvent, 'id'>) => {
    dispatch({type: 'event', event});
  }, []);

  /**
   * Persist a partial config and merge it locally.
   *
   * The daemon answers with a result rather than the new config, so the patch
   * we sent is what gets merged, matching what the old UI did. Throws on
   * failure so callers can revert their own control.
   */
  const saveConfig = useCallback(
    async (patch: Record<string, Record<string, unknown>>) => {
      const result = await api.saveConfig(patch);
      if (result.ok === false) throw new Error(result.error || t('events.notSaved'));
      dispatch({type: 'mergeConfig', patch});
      return result;
    },
    [],
  );

  const visibleClips = useMemo(() => {
    const query = state.searchQuery.trim().toLowerCase();
    const playlist = state.currentPlaylistId
      ? state.playlists.find(p => p.id === state.currentPlaylistId)
      : null;
    let list = state.clips;
    if (playlist) {
      const members = new Set(playlist.clip_slugs ?? []);
      list = list.filter(c => members.has(c.slug));
    }
    if (query) {
      list = list.filter(
        c => c.name.toLowerCase().includes(query) || (c.game ?? '').toLowerCase().includes(query),
      );
    }
    return list;
  }, [state.clips, state.playlists, state.currentPlaylistId, state.searchQuery]);

  // Same rules as visibleClips, against the prefixed membership key. Kept
  // separate rather than folded into one list: the two grids are shown on
  // different screens and only meet inside an open playlist.
  const visibleImages = useMemo(() => {
    const query = state.searchQuery.trim().toLowerCase();
    const playlist = state.currentPlaylistId
      ? state.playlists.find(p => p.id === state.currentPlaylistId)
      : null;
    let list = state.images;
    if (playlist) {
      const members = new Set(playlist.clip_slugs ?? []);
      list = list.filter(i => members.has(imageSlug(i.slug)));
    }
    if (query) {
      list = list.filter(
        i => i.name.toLowerCase().includes(query) || (i.game ?? '').toLowerCase().includes(query),
      );
    }
    return list;
  }, [state.images, state.playlists, state.currentPlaylistId, state.searchQuery]);

  const hotkey = useMemo(
    () => hotkeyLabel(state.config?.hotkeys?.clip as string | undefined),
    [state.config],
  );

  const value = useMemo<Store>(
    () => ({
      state,
      dispatch,
      visibleClips,
      visibleImages,
      hotkey,
      refreshClips,
      refreshImages,
      refreshPlaylists,
      notify,
      saveConfig,
    }),
    [
      state,
      visibleClips,
      visibleImages,
      hotkey,
      refreshClips,
      refreshImages,
      refreshPlaylists,
      notify,
      saveConfig,
    ],
  );

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStore(): Store {
  const store = useContext(StoreContext);
  if (!store) throw new Error('useStore was called outside the provider');
  return store;
}
