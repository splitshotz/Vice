import {useCallback, useMemo, useState, type ReactNode} from 'react';

import {api} from '../lib/api';
import {copyShareLink, shareButtonLabel, shareClip} from '../lib/share';
import {clipTitle, type Clip} from '../lib/types';
import type {ClipActions} from '../components/ClipCard';
import {ContextMenu} from '../components/ContextMenu';
import {GameTagModal} from '../components/GameTagModal';
import {Modal} from '../components/Modal';
import {useStore} from './store';
import {usePlayback} from './playback';
import {usePlaylistToggleItems} from './playlistItems';
import {isCloudOnly, useCloudActions} from './cloudActions';
import {t} from '../lib/i18n';

/**
 * Everything a clip card can do, in one place.
 *
 * Home and All Clips render the same card and are expected to behave the
 * same. Building the handler set on each screen is what let them drift the
 * first time, with Home ending up as a card that could only be opened.
 */
/**
 * Rename a clip, wherever the gesture came from.
 *
 * Shared because renaming is reachable from the card, the viewer and the trim
 * window, and the "saved as" notice below is the part that would have been
 * dropped from the copies.
 */
export function useRenameClip(): (clip: Clip, name: string) => Promise<Clip | null> {
  const {notify, refreshClips} = useStore();
  return useCallback(
    async (clip: Clip, name: string) => {
      try {
        const updated = await api.renameClip(clip.slug, name);
        await refreshClips();
        if (updated?.name && clipTitle(updated) !== name) {
          // Punctuation is normalised server side, so say what landed on disk.
          notify({
            kind: 'info',
            title: t('card.savedAs', {name: clipTitle(updated)}),
            tone: 'neutral',
            holdMs: 4000,
          });
        }
        return updated ?? null;
      } catch (err) {
        notify({
          kind: 'error',
          title: t('card.renameFailed'),
          detail: (err as Error).message,
          tone: 'error',
          holdMs: 7000,
        });
        return null;
      }
    },
    [notify, refreshClips],
  );
}

export function useClipActions(): {actions: ClipActions; overlays: ReactNode} {
  const {state, notify} = useStore();
  const {openViewer, openTrim, openShare} = usePlayback();
  const {playlists} = state;
  const playlistItems = usePlaylistToggleItems();

  const [menu, setMenu] = useState<{clip: Clip; at: {x: number; y: number}} | null>(null);
  const [confirmDelete, setConfirmDeleteRaw] = useState<Clip | null>(null);
  // Off for a clip that is also here: the server copy is the backup. On for
  // a cloud clip, which would otherwise linger on the server, unlisted.
  const [alsoRemote, setAlsoRemote] = useState(false);
  const setConfirmDelete = useCallback((clip: Clip | null) => {
    setAlsoRemote(clip ? isCloudOnly(clip) : false);
    setConfirmDeleteRaw(clip);
  }, []);
  const cloud = useCloudActions();
  const [manualCopy, setManualCopy] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [tagging, setTagging] = useState<Clip | null>(null);

  const fail = useCallback(
    (title: string) => (err: Error) =>
      notify({kind: 'error', title, detail: err.message, tone: 'error', holdMs: 7000}),
    [notify],
  );

  const say = useCallback(
    (title: string, detail?: string) =>
      notify({kind: 'info', title, detail, tone: 'accent', holdMs: 3500}),
    [notify],
  );

  const copyLink = useCallback(
    (clip: Clip) => {
      const currentClip = state.clips.find(candidate => candidate.slug === clip.slug) ?? clip;
      void copyShareLink(currentClip, notify, setManualCopy, state.uploadServers);
    },
    [notify, state.clips, state.uploadServers],
  );

  const discordFile = Boolean(state.config?.sharing?.share_discord_file);
  const share = useCallback(
    (clip: Clip) => {
      const currentClip = state.clips.find(candidate => candidate.slug === clip.slug) ?? clip;
      void shareClip(currentClip, discordFile, notify, setManualCopy, state.uploadServers);
    },
    [discordFile, notify, state.clips, state.uploadServers],
  );

  const reveal = useCallback(
    (clip: Clip) => void api.revealClip(clip.slug).catch(fail(t('viewer.errReveal'))),
    [fail],
  );

  const copyFile = useCallback(
    (clip: Clip) =>
      void api
        .copyClipFile(clip.slug)
        .then(() => say(t('card.videoCopied')))
        .catch(fail(t('card.errCopyVideo'))),
    [fail, say],
  );

  const renameClip = useRenameClip();
  const rename = useCallback(
    async (clip: Clip, name: string) => {
      await renameClip(clip, name);
    },
    [renameClip],
  );

  const actions = useMemo<ClipActions>(
    () => ({
      onOpen: clip => openViewer(clip.slug),
      onTrim: clip => openTrim(clip.slug),
      onShare: share,
      onShareMenu: clip => openShare(clip.slug),
      shareLabel: shareButtonLabel(discordFile),
      onCopyFile: copyFile,
      onReveal: reveal,
      onDelete: setConfirmDelete,
      onRename: rename,
      onContextMenu: (clip, at) => setMenu({clip, at}),
      renamingSlug: renaming,
      onRenameDone: () => setRenaming(null),
    }),
    [openViewer, openTrim, openShare, share, discordFile, copyFile, reveal, rename, renaming, setConfirmDelete],
  );

  const menuClip = menu?.clip;
  const overlays = (
    <>
      {menu && menuClip ? (
        <ContextMenu
          at={menu.at}
          heading={clipTitle(menuClip)}
          emptyLabel={t('common.noActions')}
          onClose={() => setMenu(null)}
          items={[
            {id: 'open', label: t('card.open'), onSelect: () => openViewer(menuClip.slug)},
            // A cloud clip has no file here to cut or rename.
            {
              id: 'trim',
              label: t('card.trim'),
              disabled: isCloudOnly(menuClip),
              onSelect: () => openTrim(menuClip.slug),
            },
            {
              id: 'rename',
              label: t('card.rename'),
              disabled: isCloudOnly(menuClip),
              onSelect: () => setRenaming(menuClip.slug),
            },
            {id: 'game', label: t('card.setGame'), onSelect: () => setTagging(menuClip)},
            {
              id: 'copy-link',
              label: menuClip.share_url ? t('card.copyShareLink') : t('card.noShareLink'),
              disabled: !menuClip.share_url,
              onSelect: () => copyLink(menuClip),
            },
            ...(isCloudOnly(menuClip)
              ? []
              : [
                  {id: 'copy-file', label: t('card.copyVideoShort'), onSelect: () => copyFile(menuClip)},
                  {id: 'reveal', label: t('card.reveal'), onSelect: () => reveal(menuClip)},
                ]),
            {id: 'sep-cloud', separator: true},
            ...cloud.menuItems(menuClip),
            ...(playlists.length ? [{id: 'sep-playlists', separator: true} as const] : []),
            ...playlistItems(menuClip),
            {id: 'sep-delete', separator: true},
            {
              id: 'delete',
              label: t('card.deleteClip'),
              danger: true,
              onSelect: () => setConfirmDelete(menuClip),
            },
          ]}
        />
      ) : null}

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
                void api
                  .deleteClip(clip.slug, {remote: Boolean(clip.cloud) && alsoRemote})
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
        {confirmDelete?.cloud ? (
          <label className="modal-check">
            <input type="checkbox" checked={alsoRemote} onChange={e => setAlsoRemote(e.target.checked)} />
            <span>{t('cloud.alsoDeleteRemote', {server: confirmDelete.cloud.server_name})}</span>
          </label>
        ) : null}
      </Modal>

      <GameTagModal clip={tagging} onClose={() => setTagging(null)} />

      <Modal open={manualCopy !== null} title={t('viewer.copyLinkTitle')} onClose={() => setManualCopy(null)}>
        <p>{t('viewer.copyLinkBody')}</p>
        <textarea className="manual-copy" readOnly value={manualCopy ?? ''} rows={3} />
      </Modal>
    </>
  );

  return {actions, overlays};
}
