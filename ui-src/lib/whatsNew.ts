/**
 * What the "What's new" window shows, per release. Only the things a user
 * needs to know or act on go here, not the whole changelog; the GitHub
 * release carries that.
 *
 * Each id is a pair of keys in the locale files, `whatsNew.<release>.<id>.title`
 * and `.body`, where <release> is the version with dots as underscores (a dot
 * would read as a key path). A release with no entry shows nothing, so a
 * patch release can skip it.
 */
export interface ReleaseNotes {
  items: string[];
  /** GitHub handles, thanked at the bottom. Names, so never translated. */
  thanks?: string[];
}

export const WHATS_NEW: Record<string, ReleaseNotes> = {
  '2.19.0': {
    items: ['soundTest', 'voiceLength', 'discordSize', 'newLink'],
  },
  '2.18.0': {
    items: ['soundPacks', 'matugen', 'gameTags', 'previews', 'overlayKeys', 'voiceClipping', 'closeWindow'],
    thanks: ['LeonnJD', 'voltek-laruelle', 'Cryptorial0', 'milan483'],
  },
  '2.17.0': {
    items: ['flatpak'],
  },
  '2.16.0': {
    items: ['clipDuration', 'viewerPlaylist', 'shareCopy', 'indonesian'],
    thanks: ['milan483'],
  },
  '2.15.1': {
    items: ['gameCaptureX11'],
  },
  '2.15.0': {
    items: [
      'gameCapture',
      'keyOverlay',
      'trimCopy',
      'discordShare',
      'trackVolume',
      'tray',
      'gameIcons',
      'steamNames',
      'niri',
      'flatpakGsr',
      'languages',
    ],
    thanks: ['bryan-ovalle2', 'suriyahs', '20plays', 'voltek-laruelle', 'marcobabinski', 'MLG993'],
  },
};

export const releaseKey = (version: string) => `v${version.replace(/\./g, '_')}`;

export function notesFor(version: string | undefined): ReleaseNotes | null {
  return version ? WHATS_NEW[version] ?? null : null;
}

/** The newest release that has notes, for the button when this one has none. */
export function latestNotedVersion(): string | null {
  const versions = Object.keys(WHATS_NEW);
  if (!versions.length) return null;
  const parts = (v: string) => v.split('.').map(n => Number(n) || 0);
  return versions.sort((a, b) => {
    const [x, y] = [parts(a), parts(b)];
    for (let i = 0; i < Math.max(x.length, y.length); i++) {
      if ((x[i] ?? 0) !== (y[i] ?? 0)) return (y[i] ?? 0) - (x[i] ?? 0);
    }
    return 0;
  })[0];
}
