import {useCallback, useEffect, useMemo, useRef, useState} from 'react';

import type {Clip} from '../lib/types';

export interface ClipSelection {
  selected: Set<string>;
  /** True once anything is picked: a plain click then toggles instead of opening. */
  selecting: boolean;
  /** Shift extends from the last clip picked, in the order the grid shows them. */
  pick: (clip: Clip, opts?: {range?: boolean}) => void;
  clear: () => void;
  selectAll: () => void;
  clips: Clip[];
}

/**
 * Multi-select over the clips a grid is showing. Ctrl or Cmd+click picks a
 * clip, Shift+click picks a run, Esc lets go and Ctrl+A takes everything in
 * view. Clips that leave the grid (filtered out, deleted) drop out of the
 * selection, so a bulk action never reaches something the user cannot see.
 */
export function useClipSelection(visible: Clip[]): ClipSelection {
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const anchor = useRef<string | null>(null);

  useEffect(() => {
    const shown = new Set(visible.map(c => c.slug));
    setSelected(prev => {
      const next = new Set([...prev].filter(slug => shown.has(slug)));
      return next.size === prev.size ? prev : next;
    });
  }, [visible]);

  const pick = useCallback(
    (clip: Clip, opts: {range?: boolean} = {}) => {
      setSelected(prev => {
        const next = new Set(prev);
        const from = anchor.current ? visible.findIndex(c => c.slug === anchor.current) : -1;
        const to = visible.findIndex(c => c.slug === clip.slug);
        if (opts.range && from >= 0 && to >= 0) {
          const [lo, hi] = from < to ? [from, to] : [to, from];
          for (const c of visible.slice(lo, hi + 1)) next.add(c.slug);
        } else if (next.has(clip.slug)) {
          next.delete(clip.slug);
        } else {
          next.add(clip.slug);
        }
        return next;
      });
      anchor.current = clip.slug;
    },
    [visible],
  );

  const clear = useCallback(() => {
    anchor.current = null;
    setSelected(new Set());
  }, []);

  const selectAll = useCallback(() => setSelected(new Set(visible.map(c => c.slug))), [visible]);

  const selecting = selected.size > 0;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (e.key === 'Escape' && selecting) {
        clear();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a' && selecting) {
        e.preventDefault();
        selectAll();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [clear, selectAll, selecting]);

  const clips = useMemo(() => visible.filter(c => selected.has(c.slug)), [selected, visible]);
  return {selected, selecting, pick, clear, selectAll, clips};
}
