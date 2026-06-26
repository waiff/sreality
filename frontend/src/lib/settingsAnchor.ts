/* One stable DOM anchor per Settings knob, so any surface can deep-link straight to it
 * — e.g. a dedup decision's auditability breakdown links to the exact threshold it was
 * judged against: /settings#setting-dedup_cosine_haiku_min. Keep the scheme in ONE place
 * so the producer (the link) and the consumer (the Settings row + hash-scroll) never
 * drift. */

export const SETTING_ANCHOR_PREFIX = 'setting-';

export const settingAnchorId = (key: string): string =>
  `${SETTING_ANCHOR_PREFIX}${key}`;

export const settingsDeepLink = (key: string): string =>
  `/settings#${settingAnchorId(key)}`;

/* True when a URL hash targets ANY per-setting anchor — the dedup-engine Settings
 * section uses this to force itself open so the targeted row is in the DOM to scroll to.
 * (Every breakdown deep-link points at a dedup-engine knob, so the section owns them.) */
export const hashTargetsSetting = (hash: string): boolean =>
  hash.startsWith(`#${SETTING_ANCHOR_PREFIX}`);

export const settingKeyFromHash = (hash: string): string | null =>
  hashTargetsSetting(hash) ? hash.slice(`#${SETTING_ANCHOR_PREFIX}`.length) : null;
