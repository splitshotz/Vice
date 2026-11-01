import {useEffect, useRef, type RefObject} from 'react';

import {t} from '../lib/i18n';
import {keyLayoutRows, keysAt, type KeyInterval} from '../lib/keyLayout';

/**
 * The keys kept beside a clip, drawn over the video in time with it (#209).
 *
 * Imperative on purpose, like the editor engine: it follows the video's own
 * clock every frame, and a React render per frame would cost more than the
 * twenty attributes it changes.
 */
export function KeyOverlay({
  video,
  intervals,
  layout,
}: {
  video: RefObject<HTMLVideoElement | null>;
  intervals: KeyInterval[];
  /** Which keyboard the labels are for, from Settings. QWERTY when unset. */
  layout?: string;
}) {
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let frame = 0;
    const tick = () => {
      const el = video.current;
      const panel = root.current;
      if (el && panel) {
        const held = keysAt(intervals, el.currentTime);
        for (const cap of panel.querySelectorAll<HTMLElement>('[data-key]')) {
          const down = held.has(cap.dataset.key ?? '');
          if (down !== cap.hasAttribute('data-down')) cap.toggleAttribute('data-down', down);
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [video, intervals]);

  return (
    <div className="key-overlay" ref={root} aria-label={t('viewer.keysAria')} role="img">
      {keyLayoutRows(layout).map((row, r) => (
        <div className="key-overlay-row" key={r}>
          {row.map(([id, label, span], k) =>
            id === null ? (
              <span key={k} style={{gridColumn: `span ${span * 2}`}} />
            ) : (
              <span key={k} className="key-cap" data-key={id} style={{gridColumn: `span ${span * 2}`}}>
                {label}
              </span>
            ),
          )}
        </div>
      ))}
    </div>
  );
}
