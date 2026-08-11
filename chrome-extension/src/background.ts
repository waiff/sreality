/* Background service worker. The single context that talks to the
 * FastAPI service — host_permissions covers the API origin and the
 * call doesn't have to go through sreality.cz's CORS. Content scripts
 * post messages here; we route them to the api helpers. */

import {
  addNote,
  addPipelineCard,
  addToCollection,
  createEstimation,
  deleteNote,
  getBillingMe,
  getEstimation,
  listCollections,
  listNotes,
  listPipelineStages,
  lookupListings,
  movePipelineCard,
  patchScenario,
  removeFromCollection,
  removePipelineCard,
  updateNote,
} from './api';
import { getAuthState, refreshIfSignedIn, signInWithGoogle, signOut } from './auth';
import { PORTALS } from './portals';
import type { ApiMessage, ApiResult, RouteMessage } from './types';

/* MV3 service-worker eviction kills any in-memory auto-refresh timer, so the
 * lazy per-request refresh (api.ts's getAccessToken) is backstopped by this
 * periodic tick — a fresh SW instance still catches a session that's about
 * to expire even with no extension UI open. ~30 min per the Wave 1 design
 * (the access token TTL is on the order of an hour). */
const REFRESH_ALARM = 'session-refresh';

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: 30 });
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: 30 });
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === REFRESH_ALARM) void refreshIfSignedIn();
});

/* Several portals (sreality's Next.js app confirmed live; others are React/Vue
 * SPAs too) navigate between listings via the History API, not a fresh document
 * load — the only thing MV3's static content_scripts re-inject on. Without this,
 * the panel/overlay only ever reflects whatever URL was current when the script
 * first loaded, until the operator forces a real reload. webNavigation fires
 * uniformly for any router a portal uses, so no per-portal handling is needed;
 * the host filter is the same PORTALS registry the content script uses, so a
 * new portal picks this up automatically. */
const ROUTE_FILTER = { url: PORTALS.flatMap((p) => p.hosts).map((host) => ({ hostEquals: host })) };

chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (details.frameId !== 0) return;
  const message: RouteMessage = { type: 'route_changed', url: details.url };
  chrome.tabs.sendMessage(details.tabId, message).catch(() => {
    /* Tab navigated away again / no listener yet — nothing to recover. */
  });
}, ROUTE_FILTER);

chrome.runtime.onMessage.addListener(
  (
    message: ApiMessage,
    _sender,
    sendResponse: (response: ApiResult<unknown>) => void,
  ) => {
    handleMessage(message)
      .then(sendResponse)
      .catch((err: unknown) => {
        sendResponse({
          ok: false,
          status: 0,
          detail:
            err instanceof Error
              ? err.message
              : `background error: ${String(err)}`,
        });
      });
    /* Return true to keep the message channel open until sendResponse
     * fires asynchronously — required for the Promise.then path. */
    return true;
  },
);

async function handleMessage(
  message: ApiMessage,
): Promise<ApiResult<unknown>> {
  switch (message.type) {
    case 'lookup_listings':
      return lookupListings(message.items);
    case 'patch_scenario':
      return patchScenario(message.run_id, message.body);
    case 'create_estimation':
      return createEstimation(message.url);
    case 'get_estimation':
      return getEstimation(message.run_id);
    case 'add_pipeline_card':
      return addPipelineCard(message.property_id);
    case 'remove_pipeline_card':
      return removePipelineCard(message.property_id);
    case 'move_pipeline_card':
      return movePipelineCard(message.property_id, message.stage_id);
    case 'list_pipeline_stages':
      return listPipelineStages();
    case 'list_collections':
      return listCollections();
    case 'add_to_collection':
      return addToCollection(message.collection_id, message.property_id);
    case 'remove_from_collection':
      return removeFromCollection(message.collection_id, message.property_id);
    case 'list_notes':
      return listNotes(message.property_id);
    case 'add_note':
      return addNote(message.property_id, message.body, message.origin_listing_ref_id);
    case 'update_note':
      return updateNote(message.property_id, message.note_id, message.body);
    case 'delete_note':
      return deleteNote(message.property_id, message.note_id);
    case 'sign_in': {
      const res = await signInWithGoogle();
      return res.ok
        ? { ok: true, data: undefined }
        : { ok: false, status: 0, detail: res.detail };
    }
    case 'sign_out':
      await signOut();
      return { ok: true, data: undefined };
    case 'get_auth_state':
      return { ok: true, data: await getAuthState() };
    case 'get_billing':
      return getBillingMe();
  }
}
