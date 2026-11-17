import {useLayoutEffect, useMemo, useRef, useState} from 'react';

import {comboLabel} from '../lib/format';
import {t} from '../lib/i18n';
import {useImageActions} from '../state/imageActions';
import {useStore} from '../state/store';
import {ImageCard} from './ImageCard';
import {OverlayPortal} from './OverlayPortal';

const MIN_CARD = 230;
const MAX_COLUMNS = 4;
const IDEAL = 16 / 9;
// Thumbnails crop to fill, so rows can stretch or squash this far rather than
// leave a strip of empty panel under the last one.
const ASPECT_MIN = 1.45;
const ASPECT_MAX = 2.2;

/** Complete rows that fill the height, as close to 16:9 as the band allows. */
function fitRows(width: number, height: number, gap: number, columns: number) {
  const cardWidth = (width - gap * (columns - 1)) / columns;
  const ideal = cardWidth / IDEAL;
  const fits = Math.max(0, Math.floor((height + gap) / (ideal + gap)));
  let best = {rows: fits, cardHeight: ideal, off: Infinity};
  for (const rows of [fits, fits + 1]) {
    if (rows < 1) continue;
    const cardHeight = (height - gap * (rows - 1)) / rows;
    const aspect = cardWidth / cardHeight;
    const off = Math.abs(Math.log(aspect / IDEAL));
    if (aspect >= ASPECT_MIN && aspect <= ASPECT_MAX && off < best.off) best = {rows, cardHeight, off};
  }
  return {rows: best.rows, cardHeight: Math.max(0, best.cardHeight)};
}

export function HomeImages() {
  const {state, dispatch} = useStore();
  const gridRef = useRef<HTMLDivElement>(null);
  const [layout, setLayout] = useState({columns: 1, rows: 0, height: 0, cardHeight: 0, gap: 0});

  useLayoutEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    const measure = () => {
      const {width, height} = grid.getBoundingClientRect();
      const gap = parseFloat(getComputedStyle(grid).columnGap) || 0;
      const columns = Math.max(1, Math.min(MAX_COLUMNS, Math.floor((width + gap) / (MIN_CARD + gap))));
      const {rows, cardHeight} = width > 0 ? fitRows(width, height, gap, columns) : {rows: 0, cardHeight: 0};
      setLayout(previous => previous.columns === columns && previous.rows === rows && previous.height === height && previous.cardHeight === cardHeight && previous.gap === gap
        ? previous : {columns, rows, height, cardHeight, gap});
    };
    const observer = new ResizeObserver(measure);
    observer.observe(grid);
    measure();
    return () => observer.disconnect();
  }, []);

  const capacity = layout.columns * layout.rows;
  const preview = useMemo(() => state.images.slice(0, capacity), [state.images, capacity]);
  const {actions, overlays} = useImageActions(preview);
  const occupiedRows = Math.ceil(preview.length / layout.columns);
  const fullSpace = occupiedRows < layout.rows || capacity === 0;
  const spare = preview.length < capacity || capacity === 0;
  const bound = (state.config?.hotkeys?.screenshot as string | undefined) || '';

  return (
    <>
      <aside className="home-images" aria-label={t('images.heading')}>
        <header className="home-images-head">
          <div>
            <h2>{t('images.heading')}</h2>
            <p>{t('images.countImages', {count: state.images.length})}</p>
          </div>
          <button type="button" className="section-link" onClick={() => {
            dispatch({type: 'setSearch', query: ''});
            dispatch({type: 'setView', view: 'images', playlistId: null});
          }}>
            {t('home.seeAll')}
          </button>
        </header>
        <div className="home-images-grid" ref={gridRef}
          style={{gridTemplateColumns: `repeat(${layout.columns}, minmax(0, 1fr))`,
            gridTemplateRows: layout.rows ? `repeat(${layout.rows}, ${layout.cardHeight}px)` : 'minmax(0, 1fr)'}}>
          {preview.map(image => <ImageCard key={image.slug} image={image} draggable actions={actions} />)}
          {spare ? (
            <div className="home-images-placeholder" data-compact={layout.height < 240 || !fullSpace || undefined}
              style={{gridColumn: fullSpace ? '1 / -1' : `${preview.length % layout.columns + 1} / -1`,
                gridRow: fullSpace ? `${occupiedRows + 1} / -1` : undefined,
                height: fullSpace ? Math.max(0, layout.height - occupiedRows * (layout.cardHeight + layout.gap)) : undefined}}>
              <svg className="home-images-art" viewBox="0 0 280 180" aria-hidden="true">
                <rect className="image-art-capsule" x="26" y="85" width="230" height="72" rx="36" transform="rotate(-12 140 120)" />
                <path className="image-art-burst" d="m225 17 12 12 17-1 1 17 12 12-12 12-1 17-17-1-12 12-12-12-17 1-1-17-12-12 12-12 1-17 17 1z" />
                <g transform="rotate(8 135 90)">
                  <rect className="image-art-picture" x="63" y="26" width="146" height="120" rx="32" />
                  <circle className="image-art-ink" cx="104" cy="65" r="12" />
                  <path className="image-art-ink" d="m82 119 32-35q5-5 10 0l17 19 17-15q5-5 10 1l24 30z" />
                </g>
              </svg>
              <div>
                <h3>{t(state.images.length ? 'images.roomForMore' : 'images.captureMoment')}</h3>
                <p>{bound ? t('images.captureHint', {hotkey: comboLabel(bound)}) : t('images.captureNoKey')}</p>
                {!bound ? <button type="button" className="btn btn-quiet btn-sm"
                  onClick={() => dispatch({type: 'setView', view: 'settings'})}>{t('home.settings')}</button> : null}
              </div>
            </div>
          ) : null}
        </div>
      </aside>
      <OverlayPortal>{overlays}</OverlayPortal>
    </>
  );
}
