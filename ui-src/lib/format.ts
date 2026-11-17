/** Formatting shared across screens. */

/**
 * 95 becomes "1:35". With `withUnit`, anything under a minute reads as "20s".
 *
 * Under a minute a fraction is kept, because these are the readouts where it
 * carries the meaning: a half-second trim selection rendered as "0s" says the
 * selection is empty when it is not.
 */
export function formatDuration(seconds: number, withUnit = false): string {
  if (!Number.isFinite(seconds) || seconds < 0) return withUnit ? '0s' : '0:00';
  const whole = Math.floor(seconds);
  if (withUnit && seconds < 60) return `${seconds.toFixed(seconds % 1 ? 1 : 0)}s`;
  const m = Math.floor(whole / 60);
  const s = whole % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}:${String(m % 60).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
  return `${m}:${String(s).padStart(2, '0')}`;
}

/**
 * The spelled-out form the settings sliders use: "20 s", "10 min", "2:30 min".
 *
 * A buffer length is a quantity being chosen, not a position being read, and
 * "10:00" beside a slider reads like a timestamp. This is the wording the old
 * form used and it was right.
 */
export function formatLengthLong(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0 s';
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const m = Math.floor(seconds / 60);
  const r = Math.round(seconds % 60);
  return r === 0 ? `${m} min` : `${m}:${String(r).padStart(2, '0')} min`;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 MB';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** i;
  return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

/**
 * Turn an evdev key name into something a person recognises: KEY_F9 is F9,
 * KEY_LEFTCTRL is Left Ctrl.
 */
export function hotkeyLabel(key: string | undefined | null): string {
  if (!key) return 'the clip key';
  return key
    .replace(/^KEY_/, '')
    .toLowerCase()
    .replace(/^left/, 'left ')
    .replace(/^right/, 'right ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

// Either side's modifier fires a binding, so a combo names neither side.
const MODIFIER_LABELS: Record<string, string> = {
  CTRL: 'Ctrl',
  ALT: 'Alt',
  SHIFT: 'Shift',
  META: 'Super',
};

/** A whole binding as people write it: KEY_LEFTALT+KEY_F9 is Alt+F9. */
export function comboLabel(combo: string): string {
  const parts = combo.split('+').filter(Boolean);
  return parts
    .map(part => {
      const side = /^KEY_(?:LEFT|RIGHT)(CTRL|ALT|SHIFT|META)$/.exec(part);
      return side && parts.length > 1 ? MODIFIER_LABELS[side[1]] : hotkeyLabel(part);
    })
    .join('+');
}

/** Takes the daemon's ISO timestamps, which carry no timezone suffix. */
export function relativeTime(isoTimestamp: string | null | undefined): string {
  if (!isoTimestamp) return '';
  const then = new Date(isoTimestamp).getTime();
  if (Number.isNaN(then)) return '';
  const diff = (Date.now() - then) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(then).toLocaleDateString();
}
