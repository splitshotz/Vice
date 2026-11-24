import type {ClipSelection} from '../state/selection';
import {useCloudActions} from '../state/cloudActions';
import {t} from '../lib/i18n';
import {IconCloud, IconUpload} from './Icons';

/** What can be done to every picked clip at once. Floats over the grid. */
export function SelectionBar({selection}: {selection: ClipSelection}) {
  const cloud = useCloudActions();
  const {clips} = selection;
  if (!selection.selecting) return null;

  const notUp = clips.filter(c => !c.cloud);
  const withLocal = clips.filter(c => c.cloud?.local);
  const onServer = clips.filter(c => c.cloud);

  return (
    <div className="selection-bar" role="toolbar" aria-label={t('cloud.selectionLabel')}>
      <span className="selection-count">{t('cloud.selected', {count: clips.length})}</span>
      <button
        type="button"
        className="btn"
        disabled={!cloud.server || notUp.length === 0}
        title={cloud.server ? undefined : t('cloud.noServer')}
        onClick={() => {
          cloud.upload(notUp);
          selection.clear();
        }}>
        <IconUpload size={14} />
        {cloud.server
          ? t('cloud.uploadCount', {count: notUp.length, server: cloud.server.name})
          : t('cloud.noServer')}
      </button>
      {withLocal.length ? (
        <button
          type="button"
          className="btn btn-quiet"
          onClick={() => {
            cloud.deleteLocal(withLocal);
            selection.clear();
          }}>
          <IconCloud size={14} filled />
          {t('cloud.deleteLocalCount', {count: withLocal.length})}
        </button>
      ) : null}
      {onServer.length ? (
        <button
          type="button"
          className="btn btn-quiet"
          data-danger
          onClick={() => {
            cloud.removeFromServer(onServer);
            selection.clear();
          }}>
          {t('cloud.removeCount', {count: onServer.length})}
        </button>
      ) : null}
      <button type="button" className="btn btn-quiet" onClick={selection.selectAll}>
        {t('cloud.selectAll')}
      </button>
      <button type="button" className="btn btn-quiet" onClick={selection.clear}>
        {t('cloud.clearSelection')}
      </button>
    </div>
  );
}
