// Bot Microscope — Chrome Extension Service Worker (Manifest V3)
// Relays local server calls from content script to bypass page CSP & Mixed Content restrictions,
// and coordinates atomic backend persistence + active tab auto-close on manual labeling.

const EXT_VERSION = (function () {
  try {
    if (typeof chrome !== "undefined" && chrome.runtime && typeof chrome.runtime.getManifest === "function") {
      const m = chrome.runtime.getManifest();
      if (m && m.version) return m.version;
    }
  } catch (_e) {}
  return "3.0.0";
})();

if (typeof process === "undefined") {
  console.info(`[Bot Microscope Service Worker] v${EXT_VERSION} active`);
}

const PRIMARY_HOST = "http://127.0.0.1:8000";
const FALLBACK_HOST = "http://localhost:8000";
const NON_PROFILE_PATHS = new Set([
  "explore",
  "direct",
  "reels",
  "reel",
  "accounts",
  "p",
  "stories",
  "popular",
  "locations",
  "lite",
  "web",
  "threads",
  "meta",
  "about",
  "legal",
  "privacy",
  "terms",
  "help",
  "api",
  "blog",
  "jobs",
  "press",
  "developers",
  "challenge",
  "directory",
  "session"
]);

let activeArtistCache = "";
const mbCache = { data: null, fetchedAt: 0, promise: null };
let mbCacheGen = 0;
const cpCache = new Map();
const cpCacheGen = new Map();
let tabCountBroadcastTimer = null;

function invalidateCaches(artist) {
  mbCacheGen += 1;
  mbCache.data = null;
  mbCache.fetchedAt = 0;
  mbCache.promise = null;
  const cleanLow = String(artist || "").trim().replace(/^@/, "").toLowerCase();
  if (cleanLow) {
    cpCacheGen.set(cleanLow, (cpCacheGen.get(cleanLow) || 0) + 1);
    cpCache.delete(cleanLow);
  } else {
    for (const k of cpCache.keys()) {
      cpCacheGen.set(k, (cpCacheGen.get(k) || 0) + 1);
    }
    cpCacheGen.set("", (cpCacheGen.get("") || 0) + 1);
    cpCache.clear();
  }
}

function extractIgHandleFromUrl(urlStr) {
  if (typeof urlStr !== "string" || !urlStr) {
    return { handle: "", isFollowersModalPath: false };
  }
  const m = urlStr.match(/^https?:\/\/(?:www\.)?instagram\.com\/([^/?#]+)(?:\/([^/?#]+))?/i);
  if (!m || !m[1]) return { handle: "", isFollowersModalPath: false };
  const handle = m[1].replace(/^@/, "").trim();
  const low = handle.toLowerCase();
  if (!handle || NON_PROFILE_PATHS.has(low)) {
    return { handle: "", isFollowersModalPath: false };
  }
  const sub = String(m[2] || "").toLowerCase();
  return {
    handle,
    isFollowersModalPath: sub === "followers" || sub === "following"
  };
}

function queryOpenInstagramTabs(artist, cb) {
  if (
    typeof chrome === "undefined" ||
    !chrome.tabs ||
    typeof chrome.tabs.query !== "function"
  ) {
    cb({ tabs: [], openFollowerTabs: 0 });
    return;
  }
  const artistLow = String(artist || activeArtistCache || "").trim().replace(/^@/, "").toLowerCase();
  try {
    chrome.tabs.query({ url: "https://*.instagram.com/*" }, (tabs) => {
      void chrome.runtime.lastError;
      const list = Array.isArray(tabs) ? tabs : [];
      let openFollowerTabs = 0;
      for (const t of list) {
        if (!t || typeof t.url !== "string") continue;
        const parsed = extractIgHandleFromUrl(t.url);
        if (!parsed.handle || parsed.isFollowersModalPath) continue;
        if (artistLow && parsed.handle.toLowerCase() === artistLow) continue;
        openFollowerTabs += 1;
      }
      cb({ tabs: list, openFollowerTabs });
    });
  } catch (_e) {
    cb({ tabs: [], openFollowerTabs: 0 });
  }
}

function broadcastToInstagramTabs(action, payload, excludeTabId = null) {
  if (
    typeof chrome === "undefined" ||
    !chrome.tabs ||
    typeof chrome.tabs.query !== "function" ||
    typeof chrome.tabs.sendMessage !== "function"
  ) {
    return;
  }
  queryOpenInstagramTabs((payload && payload.artist) || activeArtistCache, ({ tabs, openFollowerTabs }) => {
    const fullPayload = Object.assign({}, payload || {}, { openFollowerTabs });
    for (const t of tabs) {
      if (!t || typeof t.id !== "number") continue;
      if (excludeTabId !== null && t.id === excludeTabId) continue;
      try {
        chrome.tabs.sendMessage(t.id, { action, payload: fullPayload }, () => {
          void chrome.runtime.lastError;
        });
      } catch (_e) {}
    }
  });
}

function scheduleTabCountBroadcast() {
  if (tabCountBroadcastTimer) {
    clearTimeout(tabCountBroadcastTimer);
  }
  tabCountBroadcastTimer = setTimeout(() => {
    tabCountBroadcastTimer = null;
    broadcastToInstagramTabs("BOTAUDIT_TABS_COUNT", { artist: activeArtistCache });
  }, 180);
  if (tabCountBroadcastTimer && typeof tabCountBroadcastTimer.unref === "function") {
    tabCountBroadcastTimer.unref();
  }
}

if (typeof chrome !== "undefined" && chrome.tabs && typeof chrome.tabs.sendMessage === "function") {
  if (chrome.tabs.onRemoved && typeof chrome.tabs.onRemoved.addListener === "function") {
    chrome.tabs.onRemoved.addListener(() => scheduleTabCountBroadcast());
  }
  if (chrome.tabs.onUpdated && typeof chrome.tabs.onUpdated.addListener === "function") {
    chrome.tabs.onUpdated.addListener((_tabId, changeInfo) => {
      if (changeInfo && (changeInfo.url || changeInfo.status === "complete")) {
        scheduleTabCountBroadcast();
      }
    });
  }
}

async function fetchWithFallback(path, options = {}) {
  try {
    const res = await fetch(`${PRIMARY_HOST}${path}`, options);
    return res;
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw err;
    }
    try {
      const res = await fetch(`${FALLBACK_HOST}${path}`, options);
      return res;
    } catch (_fallbackErr) {
      throw err;
    }
  }
}

function fetchWithTimeout(path, options = {}, timeoutMs = 2500) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const mergedOptions = Object.assign({}, options, { signal: controller.signal });
  return fetchWithFallback(path, mergedOptions).finally(() => clearTimeout(timer));
}

function closeSenderOrActiveTab(sender) {
  const removeById = (tabId) => {
    if (typeof tabId !== "number") return;
    try {
      chrome.tabs.remove(tabId, () => {
        void chrome.runtime.lastError;
      });
    } catch (_e) {
      // Ignore if tab already closed
    }
  };

  if (sender && sender.tab && typeof sender.tab.id === "number") {
    removeById(sender.tab.id);
    return;
  }

  try {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      void chrome.runtime.lastError;
      if (Array.isArray(tabs) && tabs[0] && typeof tabs[0].id === "number") {
        removeById(tabs[0].id);
      }
    });
  } catch (_e) {
    // Ignore query failure
  }
}

function openOrFocusAuditTab(targetUrl, onDone) {
  const fallbackCreate = () => {
    try {
      chrome.tabs.create({ url: targetUrl });
    } catch (_tabErr) {}
    if (onDone) onDone({ reusedTab: false });
  };

  if (
    typeof chrome === "undefined" ||
    !chrome.tabs ||
    typeof chrome.tabs.query !== "function" ||
    typeof chrome.tabs.update !== "function"
  ) {
    fallbackCreate();
    return;
  }

  try {
    chrome.tabs.query(
      { url: ["http://localhost:8000/*", "http://127.0.0.1:8000/*"] },
      (tabs) => {
        void chrome.runtime.lastError;
        if (Array.isArray(tabs) && tabs.length > 0 && typeof tabs[0].id === "number") {
          const existing = tabs[0];
          chrome.tabs.update(existing.id, { url: targetUrl, active: true }, () => {
            void chrome.runtime.lastError;
            if (
              chrome.windows &&
              typeof chrome.windows.update === "function" &&
              typeof existing.windowId === "number"
            ) {
              try {
                chrome.windows.update(existing.windowId, { focused: true }, () => {
                  void chrome.runtime.lastError;
                });
              } catch (_winErr) {}
            }
            if (onDone) onDone({ reusedTab: true });
          });
        } else {
          fallbackCreate();
        }
      }
    );
  } catch (_e) {
    fallbackCreate();
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.action) return false;

  if (message.action === "SEND_TO_AUDIT_APP") {
    const payload = message.payload || {};
    fetchWithFallback("/api/extension-import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(async (resp) => {
        if (!resp.ok) {
          let errDetail = `HTTP ${resp.status}`;
          try {
            const errJson = await resp.json();
            if (errJson && errJson.error) errDetail = errJson.error;
          } catch (_e) {}
          sendResponse({ ok: false, error: errDetail });
          return;
        }
        const data = await resp.json();
        const artist = encodeURIComponent(payload.artist || "");
        const targetUrl = `${FALLBACK_HOST}/?source=extension&artist=${artist}&_sync=${Date.now()}`;
        openOrFocusAuditTab(targetUrl, (tabInfo) => {
          sendResponse({
            ok: true,
            data,
            count: (payload.profiles || []).length,
            openedTab: true,
            reusedTab: Boolean(tabInfo && tabInfo.reusedTab)
          });
        });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err.message || err) });
      });
    return true; // Keep message channel open for async response
  }

  if (message.action === "GET_OPEN_TABS_COUNT") {
    const reqArtist = (message.payload && message.payload.artist) || message.artist || "";
    if (reqArtist && reqArtist !== "Audited_Account") {
      activeArtistCache = reqArtist;
    }
    queryOpenInstagramTabs(reqArtist || activeArtistCache, ({ tabs, openFollowerTabs }) => {
      sendResponse({
        ok: true,
        openFollowerTabs,
        totalInstagramTabs: tabs.length
      });
    });
    return true;
  }

  if (message.action === "MARK_PROFILE_AND_CLOSE") {
    const payload = message.payload || {};
    const artist = payload.artist || "Audited_Account";
    if (artist && artist !== "Audited_Account") {
      activeArtistCache = artist;
    }
    const profile = payload.profile || null;
    const labelEntry = payload.labelEntry || {};
    const shouldClose = payload.closeTab !== false;
    const hasLabelFollowup = Boolean(labelEntry && labelEntry.username);
    const startedAt = Date.now();
    const FAST_CLOSE_CAP_MS = 220;
    const MIN_GLOW_MS = 90;

    let settled = false;
    let fastCloseTimer = null;

    const finishResponse = (respPayload, closeTabNow) => {
      if (settled) return;
      settled = true;
      if (fastCloseTimer) {
        clearTimeout(fastCloseTimer);
        fastCloseTimer = null;
      }
      try {
        sendResponse(respPayload);
      } catch (_e) {
        // Port may already be closing
      }
      if (closeTabNow) {
        closeSenderOrActiveTab(sender);
      }
    };

    if (shouldClose) {
      fastCloseTimer = setTimeout(() => {
        finishResponse(
          {
            ok: true,
            savedServer: null,
            pendingServer: true,
            data: null,
            error: null,
            closedTab: true
          },
          true
        );
      }, FAST_CLOSE_CAP_MS);
    }

    const persistTask = (async () => {
      let savedServer = false;
      let cpData = null;
      let labelData = null;
      let errorMsg = null;

      try {
        // Task 1A: Persist observed profile numbers into artist checkpoint first
        // so that sync_sample_benchmarks() in step 1B immediately finds the profile's real metrics.
        if (profile && profile.username) {
          const cpOpts = {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              artist: artist,
              profiles: [profile],
              last_username: profile.username,
              skip_live_counts: true,
              defer_workers: hasLabelFollowup
            })
          };
          let cpResp = await fetchWithTimeout("/api/checkpoint", cpOpts, 8000).catch(() => null);
          if (cpResp && !cpResp.ok) {
            await new Promise((r) => setTimeout(r, 120));
            cpResp = await fetchWithTimeout("/api/checkpoint", cpOpts, 8000).catch(() => null);
          }
          if (cpResp && cpResp.ok) {
            cpData = await cpResp.json().catch(() => null);
          }
        }

        // Task 1B: Persist ground-truth label note (syncs label_notes.csv, verified_bots.csv, verified_users.csv)
        if (hasLabelFollowup) {
          const resp = await fetchWithTimeout(
            "/api/labels",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(labelEntry)
            },
            8000
          );
          labelData = await resp.json().catch(() => ({}));
          savedServer = Boolean(resp && resp.ok);
          if (!savedServer && labelData && labelData.error) {
            errorMsg = labelData.error;
          }
        }
      } catch (err) {
        savedServer = false;
        errorMsg = String(err && err.message ? err.message : err);
      }

      invalidateCaches(artist);
      const liveCounts = (labelData && labelData.live_counts) || (cpData && cpData.live_counts) || null;
      broadcastToInstagramTabs(
        "BOTAUDIT_LIVE_DELTA",
        {
          artist,
          username: labelEntry.username || (profile && profile.username) || "",
          label: labelEntry.label || "",
          profile,
          liveCounts,
          source: "mark",
          ts: Date.now()
        },
        sender && sender.tab && typeof sender.tab.id === "number" ? sender.tab.id : null
      );

      return {
        ok: true,
        savedServer,
        data: labelData,
        error: errorMsg,
        closedTab: shouldClose
      };
    })();

    persistTask.then(async (result) => {
      if (settled) return;
      if (shouldClose) {
        const elapsed = Date.now() - startedAt;
        if (elapsed < MIN_GLOW_MS) {
          await new Promise((r) => setTimeout(r, MIN_GLOW_MS - elapsed));
        }
      }
      finishResponse(result, shouldClose);
    });

    return true;
  }

  if (message.action === "CLOSE_CURRENT_TAB") {
    closeSenderOrActiveTab(sender);
    try {
      sendResponse({ ok: true });
    } catch (_e) {}
    return false;
  }

  if (message.action === "SAVE_LABEL_NOTE") {
    const labelPayload = message.payload || {};
    const lblArtist = labelPayload.artist || activeArtistCache || "";
    if (lblArtist && lblArtist !== "Audited_Account") {
      activeArtistCache = lblArtist;
    }
    fetchWithFallback("/api/labels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(labelPayload)
    })
      .then(async (resp) => {
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
          invalidateCaches(lblArtist);
          broadcastToInstagramTabs(
            "BOTAUDIT_LIVE_DELTA",
            {
              artist: lblArtist,
              username: labelPayload.username || "",
              label: labelPayload.label || "",
              profile: labelPayload.profile || null,
              liveCounts: (data && data.live_counts) || null,
              source: "inline_mark",
              ts: Date.now()
            },
            sender && sender.tab && typeof sender.tab.id === "number" ? sender.tab.id : null
          );
        }
        sendResponse({ ok: resp.ok, data });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err.message || err) });
      });
    return true;
  }

  if (message.action === "SAVE_CHECKPOINT") {
    const cpPayload = message.payload || {};
    const cpArtist = cpPayload.artist || activeArtistCache || "";
    if (cpArtist && cpArtist !== "Audited_Account" && !cpPayload.clear) {
      activeArtistCache = cpArtist;
    }
    fetchWithFallback("/api/checkpoint", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cpPayload)
    })
      .then(async (resp) => {
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
          invalidateCaches(cpArtist);
          if (!cpPayload.clear) {
            const profs = Array.isArray(cpPayload.profiles) ? cpPayload.profiles.slice(0, 25) : [];
            broadcastToInstagramTabs(
              "BOTAUDIT_LIVE_DELTA",
              {
                artist: cpArtist,
                profiles: profs,
                liveCounts: (data && data.live_counts) || null,
                source: "checkpoint",
                ts: Date.now()
              },
              sender && sender.tab && typeof sender.tab.id === "number" ? sender.tab.id : null
            );
          }
        }
        sendResponse({ ok: resp.ok, data });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err.message || err) });
      });
    return true;
  }

  if (message.action === "GET_CHECKPOINT") {
    const rawArtist = (message.payload && message.payload.artist) || message.artist || "";
    if (rawArtist && rawArtist !== "Audited_Account") {
      activeArtistCache = rawArtist;
    }
    const artistLow = String(rawArtist).trim().replace(/^@/, "").toLowerCase();
    const entry = cpCache.get(artistLow) || { data: null, fetchedAt: 0, promise: null };
    if (entry.data && Date.now() - entry.fetchedAt < 4000) {
      sendResponse({ ok: true, data: entry.data });
      return false;
    }
    const artist = encodeURIComponent(rawArtist);
    if (!entry.promise) {
      const reqGen = cpCacheGen.get(artistLow) || 0;
      entry.promise = fetchWithFallback(`/api/checkpoint?artist=${artist}`)
        .then(async (resp) => {
          if (!resp.ok) return { ok: false, status: resp.status };
          const data = await resp.json();
          if ((cpCacheGen.get(artistLow) || 0) === reqGen && cpCache.get(artistLow) === entry) {
            entry.data = data;
            entry.fetchedAt = Date.now();
          }
          return { ok: true, data };
        })
        .finally(() => {
          entry.promise = null;
        });
      cpCache.set(artistLow, entry);
    }
    entry.promise
      .then((res) => sendResponse(res))
      .catch((err) => {
        sendResponse({ ok: false, error: String(err.message || err) });
      });
    return true;
  }

  if (message.action === "GET_MEMORY_BANK") {
    if (mbCache.data && Date.now() - mbCache.fetchedAt < 10000) {
      sendResponse({ ok: true, data: mbCache.data });
      return false;
    }
    if (!mbCache.promise) {
      const reqGen = mbCacheGen;
      mbCache.promise = fetchWithFallback("/api/memory-bank")
        .then(async (resp) => {
          if (!resp.ok) return { ok: false, status: resp.status };
          const data = await resp.json();
          if (mbCacheGen === reqGen) {
            mbCache.data = data;
            mbCache.fetchedAt = Date.now();
          }
          return { ok: true, data };
        })
        .finally(() => {
          mbCache.promise = null;
        });
    }
    mbCache.promise
      .then((res) => sendResponse(res))
      .catch((err) => {
        sendResponse({ ok: false, error: String(err.message || err) });
      });
    return true;
  }

  if (message.action === "GET_LABELS") {
    fetchWithFallback("/api/labels")
      .then(async (resp) => {
        if (!resp.ok) {
          sendResponse({ ok: false, status: resp.status });
          return;
        }
        const data = await resp.json();
        sendResponse({ ok: true, data });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err.message || err) });
      });
    return true;
  }

  if (message.action === "GET_HISTORY") {
    fetchWithTimeout("/api/history", {}, 2500)
      .then(async (resp) => {
        if (!resp.ok) {
          sendResponse({ ok: false, status: resp.status });
          return;
        }
        const data = await resp.json();
        sendResponse({ ok: true, data });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err.message || err) });
      });
    return true;
  }

  return false;
});
