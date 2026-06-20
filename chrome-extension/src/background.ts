/* Background service worker. The single context that talks to the
 * FastAPI service — host_permissions covers the API origin and the
 * call doesn't have to go through sreality.cz's CORS. Content scripts
 * post messages here; we route them to the api helpers. */

import {
  addPipelineCard,
  addToCollection,
  createEstimation,
  getEstimation,
  listCollections,
  listPipelineStages,
  lookupListings,
  movePipelineCard,
  patchScenario,
  removeFromCollection,
  removePipelineCard,
} from './api';
import type { ApiMessage, ApiResult } from './types';

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
  }
}
