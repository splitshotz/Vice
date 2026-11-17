import {useEffect, useRef, useState} from 'react';

import {api} from '../lib/api';
import {formatBytes, formatDuration} from '../lib/format';
import {clipTitle, type Clip} from '../lib/types';
import {Modal} from './Modal';
import {t} from '../lib/i18n';

interface DiscordCopy {
  path: string;
  filename: string;
  size: number;
}

/** file:// URI for an absolute path, with every segment escaped. */
function fileUri(path: string): string {
  return `file://${path.split('/').map(encodeURIComponent).join('/')}`;
}

/**
 * The share sheet, over the viewer.
 *
 * Two ways out of Vice, in the order people reach for them: the share link for
 * anywhere a URL goes, and the clip itself for Discord, which will not unfurl
 * a link to a machine it cannot reach.
 */
export function ShareModal({
  clip,
  onClose,
  onCopyLink,
  onCopyFile,
  onNewLink,
}: {
  clip: Clip | null;
  onClose: () => void;
  onCopyLink: (clip: Clip) => void;
  /** Puts the Discord-sized file on the clipboard, for where a drag does not land (#289). */
  onCopyFile: (clip: Clip) => void;
  /** Replaces the share link, which stops the old one working (#213). */
  onNewLink: (clip: Clip) => void;
}) {
  // Taking a link back cannot be undone, so the button asks once.
  const [confirmNew, setConfirmNew] = useState(false);
  const [copy, setCopy] = useState<DiscordCopy | null>(null);
  const [failed, setFailed] = useState('');
  const slug = clip?.slug;

  // The file has to exist on disk before the drag starts: a dragstart handler
  // cannot wait for an encode, so it is built when the sheet opens.
  useEffect(() => {
    if (!slug) return;
    let cancelled = false;
    setConfirmNew(false);
    setCopy(null);
    setFailed('');
    void api
      .discordCopy(slug)
      .then(result => {
        if (cancelled) return;
        if (result.ok === false) throw new Error(result.error || t('viewer.shareDragFailed'));
        setCopy(result);
      })
      .catch((err: Error) => !cancelled && setFailed(err.message));
    return () => {
      cancelled = true;
    };
  }, [slug]);

  // Held through the closing animation, or the sheet would empty itself out
  // one frame before it fades.
  const last = useRef<Clip | null>(null);
  if (clip) last.current = clip;
  const shown = clip ?? last.current;
  if (!shown) return null;

  const link = shown.share_url;

  const onDragStart = (e: React.DragEvent) => {
    if (!copy) {
      e.preventDefault();
      return;
    }
    e.dataTransfer.effectAllowed = 'copy';
    // A file:// uri-list is what a file manager offers and what an app off
    // the side of the window (Discord) turns back into a dropped file.
    // Chromium's own DownloadURL never reaches the desktop from here: the
    // native window is QtWebEngine, which does not implement it.
    e.dataTransfer.setData('text/uri-list', fileUri(copy.path));
    // Read only by targets that take text, where the link is the useful
    // thing to land in a message box.
    e.dataTransfer.setData('text/plain', link || fileUri(copy.path));
  };

  return (
    <Modal open={clip !== null} title={t('viewer.shareTitle')} onClose={onClose}>
      <div className="share-sheet">
        <div className="share-link-row">
          <div className="share-link-head">
            <span className="share-label">{t('viewer.shareHighQuality')}</span>
            {link ? (
              <button
                type="button"
                className="btn btn-quiet btn-sm"
                onClick={() => {
                  if (!confirmNew) return setConfirmNew(true);
                  setConfirmNew(false);
                  onNewLink(shown);
                }}
                onBlur={() => setConfirmNew(false)}>
                {confirmNew ? t('viewer.shareNewLinkConfirm') : t('viewer.shareNewLink')}
              </button>
            ) : null}
          </div>
          {link ? (
            <button
              type="button"
              className="share-link mono"
              title={t('viewer.shareCopyHint')}
              onClick={() => onCopyLink(shown)}>
              {link}
            </button>
          ) : (
            <p className="share-nolink">{t('viewer.shareNoLink')}</p>
          )}
        </div>

        <div
          className="share-drag"
          data-ready={copy ? true : undefined}
          draggable={Boolean(copy)}
          onDragStart={onDragStart}>
          {shown.thumb_url ? (
            <img src={shown.thumb_url} alt={clipTitle(shown)} draggable={false} />
          ) : (
            <span className="share-drag-empty" aria-hidden="true" />
          )}
          {shown.duration ? (
            <span className="share-drag-time mono">{formatDuration(shown.duration)}</span>
          ) : null}
          {copy ? null : <span className="share-drag-veil">{t('viewer.sharePreparing')}</span>}
        </div>

        <div className="share-drag-copy">
          <b>{t('viewer.shareDragHeading')}</b>
          <span>
            {failed
              ? failed
              : copy
                ? `${t('viewer.shareDragHint')} · ${t('viewer.shareDiscordSize', {
                    size: formatBytes(copy.size),
                  })}`
                : t('viewer.sharePreparing')}
          </span>
        </div>

        <button
          type="button"
          className="btn btn-sm share-copy-file"
          disabled={!copy}
          onClick={() => onCopyFile(shown)}>
          {t('viewer.shareCopyFile')}
        </button>
      </div>
    </Modal>
  );
}
