import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import {api} from '../lib/api';
import {copyShareLink, shareButtonLabel, shareClip as runShare} from '../lib/share';
import {clipTitle, type Clip, type Highlight} from '../lib/types';
import {Modal} from '../components/Modal';
import {ShareModal} from '../components/ShareModal';
import {TrimModal} from '../components/TrimModal';
import {Viewer} from '../components/Viewer';
import {useRenameClip} from './clipActions';
import {useStore} from './store';
import {t} from '../lib/i18n';

interface Playback {
  openViewer: (slug: string) => void;
  openTrim: (slug: string) => void;
  openShare: (slug: string) => void;
}

const PlaybackContext = createContext<Playback | null>(null);

/**
 * Owns the viewer and the trim modal, so any screen can open a clip without
 * carrying the playback element around with it. Both surfaces live here rather
 * than inside a screen because the viewer has to survive a change of view.
 */
export function PlaybackProvider({children}: {children: ReactNode}) {
  const {state, notify, refreshClips} = useStore();
  const {clips} = state;

  const [viewerSlug, setViewerSlug] = useState<string | null>(null);
  const [trimSlug, setTrimSlug] = useState<string | null>(null);
  // Clips with a trim running on the daemon. Kept here, not in the trim
  // window, so closing and reopening it cannot forget one is still going (#281).
  const [trimming, setTrimming] = useState<ReadonlySet<string>>(() => new Set());
  const setTrimBusy = useCallback((slug: string, busy: boolean) => {
    setTrimming(current => {
      if (current.has(slug) === busy) return current;
      const next = new Set(current);
      if (busy) next.add(slug);
      else next.delete(slug);
      return next;
    });
  }, []);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [confirmDelete, setConfirmDelete] = useState<Clip | null>(null);
  const [manualCopy, setManualCopy] = useState<string | null>(null);
  const [shareSlug, setShareSlug] = useState<string | null>(null);

  const viewerClip = clips.find(c => c.slug === viewerSlug) ?? null;
  const trimClip = clips.find(c => c.slug === trimSlug) ?? null;
  const shareClip = clips.find(c => c.slug === shareSlug) ?? null;

  const openViewer = useCallback((slug: string) => {
    // Card previews keep decoding behind the scrim otherwise, and they are
    // muted, so nothing on screen would explain the extra load.
    document.querySelectorAll<HTMLVideoElement>('video.clip-preview').forEach(v => v.pause());
    setViewerSlug(slug);
  }, []);

  const openTrim = useCallback((slug: string) => setTrimSlug(slug), []);

  // Highlights belong to the clip on screen, so they reload whenever it does.
  useEffect(() => {
    if (!viewerSlug) {
      setHighlights([]);
      return;
    }
    let cancelled = false;
    void api
      .highlights(viewerSlug)
      .then(list => !cancelled && setHighlights(list))
      .catch(err => {
        console.debug('Loading highlights failed', err);
        if (!cancelled) setHighlights([]);
      });
    return () => {
      cancelled = true;
    };
  }, [viewerSlug]);

  // A rename in flight. The daemon broadcasts clip_deleted for the old slug
  // the moment the file moves, which reaches the effect below well before the
  // response carrying the new slug gets back here. Without this the viewer
  // closes on the user mid-rename and there is nothing left to redirect (#170).
  const renamingFrom = useRef<string | null>(null);

  // A clip deleted underneath the viewer, from anywhere, closes it.
  useEffect(() => {
    const gone = (slug: string) => slug !== renamingFrom.current && !clips.some(c => c.slug === slug);
    if (viewerSlug && gone(viewerSlug)) setViewerSlug(null);
    if (trimSlug && gone(trimSlug)) setTrimSlug(null);
    if (shareSlug && gone(shareSlug)) setShareSlug(null);
  }, [clips, viewerSlug, trimSlug, shareSlug]);

  const renameClip = useRenameClip();
  const rename = useCallback(
    (clip: Clip, name: string) => {
      renamingFrom.current = clip.slug;
      void renameClip(clip, name)
        .then(updated => {
          if (!updated?.slug) return;
          setViewerSlug(current => (current === clip.slug ? updated.slug : current));
          setTrimSlug(current => (current === clip.slug ? updated.slug : current));
          setShareSlug(current => (current === clip.slug ? updated.slug : current));
        })
        .finally(() => {
          renamingFrom.current = null;
        });
    },
    [renameClip],
  );

  const fail = useCallback(
    (title: string) => (err: Error) =>
      notify({kind: 'error', title, detail: err.message, tone: 'error', holdMs: 7000}),
    [notify],
  );

  const say = useCallback(
    (title: string, detail?: string, tone: 'accent' | 'error' = 'accent') =>
      notify({
        kind: tone === 'error' ? 'error' : 'info',
        title,
        detail,
        tone,
        holdMs: tone === 'error' ? 7000 : 3500,
      }),
    [notify],
  );

  const reveal = useCallback(
    (clip: Clip) => void api.revealClip(clip.slug).catch(fail(t('viewer.errReveal'))),
    [fail],
  );

  const openExternally = useCallback(
    (clip: Clip) => void api.openClip(clip.slug).catch(fail(t('viewer.errSystemPlayer'))),
    [fail],
  );

  const discordFile = Boolean(state.config?.sharing?.share_discord_file);
  const share = useCallback(
    (clip: Clip) => void runShare(clip, discordFile, notify, setManualCopy),
    [discordFile, notify],
  );
  const shareMenu = useCallback((clip: Clip) => setShareSlug(clip.slug), []);

  const copyLink = useCallback(
    (clip: Clip) => void copyShareLink(clip, notify, setManualCopy),
    [notify],
  );

  const value = useMemo<Playback>(
    () => ({openViewer, openTrim, openShare: setShareSlug}),
    [openViewer, openTrim],
  );

  return (
    <PlaybackContext.Provider value={value}>
      {children}

      <Viewer
        clip={viewerClip}
        clips={clips}
        highlights={highlights}
        onHighlightsChange={setHighlights}
        onSelect={setViewerSlug}
        onClose={() => setViewerSlug(null)}
        onTrim={clip => setTrimSlug(clip.slug)}
        trimOpen={trimSlug !== null}
        onRename={rename}
        onShare={share}
        onShareMenu={shareMenu}
        shareLabel={shareButtonLabel(discordFile)}
        onReveal={reveal}
        onOpenExternally={openExternally}
        onDelete={setConfirmDelete}
        notify={say}
      />

      <ShareModal clip={shareClip} onClose={() => setShareSlug(null)} onCopyLink={copyLink} />

      <TrimModal
        clip={trimClip}
        highlights={trimSlug && trimSlug === viewerSlug ? highlights : []}
        busy={trimClip ? trimming.has(trimClip.slug) : false}
        onBusy={setTrimBusy}
        onClose={() => setTrimSlug(null)}
        onSaved={refreshClips}
        notify={say}
        onRename={rename}
        onReveal={reveal}
        onOpenExternally={openExternally}
      />

      <Modal
        open={confirmDelete !== null}
        title={t('viewer.confirmDeleteTitle')}
        onClose={() => setConfirmDelete(null)}
        footer={
          <>
            <button type="button" className="btn btn-quiet" onClick={() => setConfirmDelete(null)}>
              {t('common.keepIt')}
            </button>
            <button
              type="button"
              className="btn btn-danger-solid"
              onClick={() => {
                const clip = confirmDelete;
                setConfirmDelete(null);
                if (!clip) return;
                setViewerSlug(null);
                void api
                  .deleteClip(clip.slug)
                  .then(() => say(t('viewer.clipDeleted')))
                  .catch(fail(t('viewer.errDelete')));
              }}>
              {t('common.delete')}
            </button>
          </>
        }>
        <p>
          {t('viewer.confirmDeleteBody', {
            name: confirmDelete ? clipTitle(confirmDelete) : '',
          })}
        </p>
      </Modal>

      <Modal open={manualCopy !== null} title={t('viewer.copyLinkTitle')} onClose={() => setManualCopy(null)}>
        <p>{t('viewer.copyLinkBody')}</p>
        <textarea className="manual-copy" readOnly value={manualCopy ?? ''} rows={3} />
      </Modal>
    </PlaybackContext.Provider>
  );
}

export function usePlayback(): Playback {
  const playback = useContext(PlaybackContext);
  if (!playback) throw new Error('usePlayback was called outside the provider');
  return playback;
}
