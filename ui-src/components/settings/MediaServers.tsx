import {useState} from 'react';

import {api} from '../../lib/api';
import {t} from '../../lib/i18n';
import type {UploadKind, UploadServer, UploadServers} from '../../lib/types';
import {useStore} from '../../state/store';
import {Row, Select, TextField, Toggle} from './Fields';

interface Form {
  server: UploadServer;
  secret: Record<string, string>;
}

const blank = (kind: UploadKind): UploadServer =>
  kind === 'zipline'
    ? {id: '', kind, name: 'Zipline', url: '', folder: ''}
    : {
        id: '',
        kind,
        name: 'S3',
        endpoint: '',
        region: '',
        bucket: '',
        prefix: 'vice/',
        public_base_url: '',
        presign_days: 7,
        path_style: false,
      };

/**
 * Settings for uploading clips to a media server. Saves as it goes rather than
 * through the page's draft: a server and its token are one thing, and the
 * token is never sent back to the page to be part of a draft.
 */
export function MediaServers() {
  const {state, dispatch, notify} = useStore();
  const data = state.uploadServers;
  const [form, setForm] = useState<Form | null>(null);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<{ok: boolean; text: string} | null>(null);
  const [saving, setSaving] = useState(false);

  const apply = (value: UploadServers) => dispatch({type: 'setUploadServers', value});
  const fail = (title: string) => (err: Error) =>
    notify({kind: 'error', title, detail: err.message, tone: 'error', holdMs: 7000});

  const edit = (server: UploadServer) => {
    setResult(null);
    setForm({server: {...server}, secret: {}});
  };
  const patch = (values: Partial<UploadServer>) =>
    setForm(f => (f ? {...f, server: {...f.server, ...values}} : f));
  const patchSecret = (key: string, value: string) =>
    setForm(f => (f ? {...f, secret: {...f.secret, [key]: value}} : f));

  const secretOut = (f: Form) => {
    const filled = Object.fromEntries(Object.entries(f.secret).filter(([, v]) => v.trim()));
    return Object.keys(filled).length ? filled : undefined;
  };

  const test = async () => {
    if (!form) return;
    setTesting(true);
    setResult(null);
    try {
      const r = await api.testUploadServer(form.server, secretOut(form));
      setResult(
        r.ok
          ? {ok: true, text: t('media.testOk', {detail: r.detail || form.server.name})}
          : {ok: false, text: r.error || t('media.testFailed')},
      );
    } catch (err) {
      setResult({ok: false, text: (err as Error).message});
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    if (!form) return;
    setSaving(true);
    try {
      const r = await api.saveUploadServer(form.server, secretOut(form), !data?.servers.length);
      apply(r);
      setForm(null);
      notify({kind: 'saved', title: t('media.saved', {name: form.server.name}), tone: 'accent', holdMs: 2500});
    } catch (err) {
      setResult({ok: false, text: (err as Error).message});
    } finally {
      setSaving(false);
    }
  };

  const remove = (server: UploadServer) =>
    void api
      .deleteUploadServer(server.id)
      .then(apply)
      .catch(fail(t('media.errRemove')));

  const activate = (server: UploadServer) =>
    void api
      .saveUploadServer(server, undefined, true)
      .then(apply)
      .catch(fail(t('media.errSave')));

  const setOption = (values: Partial<Pick<UploadServers, 'delete_after_upload' | 'concurrency'>>) => {
    if (!data) return;
    apply({...data, ...values});
    void api
      .saveConfig({upload: values})
      .then(r => {
        if (r.ok === false) throw new Error(r.error);
      })
      .catch((err: Error) => {
        apply(data);
        fail(t('media.errSave'))(err);
      });
  };

  const servers = data?.servers ?? [];
  const has = (form?.server.id && servers.find(s => s.id === form.server.id)?.has_secret) || false;
  const keep = has ? t('media.secretKept') : '';

  return (
    <>
      <Row label={t('media.servers')} help={t('media.serversHelp')} stack>
        <div className="media-servers">
          {servers.length === 0 && !form ? <p className="media-empty">{t('media.none')}</p> : null}
          {servers.map(server => (
            <div className="media-server" key={server.id} data-active={server.id === data?.active_server || undefined}>
              <button
                type="button"
                className="media-server-use"
                role="radio"
                aria-checked={server.id === data?.active_server}
                aria-label={t('media.useServer', {name: server.name})}
                title={t('media.useServer', {name: server.name})}
                onClick={() => activate(server)}
              />
              <div className="media-server-text">
                <strong>{server.name}</strong>
                <span className="mono">
                  {server.kind === 'zipline' ? server.url : `${server.bucket} · ${server.endpoint}`}
                </span>
                {server.has_secret ? null : <span className="media-warn">{t('media.noSecret')}</span>}
              </div>
              <button type="button" className="btn btn-quiet btn-sm" onClick={() => edit(server)}>
                {t('media.edit')}
              </button>
              <button type="button" className="btn btn-quiet btn-danger btn-sm" onClick={() => remove(server)}>
                {t('media.remove')}
              </button>
            </div>
          ))}
          {form ? null : (
            <div className="media-add">
              <button type="button" className="btn btn-sm" onClick={() => edit(blank('zipline'))}>
                {t('media.addZipline')}
              </button>
              <button type="button" className="btn btn-quiet btn-sm" onClick={() => edit(blank('s3'))}>
                {t('media.addS3')}
              </button>
            </div>
          )}
        </div>
      </Row>

      {form ? (
        <div className="media-form">
          <Row label={t('media.kind')}>
            <Select
              label={t('media.kind')}
              value={form.server.kind}
              onChange={kind => setForm({server: {...blank(kind as UploadKind), id: form.server.id, name: form.server.name}, secret: {}})}
              options={[
                ['zipline', 'Zipline'],
                ['s3', t('media.s3Name')],
              ]}
            />
          </Row>
          <Row label={t('media.name')}>
            <TextField label={t('media.name')} value={form.server.name} onChange={name => patch({name})} />
          </Row>

          {form.server.kind === 'zipline' ? (
            <>
              <Row label={t('media.url')} help={t('media.urlHelp')}>
                <TextField
                  label={t('media.url')}
                  mono
                  wide
                  placeholder="https://zipline.example.com"
                  value={form.server.url ?? ''}
                  onChange={url => patch({url})}
                />
              </Row>
              <Row label={t('media.token')} help={t('media.tokenHelp')}>
                <TextField
                  label={t('media.token')}
                  type="password"
                  mono
                  wide
                  placeholder={keep}
                  value={form.secret.token ?? ''}
                  onChange={v => patchSecret('token', v)}
                />
              </Row>
              <Row label={t('media.folder')} help={t('media.folderHelp')}>
                <TextField label={t('media.folder')} mono value={form.server.folder ?? ''} onChange={folder => patch({folder})} />
              </Row>
            </>
          ) : (
            <>
              <Row label={t('media.endpoint')} help={t('media.endpointHelp')}>
                <TextField
                  label={t('media.endpoint')}
                  mono
                  wide
                  placeholder="https://<account>.r2.cloudflarestorage.com"
                  value={form.server.endpoint ?? ''}
                  onChange={endpoint => patch({endpoint})}
                />
              </Row>
              <Row label={t('media.bucket')}>
                <TextField label={t('media.bucket')} mono value={form.server.bucket ?? ''} onChange={bucket => patch({bucket})} />
              </Row>
              <Row label={t('media.region')} help={t('media.regionHelp')}>
                <TextField
                  label={t('media.region')}
                  mono
                  placeholder="auto"
                  value={form.server.region ?? ''}
                  onChange={region => patch({region})}
                />
              </Row>
              <Row label={t('media.accessKey')}>
                <TextField
                  label={t('media.accessKey')}
                  type="password"
                  mono
                  wide
                  placeholder={keep}
                  value={form.secret.access_key ?? ''}
                  onChange={v => patchSecret('access_key', v)}
                />
              </Row>
              <Row label={t('media.secretKey')}>
                <TextField
                  label={t('media.secretKey')}
                  type="password"
                  mono
                  wide
                  placeholder={keep}
                  value={form.secret.secret_key ?? ''}
                  onChange={v => patchSecret('secret_key', v)}
                />
              </Row>
              <Row label={t('media.prefix')} help={t('media.prefixHelp')}>
                <TextField label={t('media.prefix')} mono value={form.server.prefix ?? ''} onChange={prefix => patch({prefix})} />
              </Row>
              <Row label={t('media.publicBase')} help={t('media.publicBaseHelp')}>
                <TextField
                  label={t('media.publicBase')}
                  mono
                  wide
                  placeholder="https://clips.example.com"
                  value={form.server.public_base_url ?? ''}
                  onChange={public_base_url => patch({public_base_url})}
                />
              </Row>
              {form.server.public_base_url ? null : (
                <Row label={t('media.presignDays')} help={t('media.presignDaysHelp')}>
                  <Select
                    label={t('media.presignDays')}
                    value={String(form.server.presign_days ?? 7)}
                    onChange={v => patch({presign_days: Number(v)})}
                    options={[1, 2, 3, 5, 7].map(n => [String(n), t('media.days', {count: n})] as [string, string])}
                  />
                </Row>
              )}
              <Row label={t('media.pathStyle')} help={t('media.pathStyleHelp')}>
                <Toggle label={t('media.pathStyle')} checked={Boolean(form.server.path_style)} onChange={path_style => patch({path_style})} />
              </Row>
            </>
          )}

          <div className="media-form-actions">
            {result ? (
              <span className="media-test" data-ok={result.ok || undefined} role="status">
                {result.text}
              </span>
            ) : null}
            <button type="button" className="btn btn-quiet btn-sm" disabled={testing} onClick={() => void test()}>
              {testing ? t('media.testing') : t('media.test')}
            </button>
            <button type="button" className="btn btn-quiet btn-sm" onClick={() => setForm(null)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="btn btn-sm" disabled={saving} onClick={() => void save()}>
              {t('media.save')}
            </button>
          </div>
        </div>
      ) : null}

      <Row label={t('media.deleteAfter')} help={t('media.deleteAfterHelp')}>
        <Toggle
          label={t('media.deleteAfter')}
          checked={Boolean(data?.delete_after_upload)}
          disabled={!data}
          onChange={delete_after_upload => setOption({delete_after_upload})}
        />
      </Row>
      <Row label={t('media.concurrency')} help={t('media.concurrencyHelp')}>
        <Select
          label={t('media.concurrency')}
          value={String(data?.concurrency ?? 2)}
          disabled={!data}
          onChange={v => setOption({concurrency: Number(v)})}
          options={[1, 2, 3, 4].map(n => [String(n), String(n)] as [string, string])}
        />
      </Row>
    </>
  );
}
