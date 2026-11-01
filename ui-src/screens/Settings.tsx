import {useCallback, useEffect, useMemo, useRef, useState} from 'react';

import {api} from '../lib/api';
import type {KeyOverlayState} from '../lib/types';
import {formatLengthLong} from '../lib/format';
import {availableLocales, currentLocale, setLocale, t, tNode} from '../lib/i18n';
import {LOCALE_LABELS, type LocaleName} from '../locales';
import {
  EFFECTS_MODES,
  applyEffects,
  effectsNote,
  isEffectsMode,
  subscribeEffects,
  type EffectsMode,
} from '../lib/effects';
import {
  RESOLUTION_PRESETS,
  SOUND_FIELDS,
  bufferNote,
  draftFromConfig,
  newClipPreset,
  patchFromDraft,
  renderClipName,
  requiredBuffer,
  resolvedResolution,
  type ClipPreset,
  type Draft,
} from '../lib/settingsDraft';
import {ACCENTS, ACCENT_NAMES} from '../theme/accents';
import {useStore} from '../state/store';
import {Modal} from '../components/Modal';
import {IconCheck, IconClose, IconPlus} from '../components/Icons';
import {AccentPicker} from '../components/AccentPicker';
import {useAccentChoice} from '../lib/accentChoice';
import {customAccent as deriveCustom} from '../theme/viceTheme';
import {AudioTracks, type AudioSource} from '../components/settings/AudioTracks';
import {KeyCapture} from '../components/settings/KeyCapture';
import {
  Row,
  Select,
  Slider,
  SoundGrid,
  TextArea,
  TextField,
  Toggle,
  type RowNote,
} from '../components/settings/Fields';

const SECTIONS = [
  ['recording', 'settings.secRecording'],
  ['audio', 'settings.secAudio'],
  ['hotkeys', 'settings.secHotkeys'],
  ['storage', 'settings.secStorage'],
  ['sharing', 'settings.secSharing'],
  ['discord', 'settings.secDiscord'],
  ['overlay', 'settings.secOverlay'],
  ['appearance', 'settings.secAppearance'],
  ['advanced', 'settings.secAdvanced'],
] as const;

type SectionId = (typeof SECTIONS)[number][0];

interface DisplayInfo {
  displays: Array<{id: string; label?: string}>;
  warning?: string | null;
  follow_mouse_supported?: boolean;
}

export function Settings() {
  const {state, saveConfig, notify} = useStore();
  const {choose, chooseCustom, seed} = useAccentChoice();
  const [picking, setPicking] = useState(false);
  const customBase = seed ? deriveCustom(seed).ramp.base : null;
  const {config, accent, status} = state;

  const [draft, setDraft] = useState<Draft | null>(null);
  const [baseline, setBaseline] = useState<string>('');
  const [displays, setDisplays] = useState<DisplayInfo>({displays: [], warning: null});
  const [sources, setSources] = useState<{sources: AudioSource[]; warning?: string | null}>({
    sources: [],
  });
  const [trackPick, setTrackPick] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [section, setSection] = useState<SectionId>('recording');
  const [restartNeeded, setRestartNeeded] = useState(false);
  const [wfMicPrompt, setWfMicPrompt] = useState(false);
  const [effects, setEffects] = useState<EffectsMode>('auto');
  const [, forceEffectsNote] = useState(0);
  const [checkingUpdate, setCheckingUpdate] = useState(false);

  const bodyRef = useRef<HTMLDivElement>(null);
  const sectionRefs = useRef(new Map<SectionId, HTMLElement>());
  // Set while the rail is scrolling somewhere, so scroll-spy does not fight it.
  const scrollingTo = useRef<SectionId | null>(null);

  const update = useCallback(
    (patch: Partial<Draft>) => setDraft(prev => (prev ? {...prev, ...patch} : prev)),
    [],
  );

  // The draft is seeded once. Later config merges (a mic toggle from Home, for
  // instance) must not wipe edits in progress, so the reseed is keyed on the
  // config arriving rather than on it changing.
  useEffect(() => {
    if (!config || draft) return;
    const next = draftFromConfig(config);
    setDraft(next);
    setBaseline(JSON.stringify(patchFromDraft(next)));
  }, [config, draft]);

  useEffect(() => {
    void api
      .getAppState()
      .then(s => {
        const mode = isEffectsMode(s.effects_mode) ? s.effects_mode : 'auto';
        setEffects(mode);
        applyEffects(mode);
      })
      .catch(() => applyEffects('auto'));
    return subscribeEffects(() => forceEffectsNote(n => n + 1));
  }, []);

  const loadDisplays = useCallback(async (backend: string) => {
    try {
      const info = await api.displays(backend || 'auto');
      setDisplays(info as unknown as DisplayInfo);
    } catch {
      setDisplays({displays: [], warning: t('settings.displayLoadFailed')});
    }
  }, []);

  const loadSources = useCallback(async () => {
    try {
      const info = await api.audioSources();
      setSources(info as unknown as {sources: AudioSource[]; warning?: string | null});
      setTrackPick(prev => prev || (info.sources as AudioSource[])[0]?.id || '');
    } catch {
      setSources({
        sources: [{id: 'default_output', label: t('settings.defaultOutput')}],
        warning: t('settings.sourceLoadFailed'),
      });
    }
  }, []);

  useEffect(() => {
    if (!draft) return;
    void loadDisplays(draft.backend);
    // Only the backend changes what is enumerable, so this does not rerun on
    // every keystroke elsewhere in the form.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft?.backend, loadDisplays]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  // Scroll-spy: whichever section heading is nearest the top of the scroller
  // owns the rail. The old rail only ever highlighted what you last clicked.
  useEffect(() => {
    const scroller = bodyRef.current;
    if (!scroller || !draft) return;
    const onScroll = () => {
      if (scrollingTo.current) return;
      const top = scroller.getBoundingClientRect().top + 96;
      let current: SectionId = SECTIONS[0][0];
      for (const [id] of SECTIONS) {
        const node = sectionRefs.current.get(id);
        if (node && node.getBoundingClientRect().top <= top) current = id;
      }
      setSection(current);
    };
    scroller.addEventListener('scroll', onScroll, {passive: true});
    onScroll();
    return () => scroller.removeEventListener('scroll', onScroll);
  }, [draft]);

  const goTo = (id: SectionId) => {
    setSection(id);
    scrollingTo.current = id;
    sectionRefs.current.get(id)?.scrollIntoView({behavior: 'smooth', block: 'start'});
    window.setTimeout(() => {
      scrollingTo.current = null;
    }, 600);
  };

  const dirty = useMemo(
    () => (draft ? JSON.stringify(patchFromDraft(draft)) !== baseline : false),
    [draft, baseline],
  );

  // Leaving with unsaved changes is the one way this screen can lose work.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  if (!draft) {
    return (
      <div className="settings">
        <p className="home-empty">{t('settings.loading')}</p>
      </div>
    );
  }

  const fail = (title: string, err: unknown) =>
    notify({
      kind: 'error',
      title,
      detail: (err as Error)?.message,
      tone: 'error',
      holdMs: 7000,
    });

  const say = (title: string, detail?: string) =>
    notify({kind: 'info', title, detail, tone: 'accent', holdMs: 3500});

  /** Persist one field on the spot, for the controls Home also owns. */
  const persistNow = async (
    patch: Record<string, Record<string, unknown>>,
    onOk: () => void,
    failure: string,
  ) => {
    try {
      const result = await saveConfig(patch);
      if (result.applied === false && result.warning) {
        notify({
          kind: 'error',
          title: t('settings.savedNotApplied'),
          detail: result.warning,
          tone: 'error',
          holdMs: 8000,
        });
      } else {
        onOk();
      }
      // The baseline moves with it, or the save bar would report a change the
      // daemon already has.
      setBaseline(prev => {
        const merged = {...JSON.parse(prev)} as Record<string, Record<string, unknown>>;
        for (const [group, values] of Object.entries(patch)) {
          merged[group] = {...(merged[group] ?? {}), ...values};
        }
        return JSON.stringify(merged);
      });
    } catch (err) {
      fail(failure, err);
    }
  };

  const micNeedsWfChoice =
    !draft.captureMic &&
    draft.captureAudio &&
    draft.wfMicStrategy === 'prompt' &&
    (draft.backend === 'wf-recorder' || status.backend === 'wf-recorder');

  const setMic = async (enabled: boolean, strategy?: string) => {
    update({captureMic: enabled, ...(strategy ? {wfMicStrategy: strategy} : {})});
    await persistNow(
      {recording: {capture_microphone: enabled, ...(strategy ? {wf_microphone_strategy: strategy} : {})}},
      () =>
        say(
          enabled ? t('settings.micOn') : t('settings.micOff'),
          enabled ? t('settings.micOnDetail') : t('settings.micOffDetail'),
        ),
      t('settings.errMic'),
    );
  };

  const save = async () => {
    if (resolvedResolution(draft) === false) {
      notify({
        kind: 'error',
        title: t('settings.badResolutionTitle'),
        detail: t('settings.badResolutionDetail'),
        tone: 'error',
        holdMs: 6000,
      });
      goTo('recording');
      return;
    }

    // The buffer cannot be shorter than the longest clip a key can save. Show
    // the correction rather than performing it behind the user's back.
    const buffer = requiredBuffer(draft);
    const corrected = buffer !== draft.bufferDuration;
    if (corrected) update({bufferDuration: buffer});

    const patch = patchFromDraft({...draft, bufferDuration: buffer});
    const sharingChanged =
      Number(patch.sharing.port) !== Number(config?.sharing?.port ?? 8765) ||
      Boolean(patch.sharing.cloudflare_tunnel) !== Boolean(config?.sharing?.cloudflare_tunnel !== false);

    setSaving(true);
    try {
      const result = await saveConfig(patch);
      if (result.applied === false && result.warning) {
        notify({
          kind: 'error',
          title: t('settings.savedNotApplied'),
          detail: result.warning,
          tone: 'error',
          holdMs: 9000,
        });
      } else {
        say(
          t('settings.saved'),
          corrected
            ? t('settings.bufferRaised', {length: formatLengthLong(buffer)})
            : undefined,
        );
      }
      if (result.restart_required && sharingChanged) setRestartNeeded(true);
      setBaseline(JSON.stringify(patch));
    } catch (err) {
      fail(t('settings.errSave'), err);
    } finally {
      setSaving(false);
    }
  };

  const revert = () => {
    if (!config) return;
    const next = draftFromConfig(config);
    setDraft(next);
    setBaseline(JSON.stringify(patchFromDraft(next)));
  };


  const setEffectsMode = (mode: EffectsMode) => {
    setEffects(mode);
    applyEffects(mode);
    void api
      .setAppState({effects_mode: mode})
      .catch(err => console.debug('Saving the effects mode failed', err));
  };

  const register = (id: SectionId) => (node: HTMLElement | null) => {
    if (node) sectionRefs.current.set(id, node);
    else sectionRefs.current.delete(id);
  };

  const buffer = bufferNote(draft);
  const followSupported = displays.follow_mouse_supported !== false;
  // Window pinning is GSR-only; status.backend is what's actually running,
  // not just configured (backend can be "auto").
  const windowCaptureSupported = draft.backend === 'gsr' || status.backend === 'gpu-screen-recorder';

  return (
    <div className="settings">
      <header className="settings-head">
        <div>
          <h1>{t('settings.title')}</h1>
          <p>{t('settings.subtitle')}</p>
        </div>
      </header>

      <nav className="settings-rail" aria-label={t('settings.sections')}>
        {SECTIONS.map(([id, labelKey]) => (
          <button
            key={id}
            type="button"
            className="rail-chip"
            aria-current={section === id ? 'true' : undefined}
            onClick={() => goTo(id)}>
            {t(labelKey)}
          </button>
        ))}
      </nav>

      <div className="settings-body" ref={bodyRef}>
        {/* ── Recording ─────────────────────────────────────────── */}
        <Card id="recording" title={t('settings.secRecording')} register={register('recording')}>
          <Row label={t('settings.bufferDuration')} note={buffer}>
            <Slider
              label={t('settings.bufferDuration')}
              exact
              value={draft.bufferDuration}
              min={30}
              max={1800}
              step={30}
              onChange={bufferDuration => update({bufferDuration})}
              format={formatLengthLong}
            />
          </Row>

          <Row
            label={t('settings.replayStorage')}
            help={t('settings.replayStorageHelp')}>
            <Select
              label={t('settings.replayStorage')}
              value={draft.replayStorage}
              onChange={replayStorage => update({replayStorage})}
              options={[
                ['auto', t('settings.optAutoRecommended')],
                ['ram', t('settings.replayRam')],
                ['disk', t('settings.replayDisk')],
              ]}
            />
          </Row>

          <Row label={t('settings.clipDuration')} help={t('settings.clipDurationHelp')}>
            <Slider
              label={t('settings.clipDuration')}
              exact
              value={draft.clipDuration}
              min={5}
              max={1800}
              step={5}
              onChange={clipDuration => update({clipDuration})}
              format={formatLengthLong}
            />
          </Row>

          <Row label={t('settings.frameRate')} help={t('settings.frameRateHelp')}>
            <Select
              label={t('settings.frameRate')}
              value={draft.fps}
              onChange={fps => update({fps})}
              options={['24', '30', '50', '60', '120', '144'].map(v => [v, t('settings.fpsOption', {fps: v})] as [string, string])}
            />
          </Row>

          <Row label={t('settings.resolution')} help={t('settings.resolutionHelp')}>
            <Select
              label={t('settings.resolution')}
              value={draft.resolution}
              onChange={resolution => update({resolution})}
              options={[...resolutionOptions(), ['custom', t('settings.resolutionCustom')] as [string, string]]}
            />
          </Row>

          {draft.resolution === 'custom' ? (
            <Row
              label={t('settings.customResolution')}
              note={
                resolvedResolution(draft) === false
                  ? {text: t('settings.customResolutionWarn'), tone: 'warning' as const}
                  : null
              }
              help={t('settings.customResolutionHelp')}>
              <TextField
                label={t('settings.customResolution')}
                mono
                value={draft.customResolution}
                placeholder="1600x900"
                onChange={customResolution => update({customResolution})}
              />
            </Row>
          ) : null}

          <Row
            label={t('settings.container')}
            help={t('settings.containerHelp')}>
            <Select
              label={t('settings.container')}
              value={draft.container}
              onChange={container => update({container})}
              options={[
                ['mp4', t('settings.containerMp4')],
                ['mkv', t('settings.containerMkv')],
              ]}
            />
          </Row>

          <Row label={t('settings.encoder')} help={t('settings.encoderHelp')}>
            <Select
              label={t('settings.encoder')}
              value={draft.encoder}
              onChange={encoder => update({encoder})}
              options={[
                ['auto', t('settings.optAutoRecommended')],
                ['h264_nvenc', t('settings.encoderH264Nvenc')],
                ['hevc_nvenc', t('settings.encoderHevcNvenc')],
                ['h264_vaapi', t('settings.encoderH264Vaapi')],
                ['hevc_vaapi', t('settings.encoderHevcVaapi')],
                ['av1_nvenc', t('settings.encoderAv1Nvenc')],
                ['av1_vaapi', t('settings.encoderAv1Vaapi')],
                ['h264_vulkan', t('settings.encoderH264Vulkan')],
                ['hevc_vulkan', t('settings.encoderHevcVulkan')],
                ['av1_vulkan', t('settings.encoderAv1Vulkan')],
                ['libx264', t('settings.encoderX264')],
                ['libx265', t('settings.encoderX265')],
              ]}
            />
          </Row>

          <Row
            label={t('settings.colourDepth')}
            help={t('settings.colourDepthHelp')}>
            <Select
              label={t('settings.colourDepth')}
              value={draft.colorDepth}
              onChange={colorDepth => update({colorDepth})}
              options={[
                ['8', t('settings.colour8')],
                ['10', t('settings.colour10')],
              ]}
            />
          </Row>

          <Row
            label={t('settings.hardwareDecode')}
            help={t('settings.hardwareDecodeHelp')}>
            <Toggle
              label={t('settings.hardwareDecode')}
              checked={draft.hardwareDecode}
              onChange={hardwareDecode => update({hardwareDecode})}
            />
          </Row>

          <Row label={t('settings.backend')} help={t('settings.backendHelp')}>
            <Select
              label={t('settings.backend')}
              value={draft.backend}
              onChange={backend => update({backend})}
              options={[
                ['auto', t('settings.optAutoRecommended')],
                ['gsr', 'gpu-screen-recorder'],
                ['wf-recorder', t('settings.backendWf')],
                ['ffmpeg', t('settings.backendFfmpeg')],
              ]}
            />
          </Row>

          {/* Only offered once the Flatpak build is installed, or already chosen. */}
          {status.gsr?.flatpak || draft.gsrSource !== 'auto' ? (
            <Row label={t('settings.gsrSource')} help={t('settings.gsrSourceHelp')}>
              <Select
                label={t('settings.gsrSource')}
                value={draft.gsrSource}
                onChange={gsrSource => update({gsrSource})}
                options={[
                  ['auto', t('settings.gsrSourceAuto')],
                  ['native', t('settings.gsrSourceNative')],
                  ['flatpak', t('settings.gsrSourceFlatpak')],
                ]}
              />
            </Row>
          ) : null}

          <Row
            label={t('settings.followMouse')}
            note={
              followSupported
                ? null
                : {
                    text: t('settings.followMouseUnsupported'),
                    tone: 'warning' as const,
                  }
            }
            help={t('settings.followMouseHelp')}>
            <Toggle
              label={t('settings.followMouse')}
              checked={followSupported && draft.followMouse}
              disabled={!followSupported || draft.windowCapture}
              onChange={followMouse => update({followMouse})}
            />
          </Row>

          <Row
            label={t('settings.windowCapture')}
            note={
              windowCaptureSupported
                ? null
                : {text: t('settings.windowCaptureUnsupported'), tone: 'warning' as const}
            }
            help={t('settings.windowCaptureHelp')}>
            <Toggle
              label={t('settings.windowCapture')}
              checked={windowCaptureSupported && draft.windowCapture}
              disabled={!windowCaptureSupported}
              onChange={windowCapture => update({windowCapture, followMouse: windowCapture ? false : draft.followMouse})}
            />
          </Row>

          <Row label={t('settings.display')} note={displayNote(draft, displays)}>
            <Select
              label={t('settings.display')}
              value={draft.display}
              disabled={(draft.followMouse && followSupported) || draft.windowCapture}
              onChange={display => update({display})}
              options={displayOptions(draft, displays)}
            />
          </Row>
        </Card>

        {/* ── Audio ─────────────────────────────────────────────── */}
        <Card id="audio" title={t('settings.secAudio')} register={register('audio')}>
          <Row label={t('settings.captureDesktopAudio')} help={t('settings.captureDesktopAudioHelp')}>
            <Toggle
              label={t('settings.captureDesktopAudio')}
              checked={draft.captureAudio}
              onChange={captureAudio => update({captureAudio})}
            />
          </Row>

          <Row
            label={t('settings.captureMic')}
            help={t('settings.captureMicHelp')}>
            <Toggle
              label={t('settings.captureMic')}
              checked={draft.captureMic}
              onChange={next => {
                if (next && micNeedsWfChoice) setWfMicPrompt(true);
                else void setMic(next);
              }}
            />
          </Row>

          <Row label={t('settings.desktopSource')} note={desktopSourceNote(draft, sources)}>
            <Select
              label={t('settings.desktopSource')}
              value={draft.desktopSource}
              onChange={desktopSource => update({desktopSource})}
              options={groupedSourceOptions(draft.desktopSource, sources.sources)}
            />
          </Row>

          <Row
            label={t('settings.micSource')}
            help={t('settings.micSourceHelp')}>
            <Select
              label={t('settings.micSource')}
              value={draft.micSource}
              onChange={micSource => update({micSource})}
              options={micSourceOptions(draft.micSource, sources.sources)}
            />
          </Row>

          <Row
            label={t('settings.micMono')}
            help={t('settings.micMonoHelp')}>
            <Toggle
              label={t('settings.micMono')}
              checked={draft.micMono}
              onChange={micMono => update({micMono})}
            />
          </Row>

          {/* Separate tracks keep their own levels, so the balance sliders
              would be claiming an effect they do not have. */}
          {draft.audioTracks.length === 0 ? (
            <>
              <Row label={t('settings.desktopVolume')} help={t('settings.desktopVolumeHelp')}>
                <Slider
                  label={t('settings.desktopVolume')}
                  value={draft.desktopVolume}
                  min={0}
                  max={200}
                  step={5}
                  onChange={desktopVolume => update({desktopVolume})}
                  format={v => `${v}%`}
                />
              </Row>
              <Row
                label={t('settings.micVolume')}
                help={t('settings.micVolumeHelp')}>
                <Slider
                  label={t('settings.micVolume')}
                  value={draft.micVolume}
                  min={0}
                  max={200}
                  step={5}
                  onChange={micVolume => update({micVolume})}
                  format={v => `${v}%`}
                />
              </Row>
            </>
          ) : null}

          <Row
            label={t('settings.notifyVolume')}
            help={t('settings.notifyVolumeHelp')}>
            <Slider
              label={t('settings.notifyVolume')}
              value={draft.notifyVolume}
              min={0}
              max={100}
              step={5}
              onChange={notifyVolume => update({notifyVolume})}
              format={v => (v > 0 ? `${v}%` : t('settings.volumeOff'))}
            />
          </Row>

          <Row
            label={t('settings.customSounds')}
            stack
            help={t('settings.customSoundsHelp')}>
            <SoundGrid
              fields={SOUND_FIELDS}
              values={draft.sounds}
              onChange={(key, value) => update({sounds: {...draft.sounds, [key]: value}})}
            />
          </Row>

          <Row
            label={t('settings.audioTracks')}
            stack
            help={t('settings.audioTracksHelp')}>
            <AudioTracks
              tracks={draft.audioTracks}
              sources={sources.sources}
              mixFirst={draft.mixFirstTrack}
              desktopAudioOn={draft.captureAudio}
              pick={trackPick}
              onPickChange={setTrackPick}
              onChange={audioTracks => update({audioTracks})}
              volumes={draft.audioTrackVolumes}
              onVolumeChange={(id, percent) =>
                update({audioTrackVolumes: {...draft.audioTrackVolumes, [id]: percent}})
              }
              micTrack={
                draft.captureMic &&
                !draft.audioTracks.some(track =>
                  track.split('|').includes(draft.micSource || 'default_input'),
                )
                  ? {value: draft.micVolume, onChange: micVolume => update({micVolume})}
                  : null
              }
              refreshing={refreshing}
              onDuplicate={() =>
                notify({
                  kind: 'error',
                  title: t('settings.duplicateTrack'),
                  tone: 'error',
                  holdMs: 3500,
                })
              }
              onRefresh={() => {
                setRefreshing(true);
                void loadSources()
                  .then(() => say(t('settings.sourcesRefreshed')))
                  .finally(() => setRefreshing(false));
              }}
            />
          </Row>

          <Row
            label={t('settings.mixFirstTrack')}
            help={t('settings.mixFirstTrackHelp')}>
            <Toggle
              label={t('settings.mixFirstTrack')}
              checked={draft.mixFirstTrack}
              onChange={mixFirstTrack => update({mixFirstTrack})}
            />
          </Row>

          <Row
            label={t('settings.wfMicMode')}
            help={t('settings.wfMicModeHelp')}>
            <Select
              label={t('settings.wfMicMode')}
              value={draft.wfMicStrategy}
              onChange={wfMicStrategy => update({wfMicStrategy})}
              options={[
                ['prompt', t('settings.wfMicPrompt')],
                ['backend_fallback', t('settings.wfMicFallback')],
                ['mic_only', t('settings.wfMicOnly')],
              ]}
            />
          </Row>
        </Card>

        {/* ── Hotkeys ───────────────────────────────────────────── */}
        <Card id="hotkeys" title={t('settings.secHotkeys')} register={register('hotkeys')}>
          <Row
            label={t('settings.clipKey')}
            note={
              status.hotkeys_available === false
                ? {
                    text: t('settings.hotkeysUnavailable'),
                    tone: 'warning' as const,
                  }
                : null
            }
            help={t('settings.clipKeyHelp')}>
            <KeyCapture
              value={draft.clipKey}
              onUnsupported={() =>
                notify({kind: 'error', title: t('settings.keyUnsupported'), tone: 'error', holdMs: 4000})
              }
              onCapture={clipKey => {
                update({clipKey});
                void persistNow(
                  {hotkeys: {clip: clipKey}},
                  () => say(t('settings.clipKeyNow', {key: clipKey})),
                  t('settings.errSaveKey'),
                );
              }}
            />
          </Row>

          <Row label={t('settings.screenshotKey')} help={t('settings.screenshotKeyHelp')}>
            <div className="key-pair">
              <KeyCapture
                value={draft.screenshotKey}
                onUnsupported={() =>
                  notify({kind: 'error', title: t('settings.keyUnsupported'), tone: 'error', holdMs: 4000})
                }
                onCapture={screenshotKey => {
                  update({screenshotKey});
                  void persistNow(
                    {hotkeys: {screenshot: screenshotKey}},
                    () => say(t('settings.screenshotKeyNow', {key: screenshotKey})),
                    t('settings.errSaveKey'),
                  );
                }}
              />
              {draft.screenshotKey ? (
                <button
                  type="button"
                  className="btn btn-quiet btn-sm"
                  onClick={() => {
                    update({screenshotKey: ''});
                    void persistNow(
                      {hotkeys: {screenshot: ''}},
                      () => say(t('settings.screenshotKeyCleared')),
                      t('settings.errSaveKey'),
                    );
                  }}>
                  {t('settings.clearKey')}
                </button>
              ) : null}
            </div>
          </Row>

          <Row
            label={t('settings.clipPresets')}
            stack
            help={t('settings.clipPresetsHelp')}>
            <ClipPresets
              presets={draft.clipPresets}
              onChange={clipPresets => update({clipPresets})}
              onUnsupported={() =>
                notify({kind: 'error', title: t('settings.keyUnsupported'), tone: 'error', holdMs: 4000})
              }
            />
          </Row>

          <Row
            label={t('settings.blocklist')}
            stack
            help={t('settings.blocklistHelp')}>
            <TextArea
              label={t('settings.blocklist')}
              value={draft.hotkeyBlocklist}
              placeholder="ggst.exe"
              onChange={hotkeyBlocklist => update({hotkeyBlocklist})}
            />
          </Row>
        </Card>

        {/* ── Storage ───────────────────────────────────────────── */}
        <Card id="storage" title={t('settings.secStorage')} register={register('storage')}>
          <Row label={t('settings.directory')} help={t('settings.directoryHelp')}>
            <TextField
              label={t('settings.directory')}
              wide
              value={draft.directory}
              placeholder="~/Videos/Vice"
              onChange={directory => update({directory})}
            />
          </Row>

          <Row label={t('settings.imageDirectory')} help={t('settings.imageDirectoryHelp')}>
            <TextField
              label={t('settings.imageDirectory')}
              wide
              value={draft.imageDirectory}
              placeholder="~/Pictures/Vice"
              onChange={imageDirectory => update({imageDirectory})}
            />
          </Row>

          <Row
            label={t('settings.tagWithGame')}
            help={t('settings.tagWithGameHelp')}>
            <Toggle
              label={t('settings.tagWithGame')}
              checked={draft.tagWithGame}
              onChange={tagWithGame => update({tagWithGame})}
            />
          </Row>

          <Row
            label={t('settings.autoPlaylist')}
            help={t('settings.autoPlaylistHelp')}>
            <Toggle
              label={t('settings.autoPlaylist')}
              checked={draft.autoPlaylist}
              onChange={autoPlaylist => update({autoPlaylist})}
            />
          </Row>

          <Row
            label={t('settings.clipFilename')}
            note={clipNameNote(draft)}
            help={
              <>
                {tNode('settings.clipFilenameHelp', {
                  n: <code>$n</code>,
                  date: <code>$date</code>,
                  time: <code>$time</code>,
                  game: <code>$game</code>,
                })}
              </>
            }>
            <TextField
              label={t('settings.clipFilename')}
              wide
              mono
              value={draft.clipNameTemplate}
              placeholder="clip_$date_$time"
              onChange={clipNameTemplate => update({clipNameTemplate})}
            />
          </Row>
        </Card>

        {/* ── Sharing ───────────────────────────────────────────── */}
        <Card id="sharing" title={t('settings.secSharing')} register={register('sharing')}>
          <Row label={t('settings.port')} help={t('settings.portHelp')}>
            <TextField
              label={t('settings.port')}
              mono
              type="number"
              min={1024}
              max={65535}
              value={draft.port}
              onChange={port => update({port: Number(port) || 0})}
            />
          </Row>

          <Row
            label={t('settings.publicLink')}
            help={t('settings.publicLinkHelp')}>
            <Toggle
              label={t('settings.publicLink')}
              checked={draft.cloudflareTunnel}
              onChange={cloudflareTunnel => update({cloudflareTunnel})}
            />
          </Row>

          <Row
            label={t('settings.shareDiscordFile')}
            help={t('settings.shareDiscordFileHelp')}>
            <Toggle
              label={t('settings.shareDiscordFile')}
              checked={draft.shareDiscordFile}
              onChange={shareDiscordFile => update({shareDiscordFile})}
            />
          </Row>
        </Card>

        {/* ── Discord ───────────────────────────────────────────── */}
        <Card id="discord" title={t('settings.cardDiscord')} register={register('discord')}>
          <Row
            label={t('settings.discordEnabled')}
            help={t('settings.discordEnabledHelp')}>
            <Toggle
              label={t('settings.discordEnabled')}
              checked={draft.discordEnabled}
              onChange={discordEnabled => update({discordEnabled})}
            />
          </Row>

          <Row
            label={t('settings.discordGameIcons')}
            help={t('settings.discordGameIconsHelp')}>
            <Toggle
              label={t('settings.discordGameIcons')}
              checked={draft.discordGameIcons}
              onChange={discordGameIcons => update({discordGameIcons})}
            />
          </Row>

          <Row
            label={t('settings.discordCustomGames')}
            stack
            help={
              <>
                {tNode('settings.discordCustomGamesHelp', {
                  format: <code>Display Name | match1, match2</code>,
                })}
              </>
            }>
            <TextArea
              label={t('settings.discordCustomGames')}
              rows={4}
              value={draft.discordCustomGames}
              placeholder={t('settings.discordCustomGamesPlaceholder')}
              onChange={discordCustomGames => update({discordCustomGames})}
            />
          </Row>

          <Row
            label={t('settings.discordClientId')}
            help={t('settings.discordClientIdHelp')}>
            <TextField
              label={t('settings.discordClientId')}
              wide
              mono
              value={draft.discordClientId}
              placeholder={t('settings.discordClientIdPlaceholder')}
              onChange={discordClientId => update({discordClientId})}
            />
          </Row>
        </Card>

        {/* ── Overlay ───────────────────────────────────────────── */}
        <Card id="overlay" title={t('settings.secOverlay')} register={register('overlay')}>
          <Row
            label={t('settings.keyOverlay')}
            help={t('settings.keyOverlayHelp')}
            note={keyOverlayNote(status.key_overlay ?? null, draft.keyOverlay)}>
            <Toggle
              label={t('settings.keyOverlay')}
              checked={draft.keyOverlay}
              onChange={keyOverlay => {
                update({keyOverlay});
                void persistNow({overlay: {keys: keyOverlay}}, () => undefined, t('settings.errSaveOverlay'));
              }}
            />
          </Row>

          <Row label={t('settings.keyOverlayPosition')} help={t('settings.keyOverlayPositionHelp')}>
            <div className="key-pair">
              <Select
                label={t('settings.keyOverlayPosition')}
                value={draft.keyOverlayPosition}
                onChange={keyOverlayPosition => {
                  update({keyOverlayPosition});
                  void persistNow(
                    {overlay: {keys_position: keyOverlayPosition}},
                    () => undefined,
                    t('settings.errSaveOverlay'),
                  );
                }}
                options={[
                  ['bottom-left', t('settings.overlayBottomLeft')],
                  ['bottom-center', t('settings.overlayBottomCenter')],
                  ['bottom-right', t('settings.overlayBottomRight')],
                  ['top-left', t('settings.overlayTopLeft')],
                  ['top-right', t('settings.overlayTopRight')],
                  ['custom', t('settings.overlayCustom')],
                ]}
              />
              <button
                type="button"
                className={status.key_overlay?.moving ? 'btn btn-sm' : 'btn btn-quiet btn-sm'}
                disabled={!status.key_overlay?.running}
                onClick={() => {
                  const moving = Boolean(status.key_overlay?.moving);
                  void api
                    .moveKeyOverlay(!moving)
                    .then(async () => {
                      if (!moving) return;
                      // The daemon saved where it was dropped. Take that into
                      // the draft and the baseline, or Save would put it back.
                      const fresh = await api.getConfig();
                      const position = String(fresh.overlay?.keys_position ?? draft.keyOverlayPosition);
                      update({keyOverlayPosition: position});
                      setBaseline(prev => {
                        const merged = JSON.parse(prev) as Record<string, Record<string, unknown>>;
                        merged.overlay = {...(merged.overlay ?? {}), keys_position: position};
                        return JSON.stringify(merged);
                      });
                    })
                    .catch(err => fail(t('settings.errSaveOverlay'), err));
                }}>
                {status.key_overlay?.moving ? t('settings.overlayDoneMoving') : t('settings.overlayMove')}
              </button>
            </div>
          </Row>

          <Row label={t('settings.keyOverlaySize')}>
            <Slider
              label={t('settings.keyOverlaySize')}
              value={draft.keyOverlayScale}
              min={50}
              max={200}
              step={10}
              onChange={keyOverlayScale => update({keyOverlayScale})}
              format={v => `${v}%`}
            />
          </Row>

          <Row label={t('settings.keyOverlayLayout')} help={t('settings.keyOverlayLayoutHelp')}>
            <Select
              label={t('settings.keyOverlayLayout')}
              value={draft.keyOverlayLayout}
              onChange={keyOverlayLayout => {
                update({keyOverlayLayout});
                void persistNow(
                  {overlay: {keys_layout: keyOverlayLayout}},
                  () => undefined,
                  t('settings.errSaveOverlay'),
                );
              }}
              options={[
                ['azerty', t('settings.overlayLayoutAzerty')],
                ['qwerty', t('settings.overlayLayoutQwerty')],
              ]}
            />
          </Row>

          <Row label={t('settings.keyOverlayKey')} help={t('settings.keyOverlayKeyHelp')}>
            <div className="key-pair">
              <KeyCapture
                value={draft.keyOverlayKey}
                onUnsupported={() =>
                  notify({kind: 'error', title: t('settings.keyUnsupported'), tone: 'error', holdMs: 4000})
                }
                onCapture={keyOverlayKey => {
                  update({keyOverlayKey});
                  void persistNow(
                    {hotkeys: {key_overlay: keyOverlayKey}},
                    () => say(t('settings.keyOverlayKeyNow', {key: keyOverlayKey})),
                    t('settings.errSaveKey'),
                  );
                }}
              />
              {draft.keyOverlayKey ? (
                <button
                  type="button"
                  className="btn btn-quiet btn-sm"
                  onClick={() => {
                    update({keyOverlayKey: ''});
                    void persistNow({hotkeys: {key_overlay: ''}}, () => undefined, t('settings.errSaveKey'));
                  }}>
                  {t('settings.clearKey')}
                </button>
              ) : null}
            </div>
          </Row>

          <Row label={t('settings.keyOverlayInClips')} help={t('settings.keyOverlayInClipsHelp')}>
            <Toggle
              label={t('settings.keyOverlayInClips')}
              checked={draft.keyOverlayInClips}
              onChange={keyOverlayInClips => update({keyOverlayInClips})}
            />
          </Row>
        </Card>

        {/* ── Appearance ────────────────────────────────────────── */}
        <Card id="appearance" title={t('settings.secAppearance')} register={register('appearance')}>
          <Row label={t('settings.language')} help={t('settings.languageHelp')}>
            <Select
              label={t('settings.language')}
              value={currentLocale()}
              onChange={name => setLocale(name as LocaleName)}
              options={availableLocales().map(name => [name, LOCALE_LABELS[name]] as [string, string])}
            />
          </Row>

          <Row
            label={t('settings.accent')}
            help={t('settings.accentHelp')}>
            <div className="swatches">
              {ACCENT_NAMES.map(name => (
                <button
                  key={name}
                  type="button"
                  className="swatch"
                  data-active={accent === name || undefined}
                  style={{background: ACCENTS[name].base}}
                  title={t(`accents.${name}`)}
                  aria-label={t('settings.accentAria', {name: t(`accents.${name}`)})}
                  aria-pressed={accent === name}
                  onClick={() => choose(name)}>
                  {accent === name ? <IconCheck size={13} /> : null}
                </button>
              ))}
              <button
                type="button"
                className="swatch swatch-custom"
                data-active={accent === 'custom' || undefined}
                // The seed's own derived accent once one is saved, so the
                // swatch shows the colour it would apply rather than a
                // permanent rainbow that says nothing about the choice.
                style={customBase ? {background: customBase} : undefined}
                title={t('accents.customTitle')}
                aria-label={t('accents.customTitle')}
                aria-pressed={accent === 'custom'}
                onClick={() => setPicking(true)}>
                {accent === 'custom' ? <IconCheck size={13} /> : <IconPlus size={13} />}
              </button>
            </div>
          </Row>

          <Row
            label={t('settings.effects')}
            note={effectsNote(effects) ? {text: effectsNote(effects)} : null}
            help={t('settings.effectsHelp')}>
            <Select
              label={t('settings.effects')}
              value={effects}
              onChange={mode => setEffectsMode(mode as EffectsMode)}
              options={EFFECTS_MODES.map(m => [m, t(`settings.effects${m[0].toUpperCase()}${m.slice(1)}`)] as [string, string])}
            />
          </Row>
        </Card>

        {/* ── Advanced ──────────────────────────────────────────── */}
        <Card id="advanced" title={t('settings.secAdvanced')} register={register('advanced')}>
          <Row
            label={t('settings.gsrArgs')}
            stack
            help={
              <>
                {tNode('settings.gsrArgsHelp', {
                  example: <code>-k hevc -bm cbr -q 20000 -fm cfr</code>,
                })}
              </>
            }>
            <TextField
              label={t('settings.gsrArgs')}
              wide
              mono
              value={draft.gsrArgs}
              placeholder="-k hevc -bm cbr -q 20000 -fm cfr"
              onChange={gsrArgs => update({gsrArgs})}
            />
          </Row>

          <Row
            label={t('settings.checkForUpdates')}
            help={t('settings.checkForUpdatesHelp')}>
            <Toggle
              label={t('settings.checkForUpdates')}
              checked={draft.checkForUpdates}
              onChange={checkForUpdates => update({checkForUpdates})}
            />
          </Row>

          <Row label={t('settings.checkNow')} help={t('settings.checkNowHelp')}>
            <button
              type="button"
              className="btn btn-quiet btn-sm"
              disabled={checkingUpdate}
              onClick={() => {
                setCheckingUpdate(true);
                void api
                  .checkUpdate()
                  .then(result => {
                    const info = result as {update?: {version?: string} | null};
                    say(
                      info?.update?.version
                        ? t('settings.updateAvailable', {version: info.update.version})
                        : t('settings.upToDate'),
                    );
                  })
                  .catch(err => fail(t('settings.errCheckUpdate'), err))
                  .finally(() => setCheckingUpdate(false));
              }}>
              {checkingUpdate ? t('settings.checking') : t('settings.checkNow')}
            </button>
          </Row>
        </Card>
      </div>

      <div className="save-bar" data-dirty={dirty || undefined}>
        <span className="save-state">
          {dirty ? (
            t('settings.unsaved')
          ) : (
            <>
              <IconCheck size={13} /> {t('settings.allSaved')}
            </>
          )}
        </span>
        <button type="button" className="btn btn-quiet btn-sm" onClick={revert} disabled={!dirty || saving}>
          {t('settings.discard')}
        </button>
        <button type="button" className="btn btn-sm" onClick={() => void save()} disabled={!dirty || saving}>
          {saving ? t('settings.saving') : t('settings.saveSettings')}
        </button>
      </div>

      <Modal
        open={wfMicPrompt}
        title={t('settings.wfMicTitle')}
        onClose={() => setWfMicPrompt(false)}>
        <p>{t('settings.wfMicBody')}</p>
        <div className="choice-list">
          <button
            type="button"
            className="choice"
            onClick={() => {
              setWfMicPrompt(false);
              void setMic(true, 'backend_fallback');
            }}>
            <b>{t('settings.wfMicChoiceBoth')}</b>
            <span>{t('settings.wfMicChoiceBothBody')}</span>
          </button>
          <button
            type="button"
            className="choice"
            onClick={() => {
              setWfMicPrompt(false);
              void setMic(true, 'mic_only');
            }}>
            <b>{t('settings.wfMicChoiceMic')}</b>
            <span>{t('settings.wfMicChoiceMicBody')}</span>
          </button>
        </div>
      </Modal>

      <Modal
        open={restartNeeded}
        title={t('settings.restartTitle')}
        onClose={() => setRestartNeeded(false)}
        footer={
          <button type="button" className="btn" onClick={() => setRestartNeeded(false)}>
            {t('common.gotIt')}
          </button>
        }>
        <p>{t('settings.restartBody')}</p>
      </Modal>

      <AccentPicker
        open={picking}
        initial={seed}
        onCancel={() => setPicking(false)}
        onConfirm={next => {
          chooseCustom(next);
          setPicking(false);
        }}
      />
    </div>
  );
}

function Card({
  id,
  title,
  register,
  children,
}: {
  id: string;
  title: string;
  register: (node: HTMLElement | null) => void;
  children: React.ReactNode;
}) {
  return (
    <section className="settings-card" id={`settings-${id}`} ref={register} aria-label={title}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function ClipPresets({
  presets,
  onChange,
  onUnsupported,
}: {
  presets: ClipPreset[];
  onChange: (next: ClipPreset[]) => void;
  onUnsupported: () => void;
}) {
  const patch = (uid: string, values: Partial<ClipPreset>) =>
    onChange(presets.map(p => (p.uid === uid ? {...p, ...values} : p)));

  return (
    <div className="presets">
      {presets.map(preset => (
        <div className="preset-row" key={preset.uid}>
          <KeyCapture
            compact
            value={preset.key}
            onUnsupported={onUnsupported}
            onCapture={key => patch(preset.uid, {key})}
          />
          <input
            className="text-input preset-duration"
            type="number"
            min={5}
            max={600}
            step={5}
            value={preset.duration}
            aria-label={t('settings.presetDuration')}
            onChange={e => patch(preset.uid, {duration: Number(e.target.value)})}
          />
          <span className="preset-unit mono">s</span>
          <button
            type="button"
            className="preset-remove"
            title={t('settings.presetRemove')}
            aria-label={t('settings.presetRemove')}
            onClick={() => onChange(presets.filter(p => p.uid !== preset.uid))}>
            <IconClose size={12} />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn btn-quiet btn-sm"
        onClick={() => onChange([...presets, newClipPreset()])}>
        {t('settings.presetAdd')}
      </button>
    </div>
  );
}

/**
 * A saved value the backend is not listing right now is still a saved value.
 * Dropping it to Auto here wrote display=null on the next save and destroyed a
 * monitor set by hand, which is the only way to reach one gpu-screen-recorder
 * will not enumerate (#160). The audio pickers below do the same.
 */
function resolutionOptions(): Array<[string, string]> {
  return RESOLUTION_PRESETS.map(([value, key]) => [
    value,
    value ? key : t('settings.resolutionAuto'),
  ]);
}

function displayOptions(draft: Draft, info: DisplayInfo): Array<[string, string]> {
  const listed = usableDisplays(info);
  const options: Array<[string, string]> = [
    ['', t('settings.displayAuto')],
    ...listed.map(d => [d.id, d.label || d.id] as [string, string]),
  ];
  if (draft.display && !listed.some(d => d.id === draft.display)) {
    options.push([draft.display, t('settings.savedOption', {id: draft.display})]);
  }
  return options;
}

function displayNote(draft: Draft, info: DisplayInfo): RowNote {
  const listed = usableDisplays(info);
  if (draft.display && !listed.some(d => d.id === draft.display)) {
    return {
      text: t('settings.displayMissing', {id: draft.display}),
      tone: 'warning',
    };
  }
  if (info.warning) return {text: t('settings.displayWarning', {warning: info.warning}), tone: 'warning'};
  if (!listed.length) {
    return {text: t('settings.displayNone'), tone: 'warning'};
  }
  return {text: t('settings.displayHelp')};
}

/**
 * Defence in depth: drop anything whose id or label reads like a recorder
 * diagnostic. The backend filters these already, but a new error format
 * slipping through should show "no displays" rather than an option that
 * breaks recording when picked.
 */
function usableDisplays(info: DisplayInfo) {
  const looksLikeError = (value: string | undefined) => {
    const v = String(value || '').toLowerCase();
    return (
      v.startsWith('gsr error') ||
      v.startsWith('error:') ||
      v.includes('for_each_active_monitor') ||
      v.includes('failed to open')
    );
  };
  return (info.displays ?? []).filter(d => !(looksLikeError(d.id) || looksLikeError(d.label)));
}

function sourceKind(source: AudioSource): string {
  if (source.kind) return source.kind;
  const id = source.id || '';
  if (id === 'default_input' || (id.startsWith('device:') && !id.endsWith('.monitor'))) return 'input';
  if (id.startsWith('app:') || id.startsWith('app-inverse:')) return 'app';
  return 'monitor';
}

function groupedSourceOptions(selected: string, sources: AudioSource[]) {
  const list = sources.length ? sources : [{id: 'default_output', label: t('settings.defaultOutput')}];
  const groups: Array<{group: string; options: Array<[string, string]>}> = [];
  const kinds: Array<[string, string]> = [
    ['monitor', t('settings.groupDesktopAudio')],
    ['input', t('settings.groupMicrophones')],
    ['app', t('settings.groupApplications')],
  ];
  for (const [kind, label] of kinds) {
    const members = list.filter(s => sourceKind(s) === kind);
    if (members.length) {
      groups.push({group: label, options: members.map(s => [s.id, s.label || s.id])});
    }
  }
  const known = new Set(kinds.map(([kind]) => kind));
  const rest = list.filter(s => !known.has(sourceKind(s)));
  if (rest.length) groups.push({group: t('settings.groupOther'), options: rest.map(s => [s.id, s.label || s.id])});
  if (selected && !list.some(s => s.id === selected)) {
    groups.push({group: t('settings.groupSaved'), options: [[selected, t('settings.savedOption', {id: selected})]]});
  }
  return groups;
}

function micSourceOptions(selected: string, sources: AudioSource[]): Array<[string, string]> {
  const inputs = sources.filter(s => sourceKind(s) === 'input');
  if (!inputs.some(s => s.id === 'default_input')) {
    inputs.unshift({id: 'default_input', label: t('settings.defaultInput')});
  }
  const options = inputs.map(s => [s.id, s.label || s.id] as [string, string]);
  if (selected && !inputs.some(s => s.id === selected)) {
    options.push([selected, t('settings.savedOption', {id: selected})]);
  }
  return options;
}

function desktopSourceNote(
  draft: Draft,
  info: {sources: AudioSource[]; warning?: string | null},
): RowNote {
  const source = info.sources.find(s => s.id === draft.desktopSource);
  if (source && sourceKind(source) === 'input') {
    return {
      text: t('settings.desktopSourceIsMic'),
      tone: 'warning',
    };
  }
  if (draft.desktopSource && info.sources.length && !source) {
    return {
      text:
        info.warning || t('settings.desktopSourceMissing'),
      tone: 'warning',
    };
  }
  return {text: t('settings.desktopSourceHelp')};
}

function clipNameNote(draft: Draft): RowNote | null {
  const template = draft.clipNameTemplate.trim();
  if (!template) return null;
  const name = renderClipName(template, 4, 'Overwatch-2', new Date());
  return name
    ? {text: t('settings.clipFilenameNext', {name}), tone: 'accent'}
    : {text: t('settings.clipFilenameEmpty'), tone: 'warning'};
}

/** Why the overlay is not on screen, when it should be. */
function keyOverlayNote(state: KeyOverlayState | null, wanted: boolean): RowNote | undefined {
  if (!wanted || !state?.error) return undefined;
  const text = {
    'no-layer-shell': t('settings.overlayNoLayerShell'),
    'no-gtk': t('settings.overlayNoGtk'),
    'no-display': t('settings.overlayNoDisplay'),
    crashed: t('settings.overlayCrashed'),
  }[state.error];
  return {text, tone: 'warning'};
}
