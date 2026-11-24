import {useCallback, useMemo} from 'react';

import {api} from '../lib/api';
import {clipTitle, type Clip, type UploadServer} from '../lib/types';
import type {MenuItem} from '../components/ContextMenu';
import {useStore} from './store';
import {t} from '../lib/i18n';

/** A clip that lives only on a media server: it plays from there and has no file here. */
export const isCloudOnly = (clip: Clip) => Boolean(clip.cloud && !clip.cloud.local);

export interface CloudActions {
  /** The server uploads go to, or null when none is set up. */
  server: UploadServer | null;
  upload: (clips: Clip[]) => void;
  cancel: (clip: Clip) => void;
  /** Removes the file here and keeps the clip in the library from the server. */
  deleteLocal: (clips: Clip[]) => void;
  download: (clip: Clip) => void;
  removeFromServer: (clips: Clip[]) => void;
  /** Context menu entries for one clip. Empty when there is nothing to offer. */
  menuItems: (clip: Clip) => MenuItem[];
}

/**
 * Everything to do with media servers, for the card menu, the viewer and the
 * selection bar alike, so the three never disagree on what a cloud clip can do.
 */
export function useCloudActions(): CloudActions {
  const {state, notify} = useStore();
  const servers = state.uploadServers;
  const server = useMemo(() => {
    if (!servers?.servers.length) return null;
    return servers.servers.find(s => s.id === servers.active_server) ?? servers.servers[0];
  }, [servers]);

  const fail = useCallback(
    (title: string) => (err: Error) =>
      notify({kind: 'error', title, detail: err.message, tone: 'error', holdMs: 8000}),
    [notify],
  );

  const upload = useCallback(
    (clips: Clip[]) => {
      const slugs = clips.filter(c => !isCloudOnly(c)).map(c => c.slug);
      if (!slugs.length) return;
      void api
        .uploadClips(slugs, server?.id)
        .then(result => {
          if (!result.queued.length) {
            notify({kind: 'info', title: t('cloud.alreadyUp'), tone: 'neutral', holdMs: 3000});
          } else if (result.queued.length > 1) {
            notify({
              kind: 'saving',
              title: t('cloud.queued', {count: result.queued.length, server: server?.name ?? ''}),
              tone: 'accent',
              holdMs: 3000,
            });
          }
        })
        .catch(fail(t('cloud.errUpload')));
    },
    [fail, notify, server],
  );

  const cancel = useCallback(
    (clip: Clip) => void api.cancelUpload(clip.slug).catch(fail(t('cloud.errCancel'))),
    [fail],
  );

  const deleteLocal = useCallback(
    (clips: Clip[]) => {
      const targets = clips.filter(c => c.cloud?.local);
      void Promise.all(targets.map(c => api.deleteClip(c.slug, {keepCloud: true})))
        .then(() => {
          if (targets.length) {
            notify({
              kind: 'info',
              title: t('cloud.localRemoved', {count: targets.length}),
              tone: 'accent',
              holdMs: 3500,
            });
          }
        })
        .catch(fail(t('cloud.errDeleteLocal')));
    },
    [fail, notify],
  );

  const download = useCallback(
    (clip: Clip) =>
      void api
        .downloadCloudClip(clip.slug)
        .then(() =>
          notify({
            kind: 'saved',
            title: t('cloud.downloaded'),
            detail: clipTitle(clip),
            tone: 'accent',
            holdMs: 3500,
          }),
        )
        .catch(fail(t('cloud.errDownload'))),
    [fail, notify],
  );

  const removeFromServer = useCallback(
    (clips: Clip[]) => {
      const targets = clips.filter(c => c.cloud);
      void Promise.all(targets.map(c => api.removeFromServer(c.slug)))
        .then(() => {
          if (targets.length) {
            notify({
              kind: 'info',
              title: t('cloud.removed', {count: targets.length}),
              tone: 'neutral',
              holdMs: 3500,
            });
          }
        })
        .catch(fail(t('cloud.errRemove')));
    },
    [fail, notify],
  );

  const menuItems = useCallback(
    (clip: Clip): MenuItem[] => {
      const job = state.uploads[clip.slug];
      if (job && job.state !== 'downloading') {
        return [{id: 'cloud-cancel', label: t('cloud.cancelUpload'), onSelect: () => cancel(clip)}];
      }
      if (!clip.cloud) {
        return [
          {
            id: 'cloud-upload',
            label: server ? t('cloud.uploadTo', {server: server.name}) : t('cloud.noServer'),
            disabled: !server || Boolean(job),
            onSelect: () => upload([clip]),
          },
        ];
      }
      const items: MenuItem[] = [];
      if (clip.cloud.local) {
        items.push({id: 'cloud-delete-local', label: t('cloud.deleteLocal'), onSelect: () => deleteLocal([clip])});
      } else {
        items.push({
          id: 'cloud-download',
          label: t('cloud.download', {server: clip.cloud.server_name}),
          disabled: Boolean(job),
          onSelect: () => download(clip),
        });
      }
      items.push({
        id: 'cloud-remove',
        label: t('cloud.removeFrom', {server: clip.cloud.server_name}),
        danger: !clip.cloud.local,
        onSelect: () => removeFromServer([clip]),
      });
      return items;
    },
    [cancel, deleteLocal, download, removeFromServer, server, state.uploads, upload],
  );

  return {server, upload, cancel, deleteLocal, download, removeFromServer, menuItems};
}
