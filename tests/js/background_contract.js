// Contract harness for browser-extension/background.js (MARK_PROFILE_AND_CLOSE & CLOSE_CURRENT_TAB).
// Executed by pytest via tests/test_extension_hud_ux.py.
"use strict";

let registeredListener = null;
const removedTabs = [];
const createdTabs = [];
const fetchCalls = [];
let simulateOffline = false;
let mockFetchDelayMs = 0;

globalThis.chrome = {
  runtime: {
    lastError: null,
    onMessage: {
      addListener(fn) {
        registeredListener = fn;
      }
    }
  },
  tabs: {
    remove(tabId, cb) {
      removedTabs.push(tabId);
      if (cb) cb();
    },
    create(opts) {
      createdTabs.push(opts);
    },
    query(_q, cb) {
      cb([{ id: 999 }]);
    }
  }
};

globalThis.fetch = async (url, options = {}) => {
  fetchCalls.push({
    url,
    method: options.method || "GET",
    body: options.body ? JSON.parse(options.body) : null
  });
  if (mockFetchDelayMs > 0) {
    await new Promise((r) => setTimeout(r, mockFetchDelayMs));
  }
  if (simulateOffline) {
    throw new Error("Connection refused");
  }
  return {
    ok: true,
    status: 200,
    async json() {
      return { ok: true };
    }
  };
};

require("../../browser-extension/background.js");

function sendMessageToBg(message, sender) {
  return new Promise((resolve) => {
    registeredListener(message, sender, (resp) => resolve(resp));
  });
}

(async () => {
  const results = {};

  for (const [idx, label] of ["bot", "human", "unsure"].entries()) {
    fetchCalls.length = 0;
    removedTabs.length = 0;
    const tabId = 101 + idx;
    const resp = await sendMessageToBg(
      {
        action: "MARK_PROFILE_AND_CLOSE",
        payload: {
          artist: "target_alpha",
          profile: {
            schema_version: 3,
            artist: "target_alpha",
            username: "reea.404",
            has_profile_photo: true,
            account_private: true,
            post_count: 49,
            followers_count: 1515,
            following_count: 513,
            bio_length: 84,
            manual_label: label
          },
          labelEntry: {
            username: "reea.404",
            label: label,
            status: "available",
            labeled_on: "2026-11-07",
            labeler: "extension_hud"
          },
          closeTab: true
        }
      },
      { tab: { id: tabId } }
    );

    results[label] = {
      resp,
      fetchEndpoints: fetchCalls.map((c) => c.url),
      checkpointBody: fetchCalls[0] ? fetchCalls[0].body : null,
      labelBody: fetchCalls[1] ? fetchCalls[1].body : null,
      removedTabs: [...removedTabs]
    };
  }

  // Offline resilience: tab still closes even if local backend is unreachable
  simulateOffline = true;
  fetchCalls.length = 0;
  removedTabs.length = 0;
  const offlineResp = await sendMessageToBg(
    {
      action: "MARK_PROFILE_AND_CLOSE",
      payload: {
        artist: "target_alpha",
        profile: { username: "reea.404", post_count: 49 },
        labelEntry: { username: "reea.404", label: "human" },
        closeTab: true
      }
    },
    { tab: { id: 404 } }
  );
  results.offline = {
    resp: offlineResp,
    removedTabs: [...removedTabs]
  };

  // Slow-server resilience: tab still closes within fast-close cap (~220ms) while Service Worker finishes persisting
  simulateOffline = false;
  mockFetchDelayMs = 600;
  fetchCalls.length = 0;
  removedTabs.length = 0;
  const slowStarted = Date.now();
  const slowResp = await sendMessageToBg(
    {
      action: "MARK_PROFILE_AND_CLOSE",
      payload: {
        artist: "target_alpha",
        profile: { username: "slow_srv_user", post_count: 12, followers_count: 50, following_count: 400, bio_length: 0, has_profile_photo: true, account_private: false },
        labelEntry: { username: "slow_srv_user", label: "bot" },
        closeTab: true
      }
    },
    { tab: { id: 505 } }
  );
  const elapsedToCloseMs = Date.now() - slowStarted;
  const removedTabsDuringClose = [...removedTabs];
  await new Promise((r) => setTimeout(r, mockFetchDelayMs * 2 + 120));
  results.slowServer = {
    resp: slowResp,
    elapsedToCloseMs,
    removedTabsDuringClose,
    backgroundFetchEndpoints: fetchCalls.map((c) => c.url),
    backgroundCheckpointBody: fetchCalls[0] ? fetchCalls[0].body : null
  };

  console.log(JSON.stringify(results));
})();

