import type {Clip, Config, Highlight, Image, Playlist, Status} from './types';

/**
 * The daemon's local HTTP API. Every call is same-origin: the public server
 * never exposes any of this.
 */

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: init?.body ? {'Content-Type': 'application/json', ...init?.headers} : init?.headers,
  });
  if (!res.ok) {
    // Prefer the daemon's own explanation. It is usually the useful one.
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as {error?: string};
      if (body.error) detail = body.error;
    } catch {
      // Not JSON. The status line stands on its own.
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, {method: 'POST', body: body === undefined ? undefined : JSON.stringify(body)});

/**
 * A clip slug is a filename, so it can hold `#`, `?`, `%` or `+`, each of which
 * means something else in a URL. Encode every path segment that carries one
 * (#138); the daemon builds `video_url` and `thumb_url` already encoded.
 */
const enc = (segment: string) => encodeURIComponent(segment);

export const api = {
  status: () => request<Status>('/api/status'),

  getConfig: () => request<Config>('/api/config'),
  /**
   * Returns a result, not the config: `applied` is false when the change was
   * stored but could not take effect on the running recorder, and
   * `restart_required` means the daemon needs a restart for it to land.
   */
  saveConfig: (partial: Record<string, unknown>) =>
    post<{
      ok?: boolean;
      error?: string;
      applied?: boolean;
      warning?: string;
      restart_required?: boolean;
    }>('/api/config', partial),

  /** Small cross-session UI flags. Server-side because native localStorage is unreliable. */
  getAppState: () => request<Record<string, unknown>>('/api/app-state'),
  setAppState: (partial: Record<string, unknown>) =>
    post<Record<string, unknown>>('/api/app-state', partial),

  // The list endpoints wrap their payload in an envelope. Unwrapped here so
  // callers only ever see the list.
  clips: async () => (await request<{clips: Clip[]}>('/api/clips')).clips,
  deleteClip: (slug: string) => request<void>(`/api/clips/${enc(slug)}`, {method: 'DELETE'}),
  renameClip: (slug: string, name: string) => post<Clip>(`/api/clips/${enc(slug)}/rename`, {name}),
  setClipGame: (slug: string, game: string) => post<{ok: boolean}>(`/api/clips/${enc(slug)}/game`, {game}),
  matugen: () => request<{colors: Record<string, string> | null}>('/api/theme/matugen'),
  previewSound: (pack: string, sound = 'clip') => post<{ok: boolean}>('/api/sounds/preview', {pack, sound}),
  /** Plays the user's own sound file; `problem` says why it did not play. */
  trySound: (path: string) => post<{ok: boolean; problem: string}>('/api/sounds/try', {path}),
  prepareClipPreview: (slug: string) => post<{ok: boolean}>(`/api/clips/${enc(slug)}/prepare-preview`),
  revealClip: (slug: string) => post<void>(`/api/clips/${enc(slug)}/reveal`),
  openClip: (slug: string) => post<void>(`/api/clips/${enc(slug)}/open`),
  /**
   * Put the clip file on the clipboard, or its Discord-sized copy, which is
   * built first if it does not exist yet. The daemon answers 200 with
   * `ok: false` when there is no clipboard tool, so that has to throw too.
   */
  copyClipFile: async (slug: string, opts: {discord?: boolean} = {}) => {
    const result = await post<{ok?: boolean; error?: string}>(
      `/api/clips/${enc(slug)}/copy-file${opts.discord ? '?discord=1' : ''}`,
    );
    if (result?.ok === false) throw new Error(result.error);
  },
  /** Replaces the clip's share link; the old one stops working. */
  newShareLink: (slug: string) =>
    post<{ok?: boolean; share_url: string}>(`/api/clips/${enc(slug)}/share-link`),
  /**
   * Build (or reuse) a Discord-sized copy of the clip and return where it is.
   * Slow the first time: it transcodes.
   */
  discordCopy: (slug: string) =>
    request<{
      ok?: boolean;
      error?: string;
      path: string;
      filename: string;
      size: number;
    }>(
      `/api/clips/${enc(slug)}/discord`,
    ),
  /** With `copy` the selection becomes a new clip and the original is kept. */
  trimClip: (slug: string, start: number, end: number, copy = false) =>
    post<{slug: string; copy?: boolean}>(`/api/clips/${enc(slug)}/trim`, {start, end, copy}),
  markViewed: (slug: string) => post<{ok?: boolean; views: number}>(`/api/clips/${enc(slug)}/view`),
  /** Save the frame at `time` as a screenshot. Also puts it on the clipboard. */
  saveFrame: (slug: string, time: number) =>
    post<Image & {ok?: boolean; error?: string; copied?: boolean; copy_error?: string}>(
      `/api/clips/${enc(slug)}/frame`,
      {time},
    ),

  images: async () => (await request<{images: Image[]}>('/api/images')).images,
  deleteImage: (slug: string) => request<void>(`/api/images/${enc(slug)}`, {method: 'DELETE'}),
  renameImage: (slug: string, name: string) =>
    post<Image>(`/api/images/${enc(slug)}/rename`, {name}),
  revealImage: (slug: string) => post<void>(`/api/images/${enc(slug)}/reveal`),
  openImage: (slug: string) => post<void>(`/api/images/${enc(slug)}/open`),
  copyImage: (slug: string) => post<{ok?: boolean; error?: string}>(`/api/images/${enc(slug)}/copy`),
  /**
   * The annotated picture goes up as the PNG itself. Base64 inside a JSON
   * envelope would make an already multi-megabyte body a third larger for
   * nothing.
   */
  annotateImage: (slug: string, png: Blob) =>
    request<Image>(`/api/images/${enc(slug)}/annotate`, {
      method: 'POST',
      headers: {'Content-Type': 'image/png'},
      body: png,
    }),

  /** [[from, to, key], ...] in seconds from the clip's start, or null. */
  clipKeys: async (slug: string) =>
    (await request<{keys: [number, number, string][] | null}>(`/api/clips/${enc(slug)}/keys`)).keys,
  moveKeyOverlay: (on: boolean) => post<{ok: boolean}>('/api/overlay/move', {on}),
  /** Vice Flatpak only. Progress arrives as gsr_install WebSocket events. */
  installGsr: () => post<{ok: boolean; error?: string}>('/api/recorder/install-gsr'),
  highlights: async (slug: string) =>
    (await request<{highlights: Highlight[]}>(`/api/clips/${enc(slug)}/highlights`)).highlights ?? [],
  addHighlight: (slug: string, body: Omit<Highlight, 'id'>) =>
    post<{ok?: boolean; error?: string; highlight: Highlight}>(
      `/api/clips/${enc(slug)}/highlights`,
      body,
    ),
  updateHighlight: (slug: string, id: string, body: Partial<Omit<Highlight, 'id'>>) =>
    request<{ok?: boolean; error?: string}>(`/api/clips/${enc(slug)}/highlights/${enc(id)}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    }),
  deleteHighlight: (slug: string, id: string) =>
    request<void>(`/api/clips/${enc(slug)}/highlights/${enc(id)}`, {method: 'DELETE'}),

  triggerClip: () => post<void>('/api/trigger'),

  playlists: async () => (await request<{playlists: Playlist[]}>('/api/playlists')).playlists,
  createPlaylist: (body: unknown) =>
    post<{ok?: boolean; error?: string; playlist: Playlist}>('/api/playlists', body),
  /** Edits are a PATCH; only create and membership are POSTs. */
  updatePlaylist: (id: string, body: unknown) =>
    request<{ok?: boolean; error?: string; playlist: Playlist}>(`/api/playlists/${enc(id)}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    }),
  deletePlaylist: (id: string) => request<void>(`/api/playlists/${enc(id)}`, {method: 'DELETE'}),
  addClipToPlaylist: (id: string, slug: string) =>
    post<{ok?: boolean; error?: string}>(`/api/playlists/${enc(id)}/clips`, {slug}),
  removeClipFromPlaylist: (id: string, slug: string) =>
    request<{ok?: boolean; error?: string}>(`/api/playlists/${enc(id)}/clips/${enc(slug)}`, {
      method: 'DELETE',
    }),

  displays: (backend?: string) =>
    request<{backend: string; displays: unknown[]; warning: string | null}>(
      `/api/displays${backend ? `?backend=${encodeURIComponent(backend)}` : ''}`,
    ),
  audioSources: () => request<{sources: unknown[]; warning: string | null}>('/api/audio-sources'),

  editorProject: () => request<unknown>('/api/editor/project'),
  saveEditorProject: (project: unknown) => post<unknown>('/api/editor/project', project),
  startExport: (body: unknown) => post<{job_id: string}>('/api/editor/export', body),
  cancelExport: (jobId: string) => post<void>(`/api/editor/export/${enc(jobId)}/cancel`),

  checkUpdate: () => post<unknown>('/api/update/check'),
};
