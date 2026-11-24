import {api} from './api';
import {copyToClipboard} from './clipboard';
import type {Clip, UploadServers} from './types';
import type {IslandEvent} from '../state/store';
import {t} from './i18n';

/**
 * Copy a clip's share link, saying plainly when the link only works on this
 * network. A LAN address looks identical to a real share link right up until
 * a friend cannot open it (#105).
 *
 * `onManualCopy` is the fallback for windows with no working clipboard: the
 * link has to be put somewhere the user can select it by hand.
 */
export async function copyShareLink(
  clip: Clip,
  notify: (event: Omit<IslandEvent, 'id'>) => void,
  onManualCopy: (url: string) => void,
  servers?: UploadServers | null,
): Promise<void> {
  if (!clip.share_url) {
    notify({
      kind: 'error',
      title: t('card.noShareLinkYet'),
      tone: 'error',
      holdMs: 4000,
    });
    return;
  }
  if (clip.cloud) {
    await copyCloudLink(clip, notify, onManualCopy);
    return;
  }
  if (!(await copyToClipboard(clip.share_url))) {
    onManualCopy(clip.share_url);
    return;
  }
  if (clip.share_is_public === false) {
    notify({
      kind: 'info',
      title: t('card.linkCopiedLocal'),
      // Two ways to a link that works anywhere: put the clip on a media
      // server, or bring the tunnel up. Name the server when there is one.
      detail: activeServerName(servers)
        ? t('card.linkCopiedLocalUpload', {server: activeServerName(servers)})
        : t('card.linkCopiedLocalDetail'),
      tone: 'neutral',
      holdMs: 8000,
    });
  } else {
    notify({kind: 'info', title: t('card.shareLinkCopied'), tone: 'accent', holdMs: 3000});
  }
}

/**
 * What the Share button does. By default it copies the link; with
 * `sharing.share_discord_file` it copies a Discord-sized copy of the clip,
 * which can take a while the first time because it has to be encoded.
 */
export async function shareClip(
  clip: Clip,
  discordFile: boolean,
  notify: (event: Omit<IslandEvent, 'id'>) => void,
  onManualCopy: (url: string) => void,
  servers?: UploadServers | null,
): Promise<void> {
  // A cloud clip has no file here to make a Discord copy from.
  if (!discordFile || (clip.cloud && !clip.cloud.local)) {
    await copyShareLink(clip, notify, onManualCopy, servers);
    return;
  }
  notify({kind: 'info', title: t('card.discordPreparing'), tone: 'neutral', holdMs: 2500});
  try {
    await api.copyClipFile(clip.slug, {discord: true});
    notify({
      kind: 'info',
      title: t('card.discordCopied'),
      detail: t('card.discordCopiedDetail'),
      tone: 'accent',
      holdMs: 4000,
    });
  } catch (err) {
    notify({
      kind: 'error',
      title: t('card.errDiscordCopy'),
      detail: (err as Error).message,
      tone: 'error',
      holdMs: 7000,
    });
  }
}

/** The Share button's tooltip, which also has to say that right-click exists. */
export function shareButtonLabel(discordFile: boolean): string {
  return t('card.shareTitle', {action: discordFile ? t('card.copyForDiscord') : t('card.copyShareLink')});
}

/** The media server uploads go to, by name, or '' when none is set up. */
function activeServerName(servers?: UploadServers | null): string {
  if (!servers?.servers.length) return '';
  return (servers.servers.find(s => s.id === servers.active_server) ?? servers.servers[0]).name;
}

/**
 * Copy an uploaded clip's link, checked first. The daemon asks the server for
 * a byte of the file (two seconds at most) and, when the server does not
 * answer but the file is still on this PC, hands back the link this PC serves
 * instead. A dead link copied without a word is the failure this prevents.
 */
async function copyCloudLink(
  clip: Clip,
  notify: (event: Omit<IslandEvent, 'id'>) => void,
  onManualCopy: (url: string) => void,
): Promise<void> {
  const server = clip.cloud?.server_name ?? '';
  let resolved: Awaited<ReturnType<typeof api.resolveShare>> | null = null;
  try {
    resolved = await api.resolveShare(clip.slug);
  } catch (err) {
    console.debug('Checking the media server failed, copying its link as is', err);
  }
  const url = resolved?.share_url || clip.share_url;
  if (!(await copyToClipboard(url))) {
    onManualCopy(url);
    return;
  }
  const name = resolved?.server_name || server;
  if (resolved?.via === 'fallback') {
    notify({
      kind: 'info',
      title: t('cloud.unreachable', {server: name}),
      detail: resolved.share_is_public ? t('cloud.fallbackTunnel') : t('cloud.fallbackLocal'),
      tone: 'neutral',
      holdMs: 8000,
    });
  } else if (resolved?.via === 'server_down') {
    notify({
      kind: 'error',
      title: t('cloud.unreachable', {server: name}),
      detail: t('cloud.serverDownCopied'),
      tone: 'error',
      holdMs: 8000,
    });
  } else {
    notify({kind: 'info', title: t('cloud.linkCopied', {server: name}), tone: 'accent', holdMs: 3000});
  }
}
