import {useCallback, useEffect, useLayoutEffect, useRef, useState} from 'react';

import {api} from '../lib/api';
import {useEscape} from '../lib/escape';
import {DEFAULT_MARK_COLOR, MARK_COLORS} from '../lib/palette';
import {useExitTransition} from '../lib/exit';
import {formatBytes, formatDuration} from '../lib/format';
import {
  clipNeedsProxy,
  playQuietly,
  playbackUrl,
  seekTo,
  useVideoFailure,
  videoFailureMessage,
} from '../lib/playback';
import {clipTitle, imageTitle, type Clip, type Highlight} from '../lib/types';
import {IconClose, IconExpand} from './Icons';
import {KeyOverlay} from './KeyOverlay';
import type {KeyInterval} from '../lib/keyLayout';
import {InlineRename} from './InlineRename';
import {t, tNode} from '../lib/i18n';

/** Cycled by index as highlights are added, so a clip's marks stay distinct. */
const HIGHLIGHT_COLORS = MARK_COLORS;

const DEFAULT_HIGHLIGHT_COLOR = DEFAULT_MARK_COLOR;

export interface ViewerProps {
  /** The clip on screen, or null when the viewer is closed. */
  clip: Clip | null;
  /** Every clip, in order, which is what prev and next walk. */
  clips: Clip[];
  highlights: Highlight[];
  onHighlightsChange: (next: Highlight[]) => void;
  onSelect: (slug: string) => void;
  onClose: () => void;
  onTrim: (clip: Clip) => void;
  /** True while the trim modal is open over the viewer or the player bar. */
  trimOpen: boolean;
  onShare: (clip: Clip) => void;
  onShareMenu: (clip: Clip) => void;
  shareLabel: string;
  onReveal: (clip: Clip) => void;
  onDelete: (clip: Clip) => void;
  onOpenExternally: (clip: Clip) => void;
  onRename: (clip: Clip, name: string) => void;
  notify: (title: string, detail?: string, tone?: 'accent' | 'error') => void;
}

/**
 * Playback volume, remembered per viewer rather than per clip.
 *
 * localStorage rather than the daemon's config: this is a property of the
 * window you are watching in, not a recording setting, and the native window
 * cannot always write it, which is only ever a forgotten level (#174).
 */
const VOLUME_KEY = 'vice-volume';
const MUTED_KEY = 'vice-muted';

function storedVolume(): number {
  try {
    const raw = Number(localStorage.getItem(VOLUME_KEY));
    return Number.isFinite(raw) && raw >= 0 && raw <= 1 ? raw : 1;
  } catch {
    return 1;
  }
}

function storedMuted(): boolean {
  try {
    return localStorage.getItem(MUTED_KEY) === '1';
  } catch {
    return false;
  }
}

const KEYS_SHOWN_KEY = 'vice-viewer-keys';

/** Whether a clip's saved keys are drawn over it. On until turned off. */
function storedKeysShown(): boolean {
  try {
    return localStorage.getItem(KEYS_SHOWN_KEY) !== '0';
  } catch {
    return true;
  }
}

function remember(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // A private window or a WebKit build that refuses storage. The level
    // still applies to this session, it just does not survive a restart.
  }
}

/**
 * The viewer: one playback element, two surfaces around it.
 *
 * The modal shows the video and its highlights; the bar below carries the
 * transport and mirrors the same element. They open and close together, and
 * the bar sits in the same column as the modal so it inherits the modal's
 * width and centring rather than measuring them. The old UI floated the bar
 * separately and had to read offsetLeft rather than a client rect, because the
 * modal animates in under a scale() transform that skews a live rect; laying
 * them out together removes the measurement, and with it that whole hazard.
 */
export function Viewer(props: ViewerProps) {
  const {clips, highlights, onHighlightsChange, onSelect, onClose} = props;

  // The viewer is held on screen for the length of its exit so it animates
  // away instead of vanishing between frames. props.clip is already null by
  // then, so the last one is kept to render against; the video is stopped the
  // moment closing starts, or it would keep playing audio behind the fade.
  const {mounted, closing} = useExitTransition(props.clip !== null, 320);
  const lastClip = useRef(props.clip);
  if (props.clip) lastClip.current = props.clip;
  const clip = props.clip ?? lastClip.current;

  const videoRef = useRef<HTMLVideoElement>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({current: 0, duration: 0});
  const [paused, setPaused] = useState(true);
  const [preparing, setPreparing] = useState(false);
  const [dragging, setDragging] = useState<string | null>(null);
  const [picker, setPicker] = useState<{id: string; color: string; at: {x: number; y: number}} | null>(
    null,
  );
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renamingTitle, setRenamingTitle] = useState(false);
  const [volume, setVolume] = useState(storedVolume);
  const [muted, setMuted] = useState(storedMuted);
  const [expanded, setExpanded] = useState(false);
  const [idle, setIdle] = useState(false);
  const [grabbing, setGrabbing] = useState(false);
  const failed = useVideoFailure(videoRef, mounted);
  const [keysShown, setKeysShown] = useState(storedKeysShown);
  const [keyIntervals, setKeyIntervals] = useState<KeyInterval[] | null>(null);
  const [keyLayout, setKeyLayout] = useState<string>('qwerty');

  const index = clip ? clips.findIndex(c => c.slug === clip.slug) : -1;
  const open = props.clip !== null;

  // Attach the source only when it actually changes. Stepping back to the clip
  // already loaded must not reload it, because a fresh load is what counts as
  // a view.
  //
  // Nothing here starts playing while trim is open. Renaming from the trim
  // window changes the clip's slug, which re-attaches the source, and without
  // the guard that put this clip's audio back underneath trim's own preview of
  // the same clip (#175).
  //
  // Reopening the clip that was last shown hands back the same object, so
  // open is a dependency too: the element is new and has no source (#249).
  const trimOpen = props.trimOpen;
  useLayoutEffect(() => {
    const video = videoRef.current;
    if (!open || !video || !clip) return;
    const src = playbackUrl(clip);
    if (video.getAttribute('src') === src) {
      if (!trimOpen) playQuietly(video);
      return;
    }
    video.pause();
    video.src = src;
    video.load();
    setPreparing(clipNeedsProxy(clip));
    setPosition({current: 0, duration: 0});
    if (!trimOpen) playQuietly(video);
    void api
      .markViewed(clip.slug)
      .catch(err => console.debug('Recording the view failed', err));
  }, [clip, trimOpen, open]);

  // The keys kept beside this clip, when there are any (#209).
  const keysSlug = clip?.keys ? clip.slug : null;
  useEffect(() => {
    setKeyIntervals(null);
    if (!keysSlug) return;
    let live = true;
    void api
      .clipKeys(keysSlug)
      .then(keys => live && setKeyIntervals(keys ?? null))
      .catch(err => console.debug('Loading the clip keys failed', err));
    // Which keyboard to label the keys for. Read each time a clip opens, so a
    // change in Settings shows without a reload. QWERTY if it cannot be read.
    void api
      .getConfig()
      .then(cfg => live && setKeyLayout(String(cfg.overlay?.keys_layout ?? 'qwerty')))
      .catch(err => console.debug('Loading the key layout failed', err));
    return () => {
      live = false;
    };
  }, [keysSlug]);

  // Release the decoder when the viewer goes, not merely pause it.
  useEffect(() => {
    if (open) return;
    const video = videoRef.current;
    if (!video?.getAttribute('src')) return;
    video.pause();
    video.removeAttribute('src');
    video.load();
  }, [open]);

  // Applied on every render rather than only on change: attaching a new source
  // resets the element's own volume, so a clip stepped to at 20% would come
  // back at full blast.
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.volume = volume;
    video.muted = muted;
  });

  // Expanding is a layout change inside the Vice window, not the Fullscreen
  // API. QtWebEngine ships with fullscreen support off, so requestFullscreen
  // rejected with "fullscreen is not supported" on the native window, which is
  // the one place this button actually matters. Growing the player to fill the
  // window needs nothing from the host and cannot fail (#177).
  useEffect(() => {
    if (!open) setExpanded(false);
  }, [open]);

  /**
   * Hide the chrome once the pointer stops moving.
   *
   * Only while expanded. At window size the cursor is over the video almost
   * all the time, so the hover rule that makes the play button findable never
   * lets go of it and it sits over the middle of the clip permanently. In the
   * inline viewer the pointer leaves the stage on its own, so nothing there
   * needs this and nothing there changes.
   */
  const IDLE_MS = 3000;
  useEffect(() => {
    if (!expanded) {
      setIdle(false);
      return;
    }
    let timer = 0;
    const wake = () => {
      setIdle(false);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setIdle(true), IDLE_MS);
    };
    wake();
    // On the window, not the stage: a pointer moving over the bar or the play
    // button is still the user being present.
    window.addEventListener('pointermove', wake);
    window.addEventListener('pointerdown', wake);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('pointermove', wake);
      window.removeEventListener('pointerdown', wake);
    };
  }, [expanded]);

  // The box the stage occupied before the toggle, read while it is at rest.
  // Measuring here rather than in the layout effect is the point: by then the
  // new layout has already been applied and the old one is gone.
  const expandFrom = useRef<{rect: DOMRect; radius: string} | null>(null);
  const toggleExpanded = useCallback(() => {
    const stage = stageRef.current;
    expandFrom.current = stage
      ? {rect: stage.getBoundingClientRect(), radius: getComputedStyle(stage).borderTopLeftRadius}
      : null;
    setExpanded(on => !on);
  }, []);

  // Both directions animate, from wherever the stage was to wherever it lands.
  // A one-way keyframe grew it and then snapped it back, and the snap read as
  // a glitch rather than as the same gesture reversed.
  useLayoutEffect(() => {
    const stage = stageRef.current;
    const from = expandFrom.current;
    expandFrom.current = null;
    if (!stage || !from || typeof stage.animate !== 'function') return;

    const first = from.rect;
    const last = stage.getBoundingClientRect();
    if (!first.width || !last.width) return;
    const dx = first.left - last.left;
    const dy = first.top - last.top;
    const sx = first.width / last.width;
    const sy = first.height / last.height;
    // Sub-pixel differences are not a move, and animating one costs a frame.
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && Math.abs(sx - 1) < 0.01) return;

    // Radius is read off both ends rather than written in, so the corners
    // square off on the way out and round again on the way back without this
    // file having to know what --radius-element resolves to.
    stage.animate(
      [
        {transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`, borderRadius: from.radius},
        {transform: 'none', borderRadius: getComputedStyle(stage).borderTopLeftRadius},
      ],
      {duration: 280, easing: 'cubic-bezier(0.2, 0, 0, 1)'},
    );
  }, [expanded]);

  const applyVolume = useCallback((next: number) => {
    // Rounded before it is stored: a pointer position divided by a track width
    // is 0.39999999999999963, and that is what ends up in localStorage and in
    // the element otherwise.
    const level = round3(clamp01(next));
    setVolume(level);
    remember(VOLUME_KEY, String(level));
    // Dragging the slider up off zero is an unmute; making the user press the
    // speaker as well would be a second gesture for one intention.
    if (level > 0) {
      setMuted(false);
      remember(MUTED_KEY, '0');
    }
  }, []);

  const toggleMuted = useCallback(() => {
    setMuted(prev => {
      remember(MUTED_KEY, prev ? '0' : '1');
      return !prev;
    });
  }, []);

  const saveFrame = useCallback(async () => {
    const video = videoRef.current;
    if (!clip || !video || grabbing) return;
    setGrabbing(true);
    try {
      const result = await api.saveFrame(clip.slug, video.currentTime);
      if (result.ok === false) throw new Error(result.error || t('viewer.errSaveFrame'));
      props.notify(
        result.copied === false ? t('viewer.frameSavedNotCopied') : t('viewer.frameSaved'),
        result.copy_error || imageTitle(result),
        'accent',
      );
    } catch (err) {
      props.notify(t('viewer.errSaveFrame'), (err as Error).message, 'error');
    } finally {
      setGrabbing(false);
    }
  }, [clip, grabbing, props]);

  const step = useCallback(
    (delta: number) => {
      if (index < 0) return;
      const next = clips[index + delta];
      if (next) onSelect(next.slug);
    },
    [clips, index, onSelect],
  );

  const addHighlight = useCallback(async () => {
    const video = videoRef.current;
    if (!clip || !video) return;
    const time = video.currentTime;
    const label = nextHighlightLabel(highlights);
    const color = HIGHLIGHT_COLORS[highlights.length % HIGHLIGHT_COLORS.length];
    try {
      const result = await api.addHighlight(clip.slug, {time, label, color});
      if (!result.highlight) throw new Error(result.error || t('viewer.daemonNoMark'));
      onHighlightsChange(sortHighlights([...highlights, result.highlight]));
      props.notify(`${label} at ${formatDuration(time, true)}`, undefined, 'accent');
    } catch (err) {
      props.notify(t('viewer.errAddHighlight'), (err as Error).message, 'error');
    }
  }, [clip, highlights, onHighlightsChange, props]);

  // Escape shrinks before it closes. One press doing both would take away the
  // clip the user was watching when all they wanted was the window back.
  useEscape(open, useCallback(() => {
    if (expanded) {
      setExpanded(false);
      return;
    }
    onClose();
  }, [expanded, onClose]));

  useEffect(() => {
    if (closing) videoRef.current?.pause();
  }, [closing]);

  // Trim opens over a clip that may still be playing, and its own preview is a
  // second video of the same audio. Stop this one rather than layering them.
  useEffect(() => {
    if (trimOpen) videoRef.current?.pause();
  }, [trimOpen, clip]);

  const toggle = useCallback(() => {
    const video = videoRef.current;
    if (!video?.getAttribute('src')) return;
    if (video.paused) playQuietly(video);
    else video.pause();
  }, []);

  useEffect(() => {
    if (!open || closing || trimOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.altKey || e.metaKey) return;
      const target = e.target instanceof HTMLElement ? e.target : null;
      if (target?.closest('input, textarea, select') || target?.isContentEditable) return;
      if (e.code === 'Space' || e.key === ' ') {
        // Let a focused control keep its native keyboard activation.
        if (target?.closest('button, a, [role="button"], [role="switch"]')) return;
        e.preventDefault();
        if (!e.repeat) toggle();
        return;
      }
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        step(-1);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        step(1);
      } else if (e.key === 'h' || e.key === 'H') {
        e.preventDefault();
        void addHighlight();
      } else if (e.key === 'f' || e.key === 'F') {
        e.preventDefault();
        toggleExpanded();
      } else if (e.key === 's' || e.key === 'S') {
        e.preventDefault();
        void saveFrame();
      } else if (e.key === 'm' || e.key === 'M') {
        e.preventDefault();
        toggleMuted();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, closing, trimOpen, step, addHighlight, toggleExpanded, saveFrame, toggleMuted, toggle]);

  if (!mounted || !clip) return null;

  const duration = position.duration;
  const percent = duration > 0 ? (position.current / duration) * 100 : 0;

  const seekFromPointer = (clientX: number) => {
    const track = timelineRef.current;
    if (!track || !duration) return;
    const rect = track.getBoundingClientRect();
    seekTo(videoRef.current, clamp01((clientX - rect.left) / rect.width) * duration);
  };

  const beginHighlightDrag = (event: React.PointerEvent, highlight: Highlight) => {
    if (event.button !== 0 || !duration) return;
    event.preventDefault();
    event.stopPropagation();
    setDragging(highlight.id);

    const timeAt = (clientX: number) => {
      const rect = timelineRef.current?.getBoundingClientRect();
      if (!rect) return null;
      return clamp01((clientX - rect.left) / rect.width) * duration;
    };

    let latest = highlight.time;
    const move = (moveEvent: PointerEvent) => {
      const next = timeAt(moveEvent.clientX);
      if (next == null) return;
      latest = next;
      onHighlightsChange(sortHighlights(replaceTime(highlights, highlight.id, round3(next))));
    };
    const up = async (upEvent: PointerEvent) => {
      window.removeEventListener('pointermove', move, true);
      window.removeEventListener('pointerup', up, true);
      window.removeEventListener('pointercancel', up, true);
      setDragging(null);
      const applied = round3(timeAt(upEvent.clientX) ?? latest);
      onHighlightsChange(sortHighlights(replaceTime(highlights, highlight.id, applied)));
      try {
        const result = await api.updateHighlight(clip.slug, highlight.id, {time: applied});
        if (result.ok === false) throw new Error(result.error || t('viewer.daemonRejectedMove'));
        props.notify(`Highlight moved to ${formatDuration(applied, true)}`, undefined, 'accent');
      } catch (err) {
        // Put it back where it was. An optimistic move that silently did not
        // persist is worse than one that visibly snaps back.
        onHighlightsChange(sortHighlights(replaceTime(highlights, highlight.id, highlight.time)));
        props.notify(t('viewer.errMoveHighlight'), (err as Error).message, 'error');
      }
    };
    window.addEventListener('pointermove', move, true);
    window.addEventListener('pointerup', up, true);
    window.addEventListener('pointercancel', up, true);
  };

  const patchHighlight = async (id: string, body: Partial<Omit<Highlight, 'id'>>) => {
    try {
      const result = await api.updateHighlight(clip.slug, id, body);
      if (result.ok === false) throw new Error(result.error || t('viewer.daemonRejectedChange'));
      onHighlightsChange(
        sortHighlights(highlights.map(h => (h.id === id ? {...h, ...body} : h))),
      );
    } catch (err) {
      props.notify(t('viewer.errUpdateHighlight'), (err as Error).message, 'error');
    }
  };

  const removeHighlight = async (id: string) => {
    try {
      await api.deleteHighlight(clip.slug, id);
      onHighlightsChange(highlights.filter(h => h.id !== id));
    } catch (err) {
      props.notify(t('viewer.errDeleteHighlight'), (err as Error).message, 'error');
    }
  };

  const meta = [
    clip.duration ? formatDuration(Math.round(clip.duration), true) : '',
    clip.width ? `${clip.width}x${clip.height}` : '',
    clip.size ? formatBytes(clip.size) : '',
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div
      className="scrim viewer-scrim"
      data-closing={closing || undefined}
      onMouseDown={e => e.target === e.currentTarget && onClose()}>
      <div className="viewer-stack">
        <div
          className="modal viewer"
          data-closing={closing || undefined}
          role="dialog"
          aria-modal="true"
          aria-label={clipTitle(clip)}>
          <header className="viewer-head">
            <div className="viewer-heading">
              {renamingTitle ? (
                <InlineRename
                  className="viewer-rename"
                  label={t('card.nameLabel')}
                  initial={clipTitle(clip)}
                  onCancel={() => setRenamingTitle(false)}
                  onSubmit={name => {
                    setRenamingTitle(false);
                    props.onRename(clip, name);
                  }}
                />
              ) : (
                <h2
                  title={t('viewer.doubleClickRename')}
                  onDoubleClick={() => setRenamingTitle(true)}>
                  {clipTitle(clip)}
                </h2>
              )}
              <p className="mono">{meta}</p>
            </div>
            {clips.length > 1 ? (
              <span className="viewer-count mono">
                {index + 1} / {clips.length}
              </span>
            ) : null}
            <button
              type="button"
              className="viewer-nav"
              onClick={() => step(-1)}
              disabled={index <= 0}
              aria-label={t('viewer.prevClip')}>
              <Chevron dir="left" />
            </button>
            <button
              type="button"
              className="viewer-nav"
              onClick={() => step(1)}
              disabled={index < 0 || index >= clips.length - 1}
              aria-label={t('viewer.nextClip')}>
              <Chevron dir="right" />
            </button>
            <button
              type="button"
              className="viewer-nav"
              onClick={() => void saveFrame()}
              disabled={grabbing || clip.unreadable}
              title={t('viewer.saveFrameHint')}
              aria-label={t('viewer.saveFrame')}>
              <CameraGlyph />
            </button>
            <button
              type="button"
              className="viewer-nav"
              onClick={toggleExpanded}
              aria-pressed={expanded}
              title={expanded ? t('viewer.shrinkHint') : t('viewer.expandHint')}
              aria-label={expanded ? t('viewer.shrink') : t('viewer.expand')}>
              <IconExpand collapse={expanded} />
            </button>
            <button type="button" className="modal-close" onClick={onClose} aria-label={t('common.close')}>
              <IconClose size={15} />
            </button>
          </header>

          <div
            className="viewer-stage"
            ref={stageRef}
            data-expanded={expanded || undefined}
            data-idle={(expanded && idle) || undefined}
            onClick={toggle}
            onDoubleClick={toggleExpanded}>
            <video
              ref={videoRef}
              className="viewer-video"
              playsInline
              onTimeUpdate={e =>
                setPosition({
                  current: e.currentTarget.currentTime,
                  duration: Number.isFinite(e.currentTarget.duration)
                    ? e.currentTarget.duration
                    : 0,
                })
              }
              onLoadedMetadata={e =>
                setPosition({
                  current: e.currentTarget.currentTime,
                  duration: Number.isFinite(e.currentTarget.duration)
                    ? e.currentTarget.duration
                    : 0,
                })
              }
              onPlay={() => setPaused(false)}
              onPause={() => setPaused(true)}
              onCanPlay={() => setPreparing(false)}
              onLoadedData={() => setPreparing(false)}
              onError={() => setPreparing(false)}
            />
            <button
              type="button"
              className="viewer-play"
              data-paused={paused || undefined}
              aria-label={paused ? t('viewer.play') : t('viewer.pause')}>
              {paused ? <PlayGlyph /> : <PauseGlyph />}
            </button>
            <span className="viewer-timebadge mono">
              {formatDuration(position.current, true)} / {formatDuration(duration, true)}
            </span>

            {preparing ? (
              <div className="video-overlay" onClick={e => e.stopPropagation()}>
                <span className="video-spinner" aria-hidden="true" />
                <p>{t('viewer.preparingPreview')}</p>
              </div>
            ) : null}

            {/* The modal's own timeline and the player bar are both scrolled
                away while expanded, so the stage carries its own transport. */}
            {expanded ? (
              <div
                className="viewer-expanded-bar"
                onClick={e => e.stopPropagation()}
                onDoubleClick={e => e.stopPropagation()}>
                <span className="mono player-tc">{formatDuration(position.current, true)}</span>
                <Scrubber
                  percent={percent}
                  onSeek={ratio => seekTo(videoRef.current, ratio * duration)}
                />
                <span className="mono player-tc">{formatDuration(duration, true)}</span>
                <VolumeControl
                  volume={volume}
                  muted={muted}
                  onChange={applyVolume}
                  onToggleMute={toggleMuted}
                />
                <button
                  type="button"
                  className="player-btn"
                  onClick={toggleExpanded}
                  aria-label={t('viewer.shrink')}>
                  <IconExpand collapse />
                </button>
              </div>
            ) : null}

            {keyIntervals && keysShown && !failed ? (
              <KeyOverlay video={videoRef} intervals={keyIntervals} layout={keyLayout} />
            ) : null}

            {failed ? (
              <div className="video-overlay" onClick={e => e.stopPropagation()}>
                <p>{videoFailureMessage()}</p>
                <div className="video-overlay-actions">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => props.onOpenExternally(clip)}>
                    {t('viewer.openInPlayer')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-quiet"
                    onClick={() => props.onReveal(clip)}>
                    {t('viewer.showInFolder')}
                  </button>
                </div>
              </div>
            ) : null}
          </div>

          <div className="viewer-bottom">
            <div
              className="viewer-timeline"
              ref={timelineRef}
              onClick={e => seekFromPointer(e.clientX)}>
              <div className="viewer-progress" style={{width: `${percent}%`}} />
              <div className="viewer-playhead" style={{left: `${percent}%`}} />
              {highlights.map(h => (
                <button
                  key={h.id}
                  type="button"
                  className="hl-marker"
                  data-dragging={dragging === h.id || undefined}
                  style={{
                    left: `${duration ? (h.time / duration) * 100 : 0}%`,
                    background: h.color || DEFAULT_HIGHLIGHT_COLOR,
                  }}
                  title={`${h.label}, ${formatDuration(h.time, true)}, drag to move`}
                  aria-label={t('viewer.highlightAt', {label: h.label, time: formatDuration(h.time, true)})}
                  onPointerDown={e => beginHighlightDrag(e, h)}
                  onClick={e => {
                    e.stopPropagation();
                    if (!dragging) seekTo(videoRef.current, h.time);
                  }}
                />
              ))}
            </div>

            <div className="viewer-hl-head">
              <span className="eyebrow">{t('viewer.highlightsHeading')}</span>
              <button type="button" className="btn btn-quiet btn-sm" onClick={() => void addHighlight()}>
                {tNode('viewer.addHighlightKey', {key: <kbd key="k">H</kbd>})}
              </button>
            </div>

            <div className="viewer-hl-list">
              {highlights.length === 0 ? (
                <p className="viewer-hl-empty">
                  {tNode('viewer.noHighlightsHelp', {key: <kbd key="k">H</kbd>})}
                </p>
              ) : (
                highlights.map(h => (
                  <div className="hl-item" key={h.id} onClick={() => seekTo(videoRef.current, h.time)}>
                    <button
                      type="button"
                      className="hl-swatch"
                      style={{background: h.color || DEFAULT_HIGHLIGHT_COLOR}}
                      title={t('viewer.changeColour')}
                      aria-label={t('viewer.changeColourAria', {name: h.label})}
                      onClick={e => {
                        e.stopPropagation();
                        const rect = e.currentTarget.getBoundingClientRect();
                        setPicker({
                          id: h.id,
                          color: h.color || DEFAULT_HIGHLIGHT_COLOR,
                          at: {x: rect.left, y: rect.bottom + 6},
                        });
                      }}
                    />
                    <span className="hl-time mono">{formatDuration(h.time, true)}</span>
                    {renaming === h.id ? (
                      <InlineRename
                        className="hl-rename"
                        label={t('viewer.highlightLabel')}
                        initial={h.label}
                        emptyFallback={t('viewer.highlight')}
                        stopPropagation
                        onCancel={() => setRenaming(null)}
                        onSubmit={label => {
                          setRenaming(null);
                          void patchHighlight(h.id, {label});
                        }}
                      />
                    ) : (
                      <span
                        className="hl-label"
                        title={t('viewer.doubleClickRename')}
                        onDoubleClick={e => {
                          e.stopPropagation();
                          setRenaming(h.id);
                        }}>
                        {h.label}
                      </span>
                    )}
                    <button
                      type="button"
                      className="hl-del"
                      title={t('viewer.removeHighlight')}
                      aria-label={t('viewer.removeHighlightAria', {name: h.label})}
                      onClick={e => {
                        e.stopPropagation();
                        void removeHighlight(h.id);
                      }}>
                      <IconClose size={12} />
                    </button>
                  </div>
                ))
              )}
            </div>

            <footer className="viewer-foot">
              <span className="viewer-shortcuts mono">
                {t('viewer.shortcuts')}
              </span>
              <div className="viewer-foot-btns">
                {keyIntervals ? (
                  <button
                    type="button"
                    className={keysShown ? 'btn btn-sm' : 'btn btn-quiet btn-sm'}
                    aria-pressed={keysShown}
                    onClick={() => {
                      setKeysShown(!keysShown);
                      remember(KEYS_SHOWN_KEY, keysShown ? '0' : '1');
                    }}>
                    {t('viewer.keys')}
                  </button>
                ) : null}
                <button type="button" className="btn btn-quiet btn-sm" onClick={() => props.onTrim(clip)}>
                  {t('viewer.trim')}
                </button>
                <button
                  type="button"
                  className="btn btn-quiet btn-sm"
                  title={props.shareLabel}
                  onClick={() => props.onShare(clip)}
                  onContextMenu={e => {
                    e.preventDefault();
                    props.onShareMenu(clip);
                  }}>
                  {t('viewer.share')}
                </button>
                <button type="button" className="btn btn-quiet btn-sm" onClick={() => props.onReveal(clip)}>
                  {t('viewer.reveal')}
                </button>
                <button
                  type="button"
                  className="btn btn-quiet btn-danger btn-sm"
                  onClick={() => props.onDelete(clip)}>
                  {t('viewer.delete')}
                </button>
              </div>
            </footer>
          </div>
        </div>

        <PlayerBar
          clip={clip}
          paused={paused}
          current={position.current}
          duration={duration}
          volume={volume}
          muted={muted}
          canStepBack={index > 0}
          canStepForward={index >= 0 && index < clips.length - 1}
          onToggle={toggle}
          onStep={step}
          onVolume={applyVolume}
          onToggleMute={toggleMuted}
          onShare={() => props.onShare(clip)}
          onShareMenu={() => props.onShareMenu(clip)}
          shareLabel={props.shareLabel}
          onClose={onClose}
        />
      </div>

      {picker ? (
        <ColorPicker
          at={picker.at}
          current={picker.color}
          onClose={() => setPicker(null)}
          onPick={color => {
            setPicker(null);
            void patchHighlight(picker.id, {color});
          }}
        />
      ) : null}
    </div>
  );
}

/**
 * The bar under the viewer.
 *
 * It used to carry a second scrub track driven by the same playhead as the
 * modal's own timeline, which meant two playheads for one video and no volume
 * control anywhere. The timeline above owns seeking, because it is the one
 * with the highlight markers on it, so the space here went to the audio (#174).
 */
function PlayerBar({
  clip,
  paused,
  current,
  duration,
  volume,
  muted,
  canStepBack,
  canStepForward,
  onToggle,
  onStep,
  onVolume,
  onToggleMute,
  onShare,
  onShareMenu,
  shareLabel,
  onClose,
}: {
  clip: Clip;
  paused: boolean;
  current: number;
  duration: number;
  volume: number;
  muted: boolean;
  canStepBack: boolean;
  canStepForward: boolean;
  onToggle: () => void;
  onStep: (delta: number) => void;
  onVolume: (next: number) => void;
  onToggleMute: () => void;
  onShare: () => void;
  onShareMenu: () => void;
  shareLabel: string;
  onClose: () => void;
}) {
  return (
    <div className="player-bar">
      <div className="player-clip">
        {clip.thumb_url ? <img src={clip.thumb_url} alt="" /> : <span className="player-thumb-empty" />}
        <div className="player-titles">
          <b>{clipTitle(clip)}</b>
          <span className="mono">{clip.game || ''}</span>
        </div>
      </div>

      <div className="player-controls">
        <button
          type="button"
          className="player-btn"
          onClick={() => onStep(-1)}
          disabled={!canStepBack}
          aria-label={t('viewer.prevClip')}>
          <StepGlyph dir="back" />
        </button>
        <button type="button" className="player-main" onClick={onToggle} aria-label={paused ? t('viewer.play') : t('viewer.pause')}>
          {paused ? <PlayGlyph /> : <PauseGlyph />}
        </button>
        <button
          type="button"
          className="player-btn"
          onClick={() => onStep(1)}
          disabled={!canStepForward}
          aria-label={t('viewer.nextClip')}>
          <StepGlyph dir="forward" />
        </button>
      </div>

      <div className="player-audio">
        <span className="mono player-tc">
          {formatDuration(current, true)} / {formatDuration(duration, true)}
        </span>
        <VolumeControl
          volume={volume}
          muted={muted}
          onChange={onVolume}
          onToggleMute={onToggleMute}
        />
      </div>

      <div className="player-extra">
        <button
          type="button"
          className="player-btn"
          onClick={onShare}
          onContextMenu={e => {
            e.preventDefault();
            onShareMenu();
          }}
          title={shareLabel}
          aria-label={shareLabel}>
          <ShareGlyph />
        </button>
        <button type="button" className="player-btn" onClick={onClose} aria-label={t('viewer.closePlayer')}>
          <IconClose size={15} />
        </button>
      </div>
    </div>
  );
}

/** A seekable track. Click anywhere, or press and drag along it. */
function Scrubber({percent, onSeek}: {percent: number; onSeek: (ratio: number) => void}) {
  const trackRef = useRef<HTMLDivElement>(null);

  const ratioAt = (clientX: number): number | null => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect || !rect.width) return null;
    return clamp01((clientX - rect.left) / rect.width);
  };

  const begin = (event: React.PointerEvent) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const first = ratioAt(event.clientX);
    if (first !== null) onSeek(first);

    const move = (moveEvent: PointerEvent) => {
      const next = ratioAt(moveEvent.clientX);
      if (next !== null) onSeek(next);
    };
    const up = () => {
      window.removeEventListener('pointermove', move, true);
      window.removeEventListener('pointerup', up, true);
      window.removeEventListener('pointercancel', up, true);
    };
    window.addEventListener('pointermove', move, true);
    window.addEventListener('pointerup', up, true);
    window.addEventListener('pointercancel', up, true);
  };

  return (
    <div className="player-track" ref={trackRef} onPointerDown={begin}>
      <div className="player-fill" style={{width: `${percent}%`}} />
      <div className="player-knob" style={{left: `${percent}%`}} />
    </div>
  );
}

/**
 * Speaker plus level.
 *
 * The track is a native range input: arrows, Home, End and a screen reader all
 * come from the platform rather than from a hand-rolled div with a role on it.
 * That approach and the stylesheet behind it are voltek's, from #179.
 *
 * Mute is kept as its own flag rather than as a level of zero, so muting does
 * not throw away the level the user chose. Storing zero is what made unmuting
 * after a restart jump back to full volume.
 */
function VolumeControl({
  volume,
  muted,
  onChange,
  onToggleMute,
}: {
  volume: number;
  muted: boolean;
  onChange: (next: number) => void;
  onToggleMute: () => void;
}) {
  // Muted shows an empty track, so the control always agrees with what is
  // coming out of the speakers rather than with the number behind it.
  const shown = muted ? 0 : volume;

  return (
    <div className="player-volume">
      <button
        type="button"
        className="player-btn"
        onClick={onToggleMute}
        aria-pressed={muted}
        title={muted ? t('viewer.unmuteHint') : t('viewer.muteHint')}
        aria-label={muted ? t('viewer.unmute') : t('viewer.mute')}>
        <SpeakerGlyph level={shown} />
      </button>
      <input
        type="range"
        className="player-volume-range"
        min={0}
        max={1}
        step={0.01}
        value={shown}
        aria-label={t('viewer.volume')}
        style={{['--filled' as string]: `${shown * 100}%`}}
        onChange={e => onChange(Number(e.target.value))}
      />
    </div>
  );
}

function ColorPicker({
  at,
  current,
  onPick,
  onClose,
}: {
  at: {x: number; y: number};
  current: string;
  onPick: (color: string) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const dismiss = (e: Event) => {
      if (ref.current?.contains(e.target as Node)) return;
      onClose();
    };
    // Deferred a frame: the click that opened this popup is still travelling.
    const id = window.setTimeout(() => document.addEventListener('pointerdown', dismiss, true), 0);
    return () => {
      window.clearTimeout(id);
      document.removeEventListener('pointerdown', dismiss, true);
    };
  }, [onClose]);

  return (
    <div className="hl-picker" ref={ref} style={{left: at.x, top: at.y}} role="menu">
      {HIGHLIGHT_COLORS.map(color => (
        <button
          key={color}
          type="button"
          className="hl-picker-dot"
          data-active={color === current || undefined}
          style={{background: color}}
          title={color}
          aria-label={t('viewer.useColour', {color})}
          onClick={() => onPick(color)}
        />
      ))}
    </div>
  );
}

const clamp01 = (n: number) => Math.max(0, Math.min(1, n));
const round3 = (n: number) => Number(n.toFixed(3));
const sortHighlights = (list: Highlight[]) => [...list].sort((a, b) => a.time - b.time);
const replaceTime = (list: Highlight[], id: string, time: number) =>
  list.map(h => (h.id === id ? {...h, time} : h));

/** "Highlight", then "Highlight 1", and so on, skipping names already used. */
function nextHighlightLabel(highlights: Highlight[]): string {
  // The label is stored with the clip, so this is a default name the user can
  // overwrite, like "Untitled" in an editor. It is translated for the same
  // reason. Labels already on disk keep whatever they were saved as.
  const base = t('viewer.highlight');
  const used = new Set(highlights.map(h => h.label));
  if (!used.has(base)) return base;
  let n = 1;
  while (used.has(t('viewer.highlightNumbered', {base, n}))) n++;
  return t('viewer.highlightNumbered', {base, n});
}

const stroke = {
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2.2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
};

const Chevron = ({dir}: {dir: 'left' | 'right'}) => (
  <svg {...stroke} width={16} height={16}>
    <path d={dir === 'left' ? 'M15 6 9 12l6 6' : 'M9 6l6 6-6 6'} />
  </svg>
);
const PlayGlyph = () => (
  <svg viewBox="0 0 24 24" width={16} height={16} fill="currentColor" aria-hidden="true">
    <path d="M7 4l13 8-13 8z" />
  </svg>
);
const PauseGlyph = () => (
  <svg viewBox="0 0 24 24" width={16} height={16} fill="currentColor" aria-hidden="true">
    <path d="M7 4h3.5v16H7zM13.5 4H17v16h-3.5z" />
  </svg>
);
const StepGlyph = ({dir}: {dir: 'back' | 'forward'}) => (
  <svg viewBox="0 0 24 24" width={13} height={13} fill="currentColor" aria-hidden="true">
    <path d={dir === 'back' ? 'M6 4h2v16H6zM20 4 9.5 12 20 20z' : 'M18 4h-2v16h2zM4 4l10.5 8L4 20z'} />
  </svg>
);
const CameraGlyph = () => (
  <svg {...stroke} width={15} height={15} strokeWidth={2}>
    <path d="M3 8.5A1.5 1.5 0 0 1 4.5 7h2L8 4.5h8L17.5 7h2A1.5 1.5 0 0 1 21 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z" />
    <circle cx="12" cy="13" r="3.4" />
  </svg>
);
/* Three states rather than two: at a glance the difference between quiet and
   muted matters more than the exact number on the slider. */
const SpeakerGlyph = ({level}: {level: number}) => (
  <svg {...stroke} width={15} height={15} strokeWidth={2}>
    <path d="M4 9.5h3L11 6v12L7 14.5H4z" />
    {level <= 0 ? (
      <path d="m15 9.5 4.5 5M19.5 9.5l-4.5 5" />
    ) : level < 0.55 ? (
      <path d="M14.8 9.8a3 3 0 0 1 0 4.4" />
    ) : (
      <path d="M14.8 9.8a3 3 0 0 1 0 4.4M17.3 7.6a6.5 6.5 0 0 1 0 8.8" />
    )}
  </svg>
);
const ShareGlyph = () => (
  <svg {...stroke} width={15} height={15} strokeWidth={2}>
    <circle cx="18" cy="5" r="3" />
    <circle cx="6" cy="12" r="3" />
    <circle cx="18" cy="19" r="3" />
    <path d="m8.6 13.5 6.8 4M15.4 6.5l-6.8 4" />
  </svg>
);
