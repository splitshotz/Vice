import {useState} from 'react';

import {useStore} from '../state/store';
import {useClipActions} from '../state/clipActions';
import {useImageActions} from '../state/imageActions';
import {api} from '../lib/api';
import {t} from '../lib/i18n';
import {ClipCard} from '../components/ClipCard';
import {ImageCard} from '../components/ImageCard';
import {ContextMenu} from '../components/ContextMenu';
import {PlaylistModal, type PlaylistDraft} from '../components/PlaylistModal';
import {Modal} from '../components/Modal';
import {IconMore, IconWarning} from '../components/Icons';
import {SelectionBar} from '../components/SelectionBar';
import {useClipSelection} from '../state/selection';

export function Clips() {
  const {state, dispatch, visibleClips, visibleImages, hotkey, notify, refreshPlaylists} =
    useStore();
  const {playlists, currentPlaylistId, searchQuery, recentNew, status, config} = state;

  const playlist = currentPlaylistId
    ? (playlists.find(p => p.id === currentPlaylistId) ?? null)
    : null;

  const {actions, overlays} = useClipActions();
  const selection = useClipSelection(visibleClips);
  // A playlist holds both kinds. All Clips stays clips only, and the Images
  // section stays images only, so this is the one screen that shows a mix.
  const images = playlist ? visibleImages : [];
  const {actions: imageActions, overlays: imageOverlays} = useImageActions();
  const [editingPlaylist, setEditingPlaylist] = useState<'new' | 'edit' | null>(null);
  const [confirmPlaylistDelete, setConfirmPlaylistDelete] = useState(false);
  const [playlistMenu, setPlaylistMenu] = useState<{x: number; y: number} | null>(null);

  const fail = (title: string) => (err: Error) =>
    notify({kind: 'error', title, detail: err.message, tone: 'error', holdMs: 7000});

  const submitPlaylist = async (draft: PlaylistDraft) => {
    if (editingPlaylist === 'edit' && playlist) {
      const result = await api.updatePlaylist(playlist.id, draft);
      if (result.ok === false) throw new Error(result.error || t('clips.errUpdatePlaylist'));
      await refreshPlaylists();
      notify({kind: 'info', title: t('clips.playlistUpdated'), tone: 'accent', holdMs: 3000});
    } else {
      const result = await api.createPlaylist(draft);
      if (result.ok === false) throw new Error(result.error || t('clips.errCreatePlaylist'));
      await refreshPlaylists();
      dispatch({type: 'setView', view: 'clips', playlistId: result.playlist.id});
      notify({
        kind: 'info',
        title: t('clips.playlistCreated', {name: draft.name}),
        tone: 'accent',
        holdMs: 3500,
      });
    }
    setEditingPlaylist(null);
  };

  const title = playlist ? playlist.name : t('clips.allClips');
  const count = visibleClips.length;
  const query = searchQuery.trim();
  const subtitle = query
    ? t('clips.countMatches', {count, query})
    : t('clips.countClips', {count});
  const empty = count === 0 && images.length === 0;
  const isAuto = playlist?.kind === 'auto';

  return (
    <div className="clips">
      <header className="clips-head">
        <div className="clips-title">
          <h1>
            {playlist?.emoji ? <span className="clips-emoji">{playlist.emoji}</span> : null}
            {title}
          </h1>
          <p>{subtitle}</p>
        </div>

        <div className="clips-tools">
          {playlist ? (
            <button
              type="button"
              className="btn btn-quiet btn-icon-only"
              title={t('clips.playlistOptions')}
              aria-label={t('clips.playlistOptions')}
              onClick={e => {
                const r = e.currentTarget.getBoundingClientRect();
                setPlaylistMenu({x: r.right - 200, y: r.bottom + 6});
              }}>
              <IconMore size={16} />
            </button>
          ) : null}
          <button type="button" className="btn btn-quiet" onClick={() => setEditingPlaylist('new')}>
            {t('clips.newPlaylist')}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => void api.triggerClip().catch(fail(t('clips.errSaveClip')))}>
            {t('clips.saveClip')}
          </button>
        </div>
      </header>

      {status.hotkeys_available === false ? (
        <div className="banner" data-tone="warning" role="status">
          <IconWarning size={17} className="banner-icon" />
          <div className="banner-text">
            <strong>{t('clips.hotkeysUnavailableTitle')}</strong>
            <span>{t('clips.hotkeysUnavailableBody', {hotkey})}</span>
          </div>
        </div>
      ) : null}

      {empty ? (
        <p className="home-empty">
          {query
            ? t('clips.emptySearch', {query})
            : playlist
              ? t('clips.emptyPlaylist')
              : t('clips.emptyLibrary', {
                  hotkey,
                  duration: config?.recording?.clip_duration ?? 20,
                })}
        </p>
      ) : null}

      {count > 0 ? (
        <div className="clip-grid">
          {visibleClips.map(clip => (
            <ClipCard
              key={clip.slug}
              clip={clip}
              draggable
              isNew={recentNew.includes(clip.slug)}
              actions={actions}
              selection={{
                selected: selection.selected.has(clip.slug),
                selecting: selection.selecting,
                onPick: selection.pick,
              }}
            />
          ))}
        </div>
      ) : null}

      {images.length > 0 ? (
        <>
          <h2 className="clips-section">{t('clips.imagesHeading', {count: images.length})}</h2>
          <div className="clip-grid">
            {images.map(image => (
              <ImageCard key={image.slug} image={image} draggable actions={imageActions} />
            ))}
          </div>
        </>
      ) : null}

      {playlistMenu && playlist ? (
        <ContextMenu
          at={playlistMenu}
          heading={playlist.name}
          emptyLabel={t('common.noActions')}
          onClose={() => setPlaylistMenu(null)}
          items={[
            {
              id: 'edit',
              label: t('clips.editPlaylist'),
              onSelect: () => setEditingPlaylist('edit'),
            },
            {
              id: 'delete',
              label: t('clips.deletePlaylist'),
              danger: true,
              onSelect: () => setConfirmPlaylistDelete(true),
            },
          ]}
        />
      ) : null}

      <PlaylistModal
        open={editingPlaylist !== null}
        editing={editingPlaylist === 'edit' ? playlist : null}
        onClose={() => setEditingPlaylist(null)}
        onSubmit={submitPlaylist}
      />

      <Modal
        open={confirmPlaylistDelete}
        title={t('clips.confirmDeleteTitle')}
        onClose={() => setConfirmPlaylistDelete(false)}
        footer={
          <>
            <button
              type="button"
              className="btn btn-quiet"
              onClick={() => setConfirmPlaylistDelete(false)}>
              {t('common.keepIt')}
            </button>
            <button
              type="button"
              className="btn btn-danger-solid"
              onClick={() => {
                setConfirmPlaylistDelete(false);
                if (!playlist) return;
                void api
                  .deletePlaylist(playlist.id)
                  .then(async () => {
                    await refreshPlaylists();
                    dispatch({type: 'setView', view: 'clips', playlistId: null});
                    notify({
                      kind: 'info',
                      title: t('clips.playlistDeleted'),
                      tone: 'neutral',
                      holdMs: 3000,
                    });
                  })
                  .catch(fail(t('clips.errDeletePlaylist')));
              }}>
              {t('common.delete')}
            </button>
          </>
        }>
        <p>
          {t('clips.confirmDeleteBody')}
          {isAuto ? t('clips.confirmDeleteAuto') : ''}
        </p>
      </Modal>

      <SelectionBar selection={selection} />
      {overlays}
      {imageOverlays}
    </div>
  );
}
