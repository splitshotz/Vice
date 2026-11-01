/**
 * The keyboard overlay's layout, the same one vice/keys.py draws on screen.
 * Rows of [key id, label, width in key units]; a null id is a gap.
 */
export const KEY_LAYOUT: Array<Array<[string | null, string, number]>> = [
  [[null, '', 0.5], ['1', '1', 1], ['2', '2', 1], ['3', '3', 1], ['4', '4', 1], ['5', '5', 1]],
  [['TAB', 'Tab', 1.5], ['Q', 'Q', 1], ['W', 'W', 1], ['E', 'E', 1], ['R', 'R', 1]],
  [['SHIFT', 'Shift', 1.5], ['A', 'A', 1], ['S', 'S', 1], ['D', 'D', 1], ['F', 'F', 1]],
  [['CTRL', 'Ctrl', 1.5], ['SPACE', '', 4]],
];

/** Keyboards the panel can be labelled for, the same as vice/keys.py. */
export const KEY_LAYOUTS = ['qwerty', 'azerty'] as const;
export type KeyLayoutName = (typeof KEY_LAYOUTS)[number];
export const DEFAULT_KEY_LAYOUT: KeyLayoutName = 'qwerty';

/**
 * Labels that differ from KEY_LAYOUT's, by key id. Ids name the physical key,
 * so the same key lights up on either layout; only what is printed on it
 * changes. On AZERTY the Q, W and A keys print A, Z and Q. The number row
 * keeps 1 to 5, the names games and players use for those keys.
 */
const LAYOUT_LABELS: Record<KeyLayoutName, Record<string, string>> = {
  qwerty: {},
  azerty: {Q: 'A', W: 'Z', A: 'Q'},
};

/** `name` when it is a known layout, the default otherwise. */
export function normalizeKeyLayout(name: unknown): KeyLayoutName {
  const value = String(name ?? '').trim().toLowerCase();
  return (KEY_LAYOUTS as readonly string[]).includes(value) ? (value as KeyLayoutName) : DEFAULT_KEY_LAYOUT;
}

/** KEY_LAYOUT with the labels of layout `name`. Same keys, same widths. */
export function keyLayoutRows(name: unknown): typeof KEY_LAYOUT {
  const labels = LAYOUT_LABELS[normalizeKeyLayout(name)];
  return KEY_LAYOUT.map(row =>
    row.map(([id, label, span]): [string | null, string, number] => [
      id,
      id !== null && labels[id] !== undefined ? labels[id] : label,
      span,
    ]),
  );
}

export type KeyInterval = [number, number, string];

/** Keys held at `time`, from intervals sorted by start. */
export function keysAt(intervals: KeyInterval[], time: number): Set<string> {
  const held = new Set<string>();
  for (const [from, to, key] of intervals) {
    if (from > time) break;
    if (time < to) held.add(key);
  }
  return held;
}
