/**
 * Bot Microscope — Content Script, Visual HUD, Next Safe Scan Timer & Multi-Day Resume
 */
(function () {
  "use strict";
  if (window.__BOT_AUDIT_CONTENT_ACTIVE__) return;
  window.__BOT_AUDIT_CONTENT_ACTIVE__ = true;

  const LEGACY_KEY = "botaudit_collector_state_v2";
  const ACTIVE_ARTIST_KEY = "botaudit_active_artist_v3";
  const RECENT_CLICK_KEY = "botaudit_recent_click_v1";
  const RECENT_CLICKS_KEY = "botaudit_recent_clicks_v2";
  const RECENT_CLICK_TTL_MS = 30 * 60 * 1000;
  const LAST_EVENT_KEY = "botaudit_last_event_v1";
  const RATE_KEY = "botaudit_rate_timestamps_v2";
  const HOUR_MS = 3600 * 1000;
  const DAY_MS = 24 * HOUR_MS;
  const SEARCH_SLICES = "abcdefghijklmnopqrstuvwxyz0123456789".split("");
  // Automatic scrolling and search-letter jumping are OFF by default: the owner scrolls by hand
  // and the extension only reads what Instagram itself loads.
  const AUTOMATION_KEY = "botaudit_experimental_automation_v1";

  const EXT_VERSION = (function () {
    try {
      if (typeof chrome !== "undefined" && chrome.runtime && typeof chrome.runtime.getManifest === "function") {
        const m = chrome.runtime.getManifest();
        if (m && m.version) return m.version;
      }
    } catch (_e) {}
    return "3.0.0";
  })();

  function isAutomationEnabled() {
    try {
      return localStorage.getItem(AUTOMATION_KEY) === "1";
    } catch (_e) {
      return false;
    }
  }

  function setAutomationEnabled(on) {
    try {
      if (on) localStorage.setItem(AUTOMATION_KEY, "1");
      else localStorage.removeItem(AUTOMATION_KEY);
    } catch (_e) {
      // Ignore storage errors
    }
  }

  function fmtStat(v) {
    return typeof v === "number" ? v.toLocaleString() : "—";
  }

  function escapeHtml(raw) {
    return String(raw === null || raw === undefined ? "" : raw)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatHandleWithDigits(username) {
    const clean = String(username || "");
    return clean
      .split(/(\d+)/)
      .map((part) =>
        /^\d+$/.test(part)
          ? `<span class="botaudit-handle-digits">${escapeHtml(part)}</span>`
          : escapeHtml(part)
      )
      .join("");
  }

  function stripTransientUiFields(p) {
    if (!p || typeof p !== "object") return p;
    const copy = Object.assign({}, p);
    delete copy.avatar_url;
    delete copy.bio_preview;
    delete copy.gallery_thumbs;
    return copy;
  }

  function isTypingInEditableElement(target) {
    if (!target || typeof target !== "object") return false;
    const tag = String(target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (target.isContentEditable === true) return true;
    if (
      typeof target.closest === "function" &&
      target.closest('[contenteditable="true"], [role="textbox"], input, textarea, select')
    ) {
      return true;
    }
    return false;
  }

  function formatFollowRatioPill(followersCount, followingCount, flags, isRo) {
    if (typeof followersCount !== "number" || typeof followingCount !== "number") {
      return "";
    }
    const ratio = followingCount / Math.max(1, followersCount);
    const ratioText =
      ratio >= 10 ? `${Math.round(ratio).toLocaleString()}×` : `${ratio.toFixed(1)}×`;
    const isMass = Boolean(flags && flags.mass_following === true);
    const tone = isMass
      ? "is-danger"
      : followingCount >= 150 && ratio >= 4.0
        ? "is-warn"
        : ratio <= 2.5
          ? "is-ok"
          : "";
    const tip = isRo
      ? `Urmăriri / Urmăritori: ${followingCount.toLocaleString()} ÷ ${Math.max(1, followersCount).toLocaleString()}`
      : `Following / Followers: ${followingCount.toLocaleString()} ÷ ${Math.max(1, followersCount).toLocaleString()}`;
    return `<span class="botaudit-forensic-pill ${tone}" title="${escapeHtml(tip)}">⚖️ <b>${ratioText}</b> ${
      isRo ? "raport" : "ratio"
    }</span>`;
  }

  function applyScore(p, scored) {
    p.same_day_post_burst = scored.flags.same_day_post_burst;
    p.bot_score = scored.botScore;
    p.score_pts = scored.scorePts;
    p.raw_score_pts = scored.rawScorePts;
    p.max_score_pts = scored.maxScorePts;
    p.ratio_cluster_raw_pts = scored.ratioClusterRawPts;
    p.ratio_cluster_pts = scored.ratioClusterPts;
    p.ratio_cluster_capped = scored.ratioClusterCapped;
    p.verdict_tier = scored.verdictTier;
    p.bot_like = scored.botLike;
    p.score_conclusive = Boolean(scored.conclusive);
    p.score_complete = scored.complete;
    p.flags = scored.flags;
  }

  function resolveManualLabel(profile, manualLabelOverride) {
    if (manualLabelOverride !== undefined) {
      return String(manualLabelOverride || "").trim().toLowerCase();
    }
    const unameLow = String((profile && profile.username) || "").toLowerCase();
    const entry = unameLow ? state.manualLabelsMap.get(unameLow) : null;
    return String((entry && entry.label) || (profile && profile.manual_label) || "")
      .trim()
      .toLowerCase();
  }

  // Single source of truth: re-scores freshly so row badges and counters never drift,
  // counts clear passive hover profiles without opening tabs, and manual always overrides auto.
  function classifyProfileVerdict(profile, manualLabelOverride) {
    if (!profile || !profile.username) {
      return {
        decided: false,
        isBot: false,
        verdict: null,
        source: "unknown",
        overriddenAutoBot: false,
        overriddenAutoHuman: false
      };
    }
    const manualLabel = resolveManualLabel(profile, manualLabelOverride);
    const P = window.BotAuditParser;
    const scored = P ? P.scoreFollowerProfile(profile) : null;
    const hasFull = Boolean(P && P.isScorableProfile(profile));
    const hasHeader = Boolean(P && P.hasHeaderCounts(profile));
    const autoBotLike = scored
      ? scored.botLike
      : profile.bot_like === true || profile.bot_like === false
        ? profile.bot_like
        : null;
    const hasVerdictBasis = hasFull || hasHeader || autoBotLike === true;
    if (!hasVerdictBasis) {
      return {
        decided: false,
        isBot: false,
        verdict: null,
        source: "unknown",
        overriddenAutoBot: false,
        overriddenAutoHuman: false
      };
    }

    if (manualLabel === "human") {
      return {
        decided: true,
        isBot: false,
        verdict: "human",
        source: "manual",
        overriddenAutoBot: autoBotLike === true,
        overriddenAutoHuman: false
      };
    }
    if (manualLabel === "bot") {
      return {
        decided: true,
        isBot: true,
        verdict: "bot",
        source: "manual",
        overriddenAutoBot: false,
        overriddenAutoHuman: autoBotLike === false
      };
    }
    if (autoBotLike === true) {
      return {
        decided: true,
        isBot: true,
        verdict: "bot",
        source: "auto",
        overriddenAutoBot: false,
        overriddenAutoHuman: false
      };
    }
    if (autoBotLike === false) {
      const pts =
        scored && typeof scored.scorePts === "number"
          ? scored.scorePts
          : typeof profile.score_pts === "number"
            ? profile.score_pts
            : 0;
      const isSuspicious = hasFull && pts >= 35;
      return {
        decided: true,
        isBot: false,
        verdict: isSuspicious ? "suspicious" : "human",
        source: "auto",
        overriddenAutoBot: false,
        overriddenAutoHuman: false
      };
    }
    return {
      decided: false,
      isBot: false,
      verdict: null,
      source: "unknown",
      overriddenAutoBot: false,
      overriddenAutoHuman: false
    };
  }

  function upsertProfileInMap(profile) {
    if (!profile || !profile.username) return null;
    const existingKey = findProfileKeyInMap(profile.username);
    if (!existingKey) {
      state.profilesMap.set(profile.username, profile);
      return profile;
    }
    const prev = state.profilesMap.get(existingKey);
    const merged = stripTransientUiFields(prev ? mergeObserved(prev, profile) : profile);
    merged.artist = state.artist;
    state.profilesMap.set(existingKey, merged);
    return merged;
  }

  // Combine two sightings of one account: a number observed earlier is never erased by a later "unknown".
  function mergeObserved(prev, next) {
    const merged = Object.assign({}, next);
    if (prev && prev.schema_version === window.BotAuditParser.SCHEMA_VERSION) {
      [
        "has_profile_photo",
        "account_private",
        "post_count",
        "followers_count",
        "following_count",
        "bio_length"
      ].forEach((f) => {
        if (merged[f] === null || merged[f] === undefined) {
          merged[f] = prev[f] === undefined ? null : prev[f];
        }
      });
      if ((!merged.post_timestamps || merged.post_timestamps.length === 0) && prev.post_timestamps) {
        merged.post_timestamps = prev.post_timestamps;
      }
      ["observed_posts_count", "avg_post_likes", "shared_post_ratio"].forEach((fk) => {
        if (merged[fk] === null || merged[fk] === undefined) {
          if (typeof prev[fk] === "number") merged[fk] = prev[fk];
        }
      });
      merged.src = Object.assign({}, prev.src || {}, next.src || {});
    }
    if (!merged.manual_label && prev && prev.manual_label) {
      merged.manual_label = prev.manual_label;
    }
    merged.deep_checked = ["post_count", "followers_count", "following_count", "bio_length"].every(
      (f) => typeof merged[f] === "number"
    );
    merged.tier = window.BotAuditParser.profileTier(merged);
    applyScore(merged, window.BotAuditParser.scoreFollowerProfile(merged));
    return merged;
  }
  const IGNORED_PATHS = new Set([
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

  const LABELS_STORAGE_KEY = "botaudit_manual_labels_v1";

  const DEFAULT_READY_STATUS = "Ready — Previously saved accounts are skipped automatically";

  const state = {
    lang: "en",
    minimized: false,
    toolsOpen: false,
    targetPickerOpen: false,
    manualTargetLock: "",
    knownTargetAccounts: [],
    isMarkingClosing: false,
    _lastDockRenderKey: "",
    _lastInspectedHandle: "",
    _tabNonce: Math.random().toString(36).slice(2, 10),
    lastAutoSyncedSig: "",
    dirtyUsernames: new Set(),
    recentLocalUpdates: new Map(),
    _seenDeltaTimes: new Map(),
    openFollowerTabs: 0,
    batchConfirmedCount: 0,
    serverLiveCounts: null,
    artist: "",
    inspectedPageProfile: null,
    profilesMap: new Map(),
    manualLabelsMap: new Map(),
    globalObservedMap: new Map(),
    memoryBankTotals: { confirmed_bots: 0, confirmed_humans: 0, observed_profiles_count: 0 },
    labelStatusText: "",
    labelStatusColor: "",
    lastConfirmBanner: "",
    lastConfirmTone: "",
    lastConfirmUntil: 0,
    _confirmBannerTimer: null,
    completedSlices: [],
    activeSlice: "",
    lastUsername: "",
    lastScorePts: 0,
    nextMaxId: "",
    runningMode: null,
    pausedByRateLimit: false,
    http429Triggered: false,
    http429At: 0,
    etaSeconds: 0,
    sessionTarget: 200,
    newThisSession: 0,
    fastForwarding: false,
    statusMessage: "Ready — Previously saved accounts are skipped automatically"
  };

  function getArtistStorageKey(artist) {
    const clean = String(artist || "Audited_Account")
      .toLowerCase()
      .replace(/[^a-z0-9._-]/g, "_");
    return `botaudit_artist_v4_${clean}`;
  }

  function getLegacyV3ArtistStorageKey(artist) {
    const clean = String(artist || "Audited_Account")
      .toLowerCase()
      .replace(/[^a-z0-9._-]/g, "_");
    return `botaudit_artist_v3_${clean}`;
  }

  function formatHoursMinutes(ms, isRo) {
    if (!ms || ms <= 0) return isRo ? "0m (Acum)" : "0m (Ready Now)";
    const totalMin = Math.ceil(ms / 60000);
    const hrs = Math.floor(totalMin / 60);
    const mins = totalMin % 60;
    if (hrs > 0) {
      return `${hrs}h ${mins}m`;
    }
    return `${mins}m`;
  }

  function formatClockTime(timestampMs) {
    if (!timestampMs || timestampMs <= 0) return "";
    const d = new Date(timestampMs);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  function injectMainWorldListener() {
    try {
      const script = document.createElement("script");
      script.src = chrome.runtime.getURL("injected-interceptor.js");
      script.onload = () => script.remove();
      (document.head || document.documentElement).appendChild(script);
    } catch (_e) {
      // Fallback: DOM-only observation
    }
  }

  function getPagePathHandle() {
    const parts = (window.location.pathname || "/").split("/").filter(Boolean);
    if (parts.length > 0 && !IGNORED_PATHS.has(parts[0].toLowerCase())) {
      return parts[0].replace(/^@/, "");
    }
    return "";
  }

  function sendBackgroundMessage(action, payload) {
    return new Promise((resolve, reject) => {
      try {
        if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
          chrome.runtime.sendMessage({ action, payload }, (response) => {
            if (chrome.runtime.lastError) {
              reject(new Error(chrome.runtime.lastError.message));
            } else {
              resolve(response || {});
            }
          });
        } else {
          reject(new Error("chrome.runtime not available"));
        }
      } catch (err) {
        reject(err);
      }
    });
  }

  function isFollowersModal(dialog) {
    if (!dialog) return false;
    const path = String((window.location && window.location.pathname) || "");
    if (/\/following\/?$/i.test(path)) return false;
    if (/\/followers\/?$/i.test(path)) return true;
    let headingText = "";
    try {
      if (typeof dialog.querySelectorAll === "function") {
        const hdrEls = dialog.querySelectorAll('h1, h2, h3, [role="heading"], header');
        if (hdrEls && hdrEls.length > 0) {
          headingText = Array.from(hdrEls)
            .map((el) => (el && (el.innerText || el.textContent)) || "")
            .filter(Boolean)
            .join("\n");
        }
      }
      if (!headingText) {
        headingText = dialog.innerText || dialog.textContent || "";
      }
    } catch (_e) {
      headingText = "";
    }
    if (
      window.BotAuditParser &&
      typeof window.BotAuditParser.isFollowersModalDialogText === "function"
    ) {
      return window.BotAuditParser.isFollowersModalDialogText(headingText);
    }
    return false;
  }

  function readRecentModalClicks() {
    const now = Date.now();
    try {
      const raw = localStorage.getItem(RECENT_CLICKS_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      const items = parsed && Array.isArray(parsed.items) ? parsed.items : [];
      return items.filter(
        (it) =>
          it &&
          typeof it.f === "string" &&
          typeof it.a === "string" &&
          now - Number(it.ts || 0) < RECENT_CLICK_TTL_MS
      );
    } catch (_e) {
      return [];
    }
  }

  function recordRecentModalClick(follower, artist) {
    const fLow = String(follower || "").trim().toLowerCase();
    const cleanArtist = String(artist || "").trim();
    if (!fLow || !cleanArtist) return;
    const now = Date.now();
    try {
      const filtered = readRecentModalClicks().filter((it) => it.f !== fLow);
      filtered.push({ f: fLow, a: cleanArtist, ts: now });
      const capped = filtered.slice(-60);
      localStorage.setItem(RECENT_CLICKS_KEY, JSON.stringify({ items: capped }));
    } catch (_e) {
      // Ignore storage error
    }
  }

  function findRecentClickArtist(handle) {
    const hLow = String(handle || "").trim().toLowerCase();
    if (!hLow) return "";
    const items = readRecentModalClicks();
    for (let i = items.length - 1; i >= 0; i--) {
      if (items[i].f === hLow && items[i].a && items[i].a.toLowerCase() !== hLow) {
        return items[i].a;
      }
    }
    try {
      const recentRaw = localStorage.getItem(RECENT_CLICK_KEY);
      if (recentRaw) {
        const recent = JSON.parse(recentRaw);
        if (
          recent &&
          recent.follower &&
          recent.artist &&
          String(recent.follower).toLowerCase() === hLow &&
          String(recent.artist).toLowerCase() !== hLow &&
          Date.now() - Number(recent.ts || 0) < RECENT_CLICK_TTL_MS
        ) {
          return String(recent.artist);
        }
      }
    } catch (_e) {}
    return "";
  }

  function wasRecentlyClickedFromActiveFollowersModal(handle) {
    if (!handle || !state.artist || state.artist === "Audited_Account") return false;
    const hLow = String(handle).trim().toLowerCase();
    const aLow = String(state.artist).trim().toLowerCase();
    if (!hLow || hLow === aLow) return false;
    const matchedArtist = findRecentClickArtist(hLow);
    return Boolean(matchedArtist && matchedArtist.toLowerCase() === aLow);
  }

  function hasFollowersListPayload(obj) {
    if (!obj || typeof obj !== "object") return false;
    if (Array.isArray(obj.users)) return true;
    const data = obj.data && typeof obj.data === "object" ? obj.data : obj;
    if (
      data.xdt_api__v1__friendships__followers ||
      data.xdt_api__v1__friendships__followers_connection
    ) {
      return true;
    }
    const userNode = data.user && typeof data.user === "object" ? data.user : null;
    if (userNode && userNode.edge_followed_by && Array.isArray(userNode.edge_followed_by.edges)) {
      return true;
    }
    return false;
  }

  function normalizeTargetHandle(raw) {
    const clean = String(raw || "")
      .trim()
      .replace(/^@+/, "")
      .replace(/^\/+|\/+$/g, "")
      .split("/")[0]
      .trim();
    if (!clean || IGNORED_PATHS.has(clean.toLowerCase())) return "";
    return clean;
  }

  function clearRecentClickForHandle(handle) {
    const hLow = String(handle || "").trim().toLowerCase();
    if (!hLow) return;
    try {
      const items = readRecentModalClicks().filter((it) => it.f !== hLow);
      localStorage.setItem(RECENT_CLICKS_KEY, JSON.stringify({ items }));
    } catch (_e) {}
    try {
      const recentRaw = localStorage.getItem(RECENT_CLICK_KEY);
      if (recentRaw) {
        const recent = JSON.parse(recentRaw);
        if (recent && recent.follower && String(recent.follower).toLowerCase() === hLow) {
          localStorage.removeItem(RECENT_CLICK_KEY);
        }
      }
    } catch (_e) {}
  }

  function listLocalTargetAccounts() {
    const out = [];
    try {
      const prefix = "botaudit_artist_v4_";
      const totalKeys = typeof localStorage.length === "number" ? localStorage.length : 0;
      for (let i = 0; i < totalKeys; i++) {
        const k = typeof localStorage.key === "function" ? localStorage.key(i) : "";
        if (!k || !k.startsWith(prefix)) continue;
        const raw = localStorage.getItem(k);
        if (!raw) continue;
        try {
          const parsed = JSON.parse(raw);
          const artist = normalizeTargetHandle(
            (parsed && parsed.artist) || k.slice(prefix.length)
          );
          if (!artist) continue;
          const profs = parsed && Array.isArray(parsed.profiles) ? parsed.profiles : [];
          const checked = profs.filter(
            (p) => p && (p.deep_checked || p.score_conclusive || typeof p.bio_length === "number")
          ).length;
          out.push({ artist, saved: profs.length, checked });
        } catch (_parseErr) {}
      }
    } catch (_e) {}
    return out;
  }

  function getMergedKnownAccounts() {
    const byLow = new Map();
    const addEntry = (rawArtist, saved, checked, priority) => {
      const artist = normalizeTargetHandle(rawArtist);
      if (!artist || artist === "Audited_Account") return;
      const low = artist.toLowerCase();
      const prev = byLow.get(low);
      const next = {
        artist: prev ? prev.artist : artist,
        saved: Math.max(prev ? prev.saved : 0, Number(saved || 0)),
        checked: Math.max(prev ? prev.checked : 0, Number(checked || 0)),
        priority: Math.max(prev ? prev.priority : 0, Number(priority || 0))
      };
      byLow.set(low, next);
    };

    const pageHandle = getPagePathHandle();
    const curStats = computeDisplayCounts();
    if (state.artist && state.artist !== "Audited_Account") {
      addEntry(state.artist, curStats.totalCount, curStats.checkedCount, 3);
    }
    if (pageHandle) {
      addEntry(pageHandle, 0, 0, 2);
    }
    for (const acc of listLocalTargetAccounts()) {
      addEntry(acc.artist, acc.saved, acc.checked, 1);
    }
    for (const srvAcc of Array.isArray(state.knownTargetAccounts) ? state.knownTargetAccounts : []) {
      if (srvAcc && srvAcc.artist) {
        addEntry(srvAcc.artist, srvAcc.total_scanned, srvAcc.deep_checked, 1);
      }
    }

    return Array.from(byLow.values())
      .sort((a, b) => b.priority - a.priority || b.checked - a.checked || b.saved - a.saved || a.artist.localeCompare(b.artist))
      .map(({ artist, saved, checked }) => ({ artist, saved, checked }));
  }

  async function refreshKnownTargetAccounts() {
    try {
      let data = null;
      try {
        const bgResp = await sendBackgroundMessage("GET_HISTORY", {});
        if (bgResp && bgResp.ok && bgResp.data) data = bgResp.data;
      } catch (_e) {}
      if (!data) {
        const resp = await fetch("http://127.0.0.1:8000/api/history").catch(() => null);
        if (resp && resp.ok) data = await resp.json().catch(() => null);
      }
      if (data && Array.isArray(data.history)) {
        state.knownTargetAccounts = data.history;
        if (state.targetPickerOpen) updateFloatingDock(true);
      }
    } catch (_e) {}
  }

  function detectCurrentArtist() {
    const pageHandle = getPagePathHandle();
    const dialog = document.querySelector('div[role="dialog"]');
    let curManualLock = "";
    try {
      curManualLock = state && state.manualTargetLock ? String(state.manualTargetLock) : "";
    } catch (_tdzErr) {}
    if (dialog && isFollowersModal(dialog) && pageHandle) {
      if (curManualLock && curManualLock.toLowerCase() !== pageHandle.toLowerCase()) {
        try {
          state.manualTargetLock = "";
        } catch (_tdzErr) {}
      }
      try {
        localStorage.setItem(ACTIVE_ARTIST_KEY, pageHandle);
      } catch (_e) {
        // Ignore
      }
      return pageHandle;
    }
    if (curManualLock) {
      return curManualLock;
    }
    try {
      // Multi-tab wave parent target lock: looks up any of the last 60 middle-clicked tabs,
      // so opening 10-15 tabs in a burst keeps exact parent attribution for every tab.
      if (pageHandle) {
        const queuedArtist = findRecentClickArtist(pageHandle);
        if (queuedArtist && queuedArtist.toLowerCase() !== pageHandle.toLowerCase()) {
          return queuedArtist;
        }
      }

      const savedActive = localStorage.getItem(ACTIVE_ARTIST_KEY);
      if (savedActive && pageHandle && savedActive.toLowerCase() !== pageHandle.toLowerCase()) {
        // Check if pageHandle has its own saved followers checkpoint (>3 followers)
        const pageRaw = localStorage.getItem(getArtistStorageKey(pageHandle));
        const pageData = pageRaw ? JSON.parse(pageRaw) : null;
        if (!pageData || !Array.isArray(pageData.profiles) || pageData.profiles.length <= 3) {
          return savedActive;
        }
      }
      if (!savedActive && pageHandle && typeof document.referrer === "string") {
        const refMatch = document.referrer.match(/instagram\.com\/([a-zA-Z0-9._]+)\/?/i);
        const refHandle = refMatch ? refMatch[1] : "";
        if (
          refHandle &&
          !IGNORED_PATHS.has(refHandle.toLowerCase()) &&
          refHandle.toLowerCase() !== pageHandle.toLowerCase()
        ) {
          return refHandle;
        }
      }
    } catch (_e) {
      // Ignore
    }
    return pageHandle || "Audited_Account";
  }

  function recordModalFollowerClick(e) {
    if (!e || !e.target || typeof e.target.closest !== "function") return;
    const dialog = document.querySelector('div[role="dialog"]');
    if (!dialog || !isFollowersModal(dialog)) return;
    if (e.target.closest(".botaudit-row-actions, .botaudit-row-act")) return;
    const anchor = e.target.closest('a[href^="/"]');
    if (!anchor || !dialog.contains(anchor)) return;
    const href = anchor.getAttribute("href") || "";
    const parts = href.split("/").filter(Boolean);
    if (parts.length !== 1) return;
    const clickedFollower = parts[0].trim();
    const pageHandle = getPagePathHandle();
    if (
      !clickedFollower ||
      !pageHandle ||
      IGNORED_PATHS.has(clickedFollower.toLowerCase()) ||
      clickedFollower.toLowerCase() === pageHandle.toLowerCase()
    ) {
      return;
    }
    try {
      localStorage.setItem(ACTIVE_ARTIST_KEY, pageHandle);
      recordRecentModalClick(clickedFollower, pageHandle);
      localStorage.setItem(
        RECENT_CLICK_KEY,
        JSON.stringify({
          follower: clickedFollower.toLowerCase(),
          artist: pageHandle,
          ts: Date.now()
        })
      );
    } catch (_e) {
      // Ignore storage error
    }
  }

  function upgradeAndRescoreProfile(rawP) {
    if (!rawP || !rawP.username || !window.BotAuditParser) return rawP;
    const uname = String(rawP.username).trim();
    if (!uname || IGNORED_PATHS.has(uname.toLowerCase())) return null;
    // Older builds saved invented numbers for followers they never opened: make them "unknown" again.
    const p = window.BotAuditParser.scrubLegacyPlaceholders(rawP);
    p.username_digit_ratio = window.BotAuditParser.analyzeHandleForensics(uname).digit_ratio;
    applyScore(p, window.BotAuditParser.scoreFollowerProfile(p));
    return p;
  }

  function hydrateWithGlobalObserved(profile) {
    if (!profile || !profile.username || !window.BotAuditParser) return profile;
    const key = String(profile.username).toLowerCase();
    const obs = state.globalObservedMap.get(key);
    if (!obs) return profile;
    const obsProfile = {
      schema_version: window.BotAuditParser.SCHEMA_VERSION,
      artist: profile.artist || state.artist,
      username: profile.username,
      has_profile_photo: obs.has_profile_photo,
      account_private: obs.account_private,
      post_count: obs.post_count,
      followers_count: obs.followers_count,
      following_count: obs.following_count,
      bio_length: obs.bio_length,
      username_digit_ratio:
        typeof profile.username_digit_ratio === "number"
          ? profile.username_digit_ratio
          : window.BotAuditParser.analyzeHandleForensics(profile.username).digit_ratio,
      post_timestamps: profile.post_timestamps || [],
      src: Object.assign({ memory_bank: "server_observed" }, profile.src || {}),
      deep_checked: typeof obs.bio_length === "number",
      collected_at: profile.collected_at || new Date().toISOString().slice(0, 10)
    };
    const merged = stripTransientUiFields(mergeObserved(obsProfile, profile));
    merged.artist = profile.artist || state.artist;
    return merged;
  }

  function loadPersistedState(explicitArtist) {
    state.artist = explicitArtist || detectCurrentArtist();
    try {
      // Remove any pre-v2.7.1 uncleaned localStorage cache so cleaned server checkpoints stay authoritative.
      localStorage.removeItem(getLegacyV3ArtistStorageKey(state.artist));
    } catch (_rmV3Err) {}
    try {
      const raw = localStorage.getItem(getArtistStorageKey(state.artist));
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && Array.isArray(parsed.profiles)) {
          const selfLow =
            state.artist && state.artist !== "Audited_Account"
              ? state.artist.toLowerCase()
              : "";
          for (const rawP of parsed.profiles) {
            const up = upgradeAndRescoreProfile(rawP);
            if (up && (!selfLow || up.username.toLowerCase() !== selfLow)) {
              up.artist = state.artist;
              upsertProfileInMap(hydrateWithGlobalObserved(up));
            }
          }
        }
        if (parsed && Array.isArray(parsed.completedSlices)) {
          state.completedSlices = parsed.completedSlices;
        }
        if (
          parsed &&
          parsed.lastUsername &&
          !IGNORED_PATHS.has(String(parsed.lastUsername).toLowerCase())
        ) {
          state.lastUsername = parsed.lastUsername;
        }
        if (parsed && parsed.lang) state.lang = parsed.lang;
      }

      const savedLabels = localStorage.getItem(LABELS_STORAGE_KEY);
      if (savedLabels) {
        const obj = JSON.parse(savedLabels);
        for (const [u, val] of Object.entries(obj || {})) {
          if (u && val) state.manualLabelsMap.set(u.toLowerCase(), val);
        }
      }
    } catch (_e) {
      // Ignore storage error
    }
    syncMemoryBankFromServer();
    syncCheckpointFromServer();
    sendBackgroundMessage("GET_OPEN_TABS_COUNT", { artist: state.artist })
      .then((resp) => {
        if (resp && typeof resp.openFollowerTabs === "number") {
          state.openFollowerTabs = resp.openFollowerTabs;
          updateFloatingDock();
        }
      })
      .catch(() => {});
  }

  function setActiveTargetArtist(rawHandle, opts) {
    const clean = normalizeTargetHandle(rawHandle);
    if (!clean) return false;
    state.manualTargetLock = clean;
    try {
      localStorage.setItem(ACTIVE_ARTIST_KEY, clean);
    } catch (_e) {}
    clearRecentClickForHandle(clean);

    state.profilesMap.clear();
    state.dirtyUsernames.clear();
    state.serverLiveCounts = null;
    state.completedSlices = [];
    state.activeSlice = "";
    state.lastUsername = "";
    state.lastScorePts = 0;
    state.newThisSession = 0;
    state.batchConfirmedCount = 0;
    state.targetPickerOpen = false;
    state._lastDockRenderKey = "";

    if (
      state.inspectedPageProfile &&
      state.inspectedPageProfile.username &&
      state.inspectedPageProfile.username.toLowerCase() === clean.toLowerCase()
    ) {
      state.inspectedPageProfile = null;
    }

    loadPersistedState(clean);

    const isRemote = Boolean(opts && opts.fromRemote);
    if (!isRemote) {
      publishLiveDelta({
        type: "TARGET_ARTIST_CHANGED",
        artist: clean,
        ts: Date.now()
      });
      const isRo = state.lang === "ro";
      showLiveConfirmBanner(
        isRo ? `🎯 Cont țintă: @${clean}` : `🎯 Target: @${clean}`,
        "human"
      );
    }

    decorateFollowerRowsInDom();
    updateFloatingDock(true);
    return true;
  }

  const liveChannel = (function () {
    try {
      if (typeof BroadcastChannel === "function") {
        return new BroadcastChannel("botaudit_sync_v1");
      }
    } catch (_e) {}
    return null;
  })();

  function mergeAndWriteLabelsStorage() {
    try {
      const mergedObj = {};
      const existingRaw = localStorage.getItem(LABELS_STORAGE_KEY);
      if (existingRaw) {
        const existingObj = JSON.parse(existingRaw);
        for (const [k, v] of Object.entries(existingObj || {})) {
          if (k && v && v.label) {
            mergedObj[k.toLowerCase()] = v;
          }
        }
      }
      for (const [k, v] of state.manualLabelsMap.entries()) {
        if (!k || !v || !v.label) continue;
        const cleanEntry = Object.assign({}, v);
        delete cleanEntry.localTs;
        mergedObj[k.toLowerCase()] = cleanEntry;
      }
      localStorage.setItem(LABELS_STORAGE_KEY, JSON.stringify(mergedObj));
    } catch (_e) {}
  }

  function publishLiveDelta(delta) {
    if (!delta || typeof delta !== "object") return;
    const packet = Object.assign({ senderId: state._tabNonce || "", ts: Date.now() }, delta);
    try {
      if (liveChannel && typeof liveChannel.postMessage === "function") {
        liveChannel.postMessage(packet);
      }
    } catch (_e) {}
    try {
      localStorage.setItem(LAST_EVENT_KEY, JSON.stringify(packet));
    } catch (_e) {}
  }

  function isOnActiveArtistFollowersModalNow() {
    const pageHandle = getPagePathHandle();
    const dialog = document.querySelector('div[role="dialog"]');
    const onFollowersPath = /^\/[^/]+\/followers\/?$/i.test(window.location.pathname || "");
    return Boolean(
      state.artist &&
        state.artist !== "Audited_Account" &&
        pageHandle.toLowerCase() === state.artist.toLowerCase() &&
        (onFollowersPath || isFollowersModal(dialog))
    );
  }

  function applyIncomingLiveDelta(msg) {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "TARGET_ARTIST_CHANGED" && msg.artist) {
      if (msg.senderId && msg.senderId === state._tabNonce) return;
      const nextTarget = normalizeTargetHandle(msg.artist);
      if (!nextTarget) return;
      const pageHandle = getPagePathHandle();
      const dialog = document.querySelector('div[role="dialog"]');
      const onOtherFollowersModal = Boolean(
        dialog &&
          isFollowersModal(dialog) &&
          pageHandle &&
          pageHandle.toLowerCase() !== nextTarget.toLowerCase()
      );
      const isChildTabOfOther = Boolean(
        pageHandle &&
          (() => {
            const parent = findRecentClickArtist(pageHandle);
            return parent && parent.toLowerCase() !== nextTarget.toLowerCase();
          })()
      );
      if (
        !onOtherFollowersModal &&
        !isChildTabOfOther &&
        nextTarget.toLowerCase() !== String(state.artist || "").toLowerCase()
      ) {
        setActiveTargetArtist(nextTarget, { fromRemote: true });
      }
      return;
    }
    if (typeof msg.openFollowerTabs === "number") {
      state.openFollowerTabs = msg.openFollowerTabs;
    }
    const targetArtist = msg.artist ? String(msg.artist).trim() : "";
    if (
      targetArtist &&
      state.artist &&
      targetArtist.toLowerCase() !== state.artist.toLowerCase()
    ) {
      if (typeof msg.openFollowerTabs === "number") updateFloatingDock();
      return;
    }

    const now = Date.now();
    const profilesList = [];
    if (msg.profile && typeof msg.profile === "object") {
      profilesList.push(msg.profile);
    }
    if (Array.isArray(msg.profiles)) {
      for (const p of msg.profiles) {
        if (p && typeof p === "object") profilesList.push(p);
      }
    }

    const selfLow =
      state.artist && state.artist !== "Audited_Account" ? state.artist.toLowerCase() : "";
    for (const rawP of profilesList) {
      const up = upgradeAndRescoreProfile(rawP);
      if (!up || !up.username) continue;
      const uLow = up.username.toLowerCase();
      if (selfLow && uLow === selfLow) continue;
      up.artist = state.artist;
      if (msg.label && msg.username && uLow === String(msg.username).toLowerCase()) {
        up.manual_label = msg.label;
      }
      state.recentLocalUpdates.set(uLow, now);
      upsertProfileInMap(up);
    }

    const uLow = msg.username ? String(msg.username).trim().toLowerCase() : "";
    if (uLow && msg.label) {
      const existingLbl = state.manualLabelsMap.get(uLow);
      if (
        !existingLbl ||
        !existingLbl.localTs ||
        Number(msg.ts || now) >= existingLbl.localTs ||
        existingLbl.label !== msg.label
      ) {
        state.manualLabelsMap.set(uLow, {
          username: uLow,
          label: msg.label,
          artist: state.artist,
          status: "available",
          labeler: "extension_hud",
          localTs: now
        });
      }
      state.recentLocalUpdates.set(uLow, now);

      const existingKey = findProfileKeyInMap(uLow);
      if (existingKey) {
        const existingP = state.profilesMap.get(existingKey);
        if (existingP) {
          existingP.manual_label = msg.label;
        }
      }
      if (
        state.inspectedPageProfile &&
        (state.inspectedPageProfile.username || "").toLowerCase() === uLow
      ) {
        state.inspectedPageProfile.manual_label = msg.label;
      }

      if (msg.source === "mark" || msg.source === "inline_mark") {
        const dedupeKey = `${msg.source}:${uLow}:${msg.label}`;
        const prevSeenAt = state._seenDeltaTimes.get(dedupeKey) || 0;
        if (now - prevSeenAt >= 10000) {
          if (state._seenDeltaTimes.size > 300) state._seenDeltaTimes.clear();
          state._seenDeltaTimes.set(dedupeKey, now);
          if (!msg.senderId || msg.senderId !== state._tabNonce) {
            state.batchConfirmedCount += 1;
            if (msg.label === "bot" || msg.label === "human" || msg.label === "unsure") {
              const isRo = state.lang === "ro";
              const bannerText =
                msg.label === "bot"
                  ? isRo
                    ? `✓ Confirmat @${msg.username} ca BOT`
                    : `✓ Confirmed @${msg.username} as BOT`
                  : msg.label === "human"
                    ? isRo
                      ? `👤 Confirmat @${msg.username} ca OM REAL`
                      : `👤 Confirmed @${msg.username} as HUMAN`
                    : isRo
                      ? `🏷️ Marcat @${msg.username} ca NESIGUR`
                      : `🏷️ Marked @${msg.username} as UNSURE`;
              showLiveConfirmBanner(bannerText, msg.label);
            }
          }
        }
      }
    }

    if (
      msg.liveCounts &&
      typeof msg.liveCounts === "object" &&
      msg.liveCounts.artist &&
      msg.liveCounts.artist.toLowerCase() === state.artist.toLowerCase()
    ) {
      const prevRev = state.serverLiveCounts ? Number(state.serverLiveCounts.revision || 0) : 0;
      const nextRev = Number(msg.liveCounts.revision || 0);
      if (!state.serverLiveCounts || nextRev >= prevRev) {
        state.serverLiveCounts = msg.liveCounts;
      }
    }

    decorateFollowerRowsInDom();
    updateFloatingDock();
  }

  if (liveChannel) {
    liveChannel.onmessage = (evt) => {
      if (evt && evt.data) {
        applyIncomingLiveDelta(evt.data);
      }
    };
    if (typeof liveChannel.unref === "function") {
      liveChannel.unref();
    }
  }

  async function syncMemoryBankFromServer() {
    try {
      let data = null;
      try {
        const bgResp = await sendBackgroundMessage("GET_MEMORY_BANK");
        if (bgResp && bgResp.ok && bgResp.data) data = bgResp.data;
      } catch (_e) {}

      if (!data) {
        const resp = await fetch("http://127.0.0.1:8000/api/memory-bank").catch(() =>
          fetch("http://localhost:8000/api/memory-bank")
        );
        if (resp && resp.ok) data = await resp.json();
      }

      if (!data || typeof data !== "object") return;
      let changed = false;
      const nowMs = Date.now();

      if (data.totals && typeof data.totals === "object") {
        state.memoryBankTotals = Object.assign({}, state.memoryBankTotals, data.totals);
      }

      if (data.labels && typeof data.labels === "object") {
        const serverLabelKeys = new Set();
        for (const [unameLow, item] of Object.entries(data.labels)) {
          if (unameLow && item && item.label) {
            const low = unameLow.toLowerCase();
            serverLabelKeys.add(low);
            const prevEntry = state.manualLabelsMap.get(low);
            if (
              prevEntry &&
              prevEntry.localTs &&
              nowMs - prevEntry.localTs < 20000 &&
              prevEntry.label !== item.label
            ) {
              continue;
            }
            if (!prevEntry || prevEntry.label !== item.label) {
              changed = true;
            }
            state.manualLabelsMap.set(low, {
              username: low,
              label: item.label,
              status: item.status || "available",
              labeled_on: item.labeled_on || "",
              bot_type: item.bot_type || null,
              labeler: item.source || "memory_bank"
            });
          }
        }
        for (const [low, entry] of Array.from(state.manualLabelsMap.entries())) {
          const isRecentLocal = Boolean(entry && entry.localTs && nowMs - entry.localTs < 20000);
          if (
            !serverLabelKeys.has(low) &&
            entry &&
            entry.labeler !== "extension_hud" &&
            !isRecentLocal
          ) {
            state.manualLabelsMap.delete(low);
            changed = true;
          }
        }
        // Sync stale local checkpoint manual_label flags to match server ground truth
        for (const [uKey, existingP] of state.profilesMap.entries()) {
          if (!existingP) continue;
          const srv = state.manualLabelsMap.get(String(uKey).toLowerCase());
          const srvLbl = srv && srv.label ? srv.label : null;
          if (srvLbl) {
            if (existingP.manual_label !== srvLbl) {
              existingP.manual_label = srvLbl;
              changed = true;
            }
          } else if (existingP.manual_label) {
            delete existingP.manual_label;
            changed = true;
          }
        }
        if (state.inspectedPageProfile && state.inspectedPageProfile.username) {
          const inspLow = state.inspectedPageProfile.username.toLowerCase();
          const srvInsp = state.manualLabelsMap.get(inspLow);
          const srvInspLbl = srvInsp && srvInsp.label ? srvInsp.label : "";
          if (srvInspLbl && state.inspectedPageProfile.manual_label !== srvInspLbl) {
            state.inspectedPageProfile.manual_label = srvInspLbl;
            changed = true;
          }
        }
      }

      if (data.observed_profiles && typeof data.observed_profiles === "object") {
        for (const [unameLow, obs] of Object.entries(data.observed_profiles)) {
          if (
            unameLow &&
            obs &&
            typeof obs.post_count === "number" &&
            typeof obs.followers_count === "number" &&
            typeof obs.following_count === "number"
          ) {
            state.globalObservedMap.set(unameLow.toLowerCase(), obs);
          }
        }
        // Upgrade any incomplete rows currently visible in this target's map
        for (const [uKey, existingP] of state.profilesMap.entries()) {
          if (existingP && !existingP.score_complete) {
            const hydrated = hydrateWithGlobalObserved(existingP);
            if (
              hydrated &&
              (hydrated.score_complete !== existingP.score_complete ||
                hydrated.bot_like !== existingP.bot_like ||
                hydrated.post_count !== existingP.post_count ||
                hydrated.followers_count !== existingP.followers_count ||
                hydrated.following_count !== existingP.following_count ||
                hydrated.bio_length !== existingP.bio_length)
            ) {
              state.profilesMap.set(uKey, hydrated);
              changed = true;
            }
          }
        }
      }

      if (changed) {
        decorateFollowerRowsInDom();
        updateFloatingDock();
      }
    } catch (_e) {
      // Local server offline
    }
  }

  async function syncLabelsFromServer() {
    try {
      let data = null;
      try {
        const bgResp = await sendBackgroundMessage("GET_LABELS");
        if (bgResp && bgResp.ok && bgResp.data) data = bgResp.data;
      } catch (_e) {}

      if (!data) {
        const resp = await fetch("http://127.0.0.1:8000/api/labels").catch(() =>
          fetch("http://localhost:8000/api/labels")
        );
        if (resp && resp.ok) data = await resp.json();
      }

      if (data && Array.isArray(data.notes)) {
        let changed = false;
        for (const n of data.notes) {
          if (n && n.username && n.label) {
            const key = n.username.toLowerCase();
            const prevEntry = state.manualLabelsMap.get(key);
            if (!prevEntry || prevEntry.label !== n.label) {
              changed = true;
            }
            state.manualLabelsMap.set(key, {
              username: key,
              label: n.label,
              status: n.status || "available",
              labeled_on: n.labeled_on || "",
              bot_type: n.bot_type || null,
              labeler: n.labeler || "server"
            });
            const existingKey = findProfileKeyInMap(key);
            const existingP = existingKey ? state.profilesMap.get(existingKey) : null;
            if (existingP && existingP.manual_label !== n.label) {
              existingP.manual_label = n.label;
              changed = true;
            }
          }
        }
        if (changed) {
          decorateFollowerRowsInDom();
          updateFloatingDock();
        }
      }
    } catch (_e) {
      // Local server offline
    }
  }

  function findProfileKeyInMap(uname) {
    if (!uname) return null;
    if (state.profilesMap.has(uname)) return uname;
    const targetLower = String(uname).toLowerCase();
    for (const k of state.profilesMap.keys()) {
      if (k.toLowerCase() === targetLower) return k;
    }
    return null;
  }

  function syncSingleProfileToServer(artist, cleanProfile) {
    if (!artist || !cleanProfile || !cleanProfile.username) return;
    const cpPayload = {
      artist: artist,
      profiles: [cleanProfile],
      last_username: cleanProfile.username
    };
    sendBackgroundMessage("SAVE_CHECKPOINT", cpPayload)
      .then((bgResp) => {
        if (bgResp && bgResp.data && bgResp.data.live_counts) {
          applyIncomingLiveDelta({
            artist: artist,
            liveCounts: bgResp.data.live_counts,
            source: "live_counts_only"
          });
        }
      })
      .catch(() => {
        fetch("http://127.0.0.1:8000/api/checkpoint", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(cpPayload),
          keepalive: true
        })
          .then((r) => (r && r.ok ? r.json() : null))
          .then((data) => {
            if (data && data.live_counts) {
              applyIncomingLiveDelta({
                artist: artist,
                liveCounts: data.live_counts,
                source: "live_counts_only"
              });
            }
          })
          .catch(() => {
            fetch("http://localhost:8000/api/checkpoint", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(cpPayload),
              keepalive: true
            }).catch(() => {});
          });
      });
  }

  async function syncCheckpointFromServer() {
    try {
      let data = null;
      try {
        const bgResp = await sendBackgroundMessage("GET_CHECKPOINT", { artist: state.artist });
        if (bgResp && bgResp.ok && bgResp.data) data = bgResp.data;
      } catch (_e) {}

      if (!data) {
        const resp = await fetch(
          `http://127.0.0.1:8000/api/checkpoint?artist=${encodeURIComponent(state.artist)}`
        ).catch(() =>
          fetch(`http://localhost:8000/api/checkpoint?artist=${encodeURIComponent(state.artist)}`)
        );
        if (resp && resp.ok) data = await resp.json();
      }

      if (data && Array.isArray(data.profiles)) {
        const selfLow =
          state.artist && state.artist !== "Audited_Account" ? state.artist.toLowerCase() : "";
        let changedCount = 0;
        const serverKeysLower = new Set();

        for (const rawP of data.profiles) {
          const up = upgradeAndRescoreProfile(rawP);
          if (!up) continue;
          const uLow = up.username.toLowerCase();
          if (selfLow && uLow === selfLow) continue;

          const hydrated = hydrateWithGlobalObserved(up);
          const srvLblEntry = state.manualLabelsMap.get(uLow);
          if (srvLblEntry && srvLblEntry.label && hydrated.manual_label !== srvLblEntry.label) {
            hydrated.manual_label = srvLblEntry.label;
          }
          serverKeysLower.add(uLow);

          const existingKey = findProfileKeyInMap(hydrated.username);
          if (!existingKey) {
            state.profilesMap.set(hydrated.username, hydrated);
            changedCount++;
          } else {
            const prev = state.profilesMap.get(existingKey);
            const merged = stripTransientUiFields(mergeObserved(prev, hydrated));
            merged.artist = state.artist;
            if (
              (merged.score_complete && !prev.score_complete) ||
              merged.bot_like !== prev.bot_like ||
              merged.post_count !== prev.post_count ||
              merged.tier > (prev.tier || 0) ||
              merged.manual_label !== prev.manual_label ||
              merged.score_pts !== prev.score_pts
            ) {
              state.profilesMap.set(existingKey, merged);
              changedCount++;
            }
          }
        }

        // Prune stale ghost handles removed from the authoritative server checkpoint,
        // while never pruning handles touched in the last 60s across concurrent tabs or being inspected now.
        if (data.checkpoint_exists === true || data.profiles.length > 0) {
          const nowMs = Date.now();
          const dirtyLower = new Set(
            Array.from(state.dirtyUsernames || [], (u) => String(u).toLowerCase())
          );
          const inspLow =
            state.inspectedPageProfile && state.inspectedPageProfile.username
              ? String(state.inspectedPageProfile.username).toLowerCase()
              : "";
          for (const existingKey of Array.from(state.profilesMap.keys())) {
            const low = String(existingKey).toLowerCase();
            const recentTs = state.recentLocalUpdates ? state.recentLocalUpdates.get(low) || 0 : 0;
            const isProtected =
              dirtyLower.has(low) ||
              (recentTs > 0 && nowMs - recentTs < 60000) ||
              (inspLow && low === inspLow);
            if (!serverKeysLower.has(low) && !isProtected) {
              state.profilesMap.delete(existingKey);
              changedCount++;
            }
          }
        }

        if (Array.isArray(data.completed_slices)) {
          const mergedSlices = new Set([...state.completedSlices, ...data.completed_slices]);
          state.completedSlices = Array.from(mergedSlices);
        }
        if (
          data.last_username &&
          !state.lastUsername &&
          !IGNORED_PATHS.has(String(data.last_username).toLowerCase())
        ) {
          state.lastUsername = data.last_username;
        }
        if (changedCount > 0) {
          savePersistedState(false);
          decorateFollowerRowsInDom();
          updateFloatingDock();
        }
      }
    } catch (_e) {
      // Local server offline
    }
  }

  function buildCompactLocalProfile(p) {
    const clean = stripTransientUiFields(p);
    if (!clean || !clean.username) return clean;
    const hasAnyObservedStat = [
      "post_count",
      "followers_count",
      "following_count",
      "bio_length"
    ].some((f) => typeof clean[f] === "number");
    if (!clean.score_complete && !clean.deep_checked && !clean.manual_label && !hasAnyObservedStat) {
      const tiny = {
        schema_version: clean.schema_version,
        artist: clean.artist,
        username: clean.username,
        tier: clean.tier || 0
      };
      if (typeof clean.has_profile_photo === "boolean") tiny.has_profile_photo = clean.has_profile_photo;
      if (typeof clean.account_private === "boolean") tiny.account_private = clean.account_private;
      if (clean.collected_at) tiny.collected_at = clean.collected_at;
      return tiny;
    }
    const compact = Object.assign({}, clean);
    delete compact.flags;
    delete compact.src;
    delete compact.bot_score;
    delete compact.score_pts;
    delete compact.max_score_pts;
    delete compact.bot_like;
    delete compact.score_complete;
    delete compact.score_conclusive;
    delete compact.same_day_post_burst;
    return compact;
  }

  function isEssentialCompactProfile(p) {
    return Boolean(
      p &&
        (p.deep_checked ||
          p.manual_label ||
          (p.tier || 0) >= 2 ||
          (typeof p.post_count === "number" &&
            typeof p.followers_count === "number" &&
            typeof p.following_count === "number"))
    );
  }

  function savePersistedState(syncToServer = true) {
    const profilesArr = Array.from(state.profilesMap.values()).map(stripTransientUiFields);
    if (profilesArr.length > 0) {
      const lastP = profilesArr[profilesArr.length - 1];
      state.lastUsername = lastP.username;
      state.lastScorePts = lastP.score_pts ?? Math.round((lastP.bot_score || 0) * 100);
    }

    const storageKey = getArtistStorageKey(state.artist);
    const dirtyCopy = Array.from(state.dirtyUsernames || []);
    const onFollowersTab = isOnActiveArtistFollowersModalNow();

    try {
      let toPersist = profilesArr;
      if (!onFollowersTab) {
        // Secondary follower profile tabs read-merge-write their dirty profiles into existing
        // localStorage instead of overwriting the main Followers modal's full profile list.
        const existingRaw = localStorage.getItem(storageKey);
        if (existingRaw) {
          const existingParsed = JSON.parse(existingRaw);
          if (existingParsed && Array.isArray(existingParsed.profiles)) {
            const mergedByLow = new Map();
            for (const rawP of existingParsed.profiles) {
              if (!rawP || !rawP.username) continue;
              const up = upgradeAndRescoreProfile(rawP);
              if (up) mergedByLow.set(up.username.toLowerCase(), up);
            }
            const patchHandles = new Set(dirtyCopy.map((u) => String(u).toLowerCase()));
            if (state.inspectedPageProfile && state.inspectedPageProfile.username) {
              patchHandles.add(String(state.inspectedPageProfile.username).toLowerCase());
            }
            for (const pLow of patchHandles) {
              const k = findProfileKeyInMap(pLow);
              if (!k) continue;
              const localP = stripTransientUiFields(state.profilesMap.get(k));
              const prevP = mergedByLow.get(pLow);
              const combined = prevP ? mergeObserved(prevP, localP) : localP;
              combined.artist = state.artist;
              mergedByLow.set(pLow, combined);
            }
            toPersist = Array.from(mergedByLow.values());
          }
        }
      }

      const compactArr = toPersist.map(buildCompactLocalProfile);
      let payload = {
        artist: state.artist,
        lang: state.lang,
        updatedAt: Date.now(),
        lastUsername: state.lastUsername,
        completedSlices: state.completedSlices,
        profiles: compactArr
      };
      let jsonStr = JSON.stringify(payload);
      if (jsonStr.length > 3200000) {
        const important = compactArr.filter(isEssentialCompactProfile);
        const handleOnly = compactArr.filter((p) => p && !isEssentialCompactProfile(p));
        const keepHandles = handleOnly.slice(-Math.min(handleOnly.length, 3500));
        payload = Object.assign({}, payload, { profiles: important.concat(keepHandles) });
        jsonStr = JSON.stringify(payload);
      }
      localStorage.setItem(storageKey, jsonStr);
      // Remove legacy duplicate copies so Chrome's 5MB per-origin localStorage quota is never wasted
      try {
        localStorage.removeItem(LEGACY_KEY);
        localStorage.removeItem(getLegacyV3ArtistStorageKey(state.artist));
      } catch (_rmErr) {}
    } catch (_e) {
      try {
        const essential = profilesArr
          .map(buildCompactLocalProfile)
          .filter(isEssentialCompactProfile);
        localStorage.setItem(
          storageKey,
          JSON.stringify({
            artist: state.artist,
            lang: state.lang,
            updatedAt: Date.now(),
            lastUsername: state.lastUsername,
            completedSlices: state.completedSlices,
            profiles: essential
          })
        );
      } catch (_innerErr) {}
    }

    if (syncToServer && profilesArr.length > 0) {
      let deltaProfiles = [];
      if (state.dirtyUsernames && state.dirtyUsernames.size > 0) {
        for (const u of state.dirtyUsernames) {
          const k = findProfileKeyInMap(u);
          if (k) {
            const cleanP = stripTransientUiFields(state.profilesMap.get(k));
            deltaProfiles.push(cleanP);
            if (cleanP && cleanP.username) {
              state.recentLocalUpdates.set(String(cleanP.username).toLowerCase(), Date.now());
            }
          }
        }
        state.dirtyUsernames.clear();
      } else {
        deltaProfiles = profilesArr.slice(-50);
      }
      if (deltaProfiles.length === 0) return;

      const cpPayload = {
        artist: state.artist,
        profiles: deltaProfiles,
        completed_slices: state.completedSlices,
        last_username: state.lastUsername,
        next_max_id: state.nextMaxId
      };
      sendBackgroundMessage("SAVE_CHECKPOINT", cpPayload)
        .then((bgResp) => {
          if (bgResp && bgResp.data && bgResp.data.live_counts) {
            applyIncomingLiveDelta({
              artist: state.artist,
              liveCounts: bgResp.data.live_counts,
              source: "live_counts_only"
            });
          }
        })
        .catch(() => {
          fetch("http://127.0.0.1:8000/api/checkpoint", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(cpPayload)
          }).catch(() => {
            fetch("http://localhost:8000/api/checkpoint", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(cpPayload)
            }).catch(() => {});
          });
        });
    }
  }

  function getRateTimestamps() {
    try {
      const raw = localStorage.getItem(RATE_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      const cutoff = Date.now() - DAY_MS;
      return Array.isArray(arr)
        ? arr.filter((t) => typeof t === "number" && t > cutoff).sort((a, b) => a - b)
        : [];
    } catch (_e) {
      return [];
    }
  }

  function recordNewProfilesChecked(countNew) {
    if (!countNew || countNew <= 0) return getRateLimitStatus();
    const arr = getRateTimestamps();
    const ticks = Math.min(countNew, 3);
    const now = Date.now();
    for (let i = 0; i < ticks; i++) {
      arr.push(now);
    }
    try {
      localStorage.setItem(RATE_KEY, JSON.stringify(arr));
    } catch (_e) {
      // Ignore
    }
    return getRateLimitStatus();
  }

  // Computes exact Hourly & 24h Rate-Limit status + Hours/Minutes until Next Safe Scan
  function getRateLimitStatus() {
    const now = Date.now();
    const allDay = getRateTimestamps();
    const lastHourArr = allDay.filter((t) => t > now - HOUR_MS);
    const checksLastHour = lastHourArr.length;
    const checksLastDay = allDay.length;

    // 1. Calculate when the 1-hour window resets or drops back to Green (<120/hr)
    let msUntilHourReset = 0;
    if (lastHourArr.length > 0) {
      msUntilHourReset = Math.max(0, lastHourArr[0] + HOUR_MS - now);
    }

    let msUntilGreenHour = 0;
    if (checksLastHour >= 120) {
      // To drop from checksLastHour to 119, (checksLastHour - 119) oldest entries in lastHourArr must expire
      const idxToExpire = Math.min(lastHourArr.length - 1, checksLastHour - 119);
      msUntilGreenHour = Math.max(0, lastHourArr[idxToExpire] + HOUR_MS - now);
    }

    // 2. Calculate when the 24-hour window resets or drops back to Green (<450/day)
    let msUntilDayReset = 0;
    if (allDay.length > 0) {
      msUntilDayReset = Math.max(0, allDay[0] + DAY_MS - now);
    }

    let msUntilGreenDay = 0;
    if (checksLastDay >= 450) {
      const idxDayExpire = Math.min(allDay.length - 1, checksLastDay - 449);
      msUntilGreenDay = Math.max(0, allDay[idxDayExpire] + DAY_MS - now);
    }

    // 3. If HTTP 429 was triggered, enforce a 45-minute safety cooldown
    let msUntil429Clear = 0;
    if (state.http429Triggered && state.http429At > 0) {
      msUntil429Clear = Math.max(0, state.http429At + 45 * 60 * 1000 - now);
      if (msUntil429Clear === 0) state.http429Triggered = false;
    }

    const msUntilSafe = Math.max(msUntilGreenHour, msUntilGreenDay, msUntil429Clear);
    const nextSafeClock = msUntilSafe > 0 ? formatClockTime(now + msUntilSafe) : "";
    const nextDayResetClock = msUntilDayReset > 0 ? formatClockTime(now + msUntilDayReset) : "";

    let zone = "green";
    let delayRangeMs = [2400, 4000];
    let labelEn = `SAFE ZONE (${checksLastHour}/180 hr · ${checksLastDay}/600 day)`;
    let labelRo = `ZONĂ SIGURĂ (${checksLastHour}/180 oră · ${checksLastDay}/600 zi)`;

    if (state.http429Triggered || checksLastHour >= 180 || checksLastDay >= 600) {
      zone = "red";
      delayRangeMs = [0, 0];
      labelEn = `COOLDOWN ACTIVE (${checksLastHour}/180 hr · ${checksLastDay}/600 day)`;
      labelRo = `PAUZĂ PROTECȚIE (${checksLastHour}/180 oră · ${checksLastDay}/600 zi)`;
    } else if (checksLastHour >= 120 || checksLastDay >= 450) {
      zone = "yellow";
      delayRangeMs = [5500, 8500];
      labelEn = `CAUTION — AUTO-SLOWED (${checksLastHour}/180 hr)`;
      labelRo = `ATENȚIE — ÎNCETINIT (${checksLastHour}/180 oră)`;
    }

    const remHourSafe = Math.max(0, 180 - checksLastHour);
    const remDaySafe = Math.max(0, 600 - checksLastDay);

    const nextScanEn =
      msUntilSafe > 0
        ? `⏳ Next Safe Scan in: ${formatHoursMinutes(msUntilSafe, false)} (at ${nextSafeClock})`
        : state.newThisSession >= 200
          ? `✅ 200 Daily Target Done! Next 24h cycle resets in ${formatHoursMinutes(msUntilDayReset, false)} (${nextDayResetClock})`
          : `⏱️ Safe Now (+${remHourSafe} left this hr · 24h resets in ${formatHoursMinutes(msUntilDayReset || DAY_MS, false)})`;

    const nextScanRo =
      msUntilSafe > 0
        ? `⏳ Următoarea Scanare Sigură în: ${formatHoursMinutes(msUntilSafe, true)} (la ${nextSafeClock})`
        : state.newThisSession >= 200
          ? `✅ Ținta de 200/zi atinsă! Ciclul 24h se resetează în ${formatHoursMinutes(msUntilDayReset, true)} (${nextDayResetClock})`
          : `⏱️ Sigur Acum (+${remHourSafe} disponibile/oră · Reset 24h în ${formatHoursMinutes(msUntilDayReset || DAY_MS, true)})`;

    return {
      zone,
      checksLastHour,
      checksLastDay,
      remHourSafe,
      remDaySafe,
      msUntilSafe,
      msUntilHourReset,
      msUntilDayReset,
      delayRangeMs,
      labelEn,
      labelRo,
      nextScanEn,
      nextScanRo
    };
  }

  // Listen to passive interceptor events
  window.addEventListener("BotAuditInterceptedData", (evt) => {
    if (!evt || !evt.detail || !evt.detail.payload || !window.BotAuditParser) return;
    const urlType =
      typeof window.BotAuditParser.classifyInstagramInterceptUrl === "function"
        ? window.BotAuditParser.classifyInstagramInterceptUrl(evt.detail.url || "")
        : "profile_detail";
    if (urlType === "following_list" || urlType === "ignore") return;

    const newArtist = detectCurrentArtist();
    if (newArtist !== state.artist) {
      state.artist = newArtist;
      state.profilesMap.clear();
      loadPersistedState();
    }

    const pageHandle = getPagePathHandle();
    const dialog = document.querySelector('div[role="dialog"]');
    const onFollowersPath = /^\/[^/]+\/followers\/?$/i.test(window.location.pathname || "");
    const isOnActiveArtistFollowersModal = Boolean(
      state.artist &&
        state.artist !== "Audited_Account" &&
        pageHandle.toLowerCase() === state.artist.toLowerCase() &&
        (onFollowersPath || isFollowersModal(dialog))
    );

    const payload = evt.detail.payload;
    let extractOpts = null;
    if (urlType === "followers_list") {
      if (!isOnActiveArtistFollowersModal) return;
      extractOpts = { allowNew: true, strictFollowerOnly: true, excludeArtist: true };
    } else if (isOnActiveArtistFollowersModal && hasFollowersListPayload(payload)) {
      extractOpts = { allowNew: true, strictFollowerOnly: true, excludeArtist: true };
    } else {
      const allowedUsernames = new Set();
      const pageLow = pageHandle ? pageHandle.toLowerCase() : "";
      const artistLow = state.artist ? state.artist.toLowerCase() : "";
      if (
        pageLow &&
        pageLow !== artistLow &&
        (findProfileKeyInMap(pageHandle) || wasRecentlyClickedFromActiveFollowersModal(pageHandle))
      ) {
        allowedUsernames.add(pageLow);
      }
      if (isOnActiveArtistFollowersModal && dialog) {
        dialog.querySelectorAll('a[href^="/"][href$="/"]').forEach((a) => {
          const parts = (a.getAttribute("href") || "").split("/").filter(Boolean);
          if (parts.length !== 1) return;
          const uLow = parts[0].trim().toLowerCase();
          if (uLow && uLow !== artistLow && !IGNORED_PATHS.has(uLow)) {
            allowedUsernames.add(uLow);
          }
        });
      }
      extractOpts = {
        allowNew: false,
        allowedUsernames,
        strictFollowerOnly: true,
        excludeArtist: true
      };
    }

    if (urlType === "followers_list" && payload && typeof payload.next_max_id === "string") {
      state.nextMaxId = payload.next_max_id;
    }
    const profileSig = (p) =>
      `${p.tier || 0}|${p.has_profile_photo}|${p.account_private}|${p.post_count}|${p.followers_count}|${p.following_count}|${p.bio_length}|${p.bot_like}|${p.score_pts}|${p.max_score_pts}`;
    const beforeSnapshot = new Map();
    for (const [u, existingP] of state.profilesMap.entries()) {
      beforeSnapshot.set(u, profileSig(existingP));
    }
    const beforeSize = state.profilesMap.size;
    window.BotAuditParser.extractProfilesFromInstagramJson(
      payload,
      state.artist,
      state.profilesMap,
      extractOpts
    );
    let updatedCount = 0;
    const changedProfiles = [];
    for (const [u, nextP] of state.profilesMap.entries()) {
      const mEntry = state.manualLabelsMap.get(String(u).toLowerCase());
      if (mEntry && mEntry.label && nextP.manual_label !== mEntry.label) {
        nextP.manual_label = mEntry.label;
      }
      if (beforeSnapshot.get(u) !== profileSig(nextP)) {
        state.dirtyUsernames.add(u);
        state.recentLocalUpdates.set(String(u).toLowerCase(), Date.now());
        changedProfiles.push(stripTransientUiFields(nextP));
        updatedCount++;
      }
    }
    const addedNow = Math.max(0, state.profilesMap.size - beforeSize);
    if (addedNow > 0) {
      state.newThisSession += addedNow;
      recordNewProfilesChecked(1);
    }
    if (addedNow > 0 || updatedCount > 0) {
      savePersistedState(true);
      if (changedProfiles.length > 0 && changedProfiles.length <= 25) {
        publishLiveDelta({
          type: "PROFILE_DELTA",
          artist: state.artist,
          profiles: changedProfiles,
          source: "intercept"
        });
      }
    }
    decorateFollowerRowsInDom();
    updateFloatingDock();
  });

  window.addEventListener("BotAuditHttp429", () => {
    state.http429Triggered = true;
    state.http429At = Date.now();
    state.runningMode = null;
    state.pausedByRateLimit = true;
    state.statusMessage =
      state.lang === "ro"
        ? "Oprit automat (HTTP 429). Toate profilurile sunt salvate!"
        : "Auto-paused (HTTP 429). All profiles saved to resume safely!";
    savePersistedState(true);
    updateFloatingDock();
  });

  function inspectCurrentOpenProfilePage() {
    if (!window.BotAuditParser) return null;
    const pageHandle = getPagePathHandle();
    if (!pageHandle) {
      state.inspectedPageProfile = null;
      state._lastInspectedHandle = "";
      return null;
    }
    const pageLow = pageHandle.toLowerCase();
    if (state._lastInspectedHandle && state._lastInspectedHandle !== pageLow) {
      state.isMarkingClosing = false;
      state.labelStatusText = "";
      state.labelStatusColor = "";
    }
    state._lastInspectedHandle = pageLow;

    const headerEl = document.querySelector("header") || document.querySelector("main");
    const metaEl =
      document.querySelector('meta[property="og:description"]') ||
      document.querySelector('meta[name="description"]');
    const headerText = headerEl ? headerEl.innerText || "" : "";
    const metaText = metaEl ? metaEl.getAttribute("content") || "" : "";
    const parsedStats =
      window.BotAuditParser.parseProfileHeaderStats(headerText) ||
      window.BotAuditParser.parseProfileHeaderStats(metaText);

    if (!parsedStats) return null;

    const bodyText = document.body ? document.body.innerText || "" : "";
    const isPrivate =
      /this profile is private|this account is private|acest cont este privat|acest profil este privat/i.test(
        bodyText
      );
    const avatarImg = headerEl
      ? headerEl.querySelector(
          'img[alt*="profile picture" i], img[alt*="fotografia de profil" i]'
        ) || headerEl.querySelector("img")
      : null;
    const avatarSrc = avatarImg ? avatarImg.getAttribute("src") || "" : "";
    const hasPhoto = avatarImg ? !avatarSrc.includes("44884218_345707102882519") : null;

    // The bio length is only known when the stats row was found or header DOM rendered stats.
    const lines = headerText
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    let bioLen = null;
    let bioPreview = "";
    const headerHasStats = Boolean(
      headerEl && window.BotAuditParser.parseProfileHeaderStats(headerText)
    );
    const statsIdx = lines.findIndex((l) =>
      /following|urmăriri|urmariri|urmărește|urmareste/i.test(l)
    );
    const uiNoiseRe =
      /^(follow|following|follow back|message|requested|similar accounts|edit profile|share profile|view archive|ad tools|urmărește|urmareste|mesaj|trimite mesaj|solicitare trimisă|\d[\d.,\skm]*\s*(?:posts?|postări|postari|followers?|urmăritori|urmaritori|following|urmăriri|urmariri))$/i;
    if (statsIdx !== -1) {
      const bioLines = lines
        .slice(statsIdx + 1)
        .filter((l) => !uiNoiseRe.test(l));
      const candidateBio = bioLines.join(" ");
      bioLen = candidateBio.length;
      bioPreview = bioLines
        .filter((l) => !/^(followed by\b|urmărit de\b|urmarit de\b)/i.test(l))
        .join(" · ")
        .slice(0, 110);
    } else if (headerHasStats && lines.length > 0) {
      const cleanHandleLow = pageHandle.toLowerCase();
      const bioLines = lines.filter(
        (l) => l.toLowerCase() !== cleanHandleLow && !uiNoiseRe.test(l)
      );
      const candidateBio = bioLines.join(" ");
      bioLen = candidateBio.length;
      bioPreview = bioLines
        .filter((l) => !/^(followed by\b|urmărit de\b|urmarit de\b)/i.test(l))
        .join(" · ")
        .slice(0, 110);
    }

    const hf = window.BotAuditParser.analyzeHandleForensics(pageHandle);
    const profile = {
      schema_version: window.BotAuditParser.SCHEMA_VERSION,
      artist: state.artist,
      username: pageHandle,
      has_profile_photo: hasPhoto,
      account_private: isPrivate,
      post_count: parsedStats.post_count,
      followers_count: parsedStats.followers_count,
      following_count: parsedStats.following_count,
      bio_length: bioLen,
      username_digit_ratio: hf.digit_ratio,
      post_timestamps: [],
      collected_at: new Date().toISOString().slice(0, 10)
    };
    const src = { account_private: "dom_body_text" };
    if (hasPhoto !== null) src.has_profile_photo = "dom_header";
    ["post_count", "followers_count", "following_count"].forEach((f) => {
      if (profile[f] !== null) src[f] = "dom_header";
    });
    if (bioLen !== null) src.bio_length = "dom_header_estimate";
    profile.src = src;
    profile.deep_checked = [
      profile.post_count,
      profile.followers_count,
      profile.following_count,
      profile.bio_length
    ].every((v) => typeof v === "number");
    profile.tier = window.BotAuditParser.profileTier(profile);
    applyScore(profile, window.BotAuditParser.scoreFollowerProfile(profile));

    const existingKeyEarly = findProfileKeyInMap(pageHandle);
    const existingProfEarly = existingKeyEarly ? state.profilesMap.get(existingKeyEarly) : null;
    const resolvedLbl = resolveManualLabel(existingProfEarly || profile);
    if (resolvedLbl) {
      profile.manual_label = resolvedLbl;
    }

    // Attach transient UI preview fields strictly for the HUD inspector card
    profile.avatar_url =
      hasPhoto === true && /^(https?:|data:)/i.test(avatarSrc) ? avatarSrc : "";
    profile.bio_preview = bioPreview;

    // Extract up to 3 visible gallery post thumbnails for photographic HUD preview (transient only)
    const galleryThumbs = [];
    const mainRoot = document.querySelector("main");
    if (mainRoot && typeof mainRoot.querySelectorAll === "function") {
      const postImgs = mainRoot.querySelectorAll(
        'a[href*="/p/"] img, a[href*="/reel/"] img, article img'
      );
      for (const imgEl of postImgs) {
        if (!imgEl || typeof imgEl.getAttribute !== "function") continue;
        const srcUrl = imgEl.getAttribute("src") || "";
        if (
          srcUrl &&
          srcUrl !== avatarSrc &&
          /^(https?:|data:)/i.test(srcUrl) &&
          !galleryThumbs.includes(srcUrl)
        ) {
          galleryThumbs.push(srcUrl);
          if (galleryThumbs.length >= 3) break;
        }
      }
    }
    profile.gallery_thumbs = galleryThumbs;

    state.inspectedPageProfile = profile;

    // Auto-persist observed numbers ONLY for already-collected followers or tabs clicked from the active Followers modal
    const existingKey = findProfileKeyInMap(pageHandle);
    const isNotSelfArtist =
      !state.artist ||
      state.artist === "Audited_Account" ||
      state.artist.toLowerCase() !== pageHandle.toLowerCase();
    const isVerifiedFollowerClick = Boolean(
      state.artist &&
        state.artist !== "Audited_Account" &&
        state.artist.toLowerCase() !== pageHandle.toLowerCase() &&
        wasRecentlyClickedFromActiveFollowersModal(pageHandle)
    );
    if (isNotSelfArtist && (existingKey || (isVerifiedFollowerClick && profile.score_complete))) {
      const targetKey = existingKey || pageHandle;
      const prev = existingKey ? state.profilesMap.get(existingKey) : null;
      const merged = stripTransientUiFields(prev ? mergeObserved(prev, profile) : profile);
      merged.artist = state.artist;
      const sig = [
        merged.artist,
        merged.username.toLowerCase(),
        merged.tier,
        merged.score_pts,
        merged.post_count,
        merged.followers_count,
        merged.following_count,
        merged.bio_length
      ].join(":");
      if (
        !prev ||
        !prev.deep_checked ||
        merged.tier !== prev.tier ||
        merged.score_pts !== prev.score_pts ||
        sig !== state.lastAutoSyncedSig
      ) {
        state.profilesMap.set(targetKey, merged);
        state.dirtyUsernames.add(targetKey);
        state.recentLocalUpdates.set(merged.username.toLowerCase(), Date.now());
        savePersistedState(false);
        if (sig !== state.lastAutoSyncedSig && merged.tier >= 1) {
          state.lastAutoSyncedSig = sig;
          syncSingleProfileToServer(state.artist, merged);
          publishLiveDelta({
            type: "PROFILE_DELTA",
            artist: state.artist,
            username: merged.username.toLowerCase(),
            profile: merged,
            source: "tab_inspect"
          });
        }
      }
    }
    return profile;
  }

  function scanAndCollectDomFollowers() {
    if (!window.BotAuditParser) return { newlyAdded: 0, visibleSeen: 0 };
    const currentArtist = detectCurrentArtist();
    if (currentArtist !== state.artist) {
      state.artist = currentArtist;
      state.profilesMap.clear();
      state.dirtyUsernames.clear();
      loadPersistedState();
    }

    const dialog = document.querySelector('div[role="dialog"]');
    if (!dialog || !isFollowersModal(dialog)) {
      // No Followers modal open — inspect the currently viewed profile header instead of scraping non-follower dialogs
      inspectCurrentOpenProfilePage();
      updateFloatingDock();
      return { newlyAdded: 0, visibleSeen: 0 };
    }

    state.inspectedPageProfile = null;
    const anchors = dialog.querySelectorAll('a[href^="/"][href$="/"]');
    let newlyAdded = 0;
    let visibleSeen = 0;

    anchors.forEach((a) => {
      const href = a.getAttribute("href") || "";
      const parts = href.split("/").filter(Boolean);
      if (parts.length !== 1) return;
      const uname = parts[0].trim();
      if (!uname || uname === state.artist || IGNORED_PATHS.has(uname.toLowerCase())) {
        return;
      }

      const existingKey = findProfileKeyInMap(uname);
      if (existingKey) {
        const existingP = state.profilesMap.get(existingKey);
        if (existingP && !existingP.score_complete && state.globalObservedMap.has(uname.toLowerCase())) {
          state.profilesMap.set(existingKey, hydrateWithGlobalObserved(existingP));
        }
        visibleSeen++;
        return;
      }

      const rowContainer = a.closest("div") || a.parentElement;
      const img = rowContainer ? rowContainer.querySelector("img") : null;
      const imgSrc = img ? img.getAttribute("src") || "" : "";
      const hasPhoto = img ? !imgSrc.includes("44884218_345707102882519") : null;
      const hf = window.BotAuditParser.analyzeHandleForensics(uname);

      const baseProfile = {
        schema_version: window.BotAuditParser.SCHEMA_VERSION,
        artist: state.artist,
        username: uname,
        has_profile_photo: hasPhoto,
        account_private: null,
        post_count: null,
        followers_count: null,
        following_count: null,
        bio_length: null,
        username_digit_ratio: hf.digit_ratio,
        post_timestamps: [],
        src: hasPhoto === null ? {} : { has_profile_photo: "dom_list" },
        deep_checked: false,
        collected_at: new Date().toISOString().slice(0, 10)
      };
      baseProfile.tier = window.BotAuditParser.profileTier(baseProfile);
      applyScore(baseProfile, window.BotAuditParser.scoreFollowerProfile(baseProfile));
      const finalProfile = hydrateWithGlobalObserved(baseProfile);
      state.profilesMap.set(uname, finalProfile);
      state.dirtyUsernames.add(uname);
      newlyAdded++;
    });

    if (newlyAdded > 0) {
      state.newThisSession += newlyAdded;
      recordNewProfilesChecked(1);
      savePersistedState(true);
    }

    decorateFollowerRowsInDom();
    updateFloatingDock();
    return { newlyAdded, visibleSeen };
  }

  function findFollowerRowContainer(anchor, dialog) {
    if (!anchor || typeof anchor.parentElement === "undefined") return null;
    let cur = anchor.parentElement;
    let best = null;
    for (let depth = 0; depth < 6 && cur && cur !== dialog; depth++) {
      try {
        const links =
          typeof cur.querySelectorAll === "function"
            ? cur.querySelectorAll('a[href^="/"][href$="/"]')
            : [];
        if (links.length > 2) break;
        if (typeof cur.querySelector === "function" && cur.querySelector("button:not(.botaudit-row-act)")) {
          best = cur;
        }
      } catch (_e) {
        break;
      }
      cur = cur.parentElement;
    }
    return best || anchor.parentElement || null;
  }

  function confirmFollowerInline(uname, label) {
    if (!uname || !label) return;
    const isRo = state.lang === "ro";
    const key = String(uname).trim().toLowerCase();
    const nowIso = new Date().toISOString().slice(0, 10);
    const entry = {
      username: key,
      label: label,
      artist: state.artist,
      status: "available",
      labeled_on: nowIso,
      labeler: "extension_hud",
      evidence_note: `Marked inline from Followers modal HUD on @${state.artist}`
    };

    state.manualLabelsMap.set(key, Object.assign({}, entry, { localTs: Date.now() }));
    state.recentLocalUpdates.set(key, Date.now());
    state.batchConfirmedCount += 1;

    let cleanProfile = null;
    const existingKey = findProfileKeyInMap(uname);
    if (existingKey) {
      const nextP = stripTransientUiFields(Object.assign({}, state.profilesMap.get(existingKey)));
      nextP.artist = state.artist;
      nextP.manual_label = label;
      applyScore(nextP, window.BotAuditParser.scoreFollowerProfile(nextP));
      state.profilesMap.set(existingKey, nextP);
      cleanProfile = nextP;
    }

    mergeAndWriteLabelsStorage();
    savePersistedState(false);
    state.serverLiveCounts = null;

    publishLiveDelta({
      type: "PROFILE_DELTA",
      artist: state.artist,
      username: key,
      label: label,
      profile: cleanProfile,
      source: "inline_mark"
    });

    const bannerText =
      label === "bot"
        ? isRo
          ? `✓ Confirmat @${uname} ca BOT`
          : `✓ Confirmed @${uname} as BOT`
        : isRo
          ? `👤 Confirmat @${uname} ca OM REAL`
          : `👤 Confirmed @${uname} as HUMAN`;
    showLiveConfirmBanner(bannerText, label);
    decorateFollowerRowsInDom();
    updateFloatingDock(true);

    const serverPayload = Object.assign({}, entry, { profile: cleanProfile });
    sendBackgroundMessage("SAVE_LABEL_NOTE", serverPayload)
      .then((bgResp) => {
        if (bgResp && bgResp.data && bgResp.data.live_counts) {
          applyIncomingLiveDelta({
            artist: state.artist,
            liveCounts: bgResp.data.live_counts,
            source: "live_counts_only"
          });
        }
      })
      .catch(() => {
        fetch("http://127.0.0.1:8000/api/labels", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(serverPayload),
          keepalive: true
        })
          .then((r) => (r && r.ok ? r.json() : null))
          .then((data) => {
            if (data && data.live_counts) {
              applyIncomingLiveDelta({
                artist: state.artist,
                liveCounts: data.live_counts,
                source: "live_counts_only"
              });
            }
          })
          .catch(() => {});
      });
  }

  function decorateFollowerRowsInDom() {
    if (!window.BotAuditParser) return;
    const dialog = document.querySelector('div[role="dialog"]');
    if (!dialog) return;

    const anchors = dialog.querySelectorAll('a[href^="/"][href$="/"]');
    anchors.forEach((a) => {
      const href = a.getAttribute("href") || "";
      const parts = href.split("/").filter(Boolean);
      if (parts.length !== 1) return;
      const uname = parts[0];
      const existingKey = findProfileKeyInMap(uname);
      const profile = existingKey ? state.profilesMap.get(existingKey) : null;
      if (!profile || !a.textContent || a.textContent.trim() !== uname) return;

      const scored = window.BotAuditParser.scoreFollowerProfile(profile);
      const manualEntry = state.manualLabelsMap.get(uname.toLowerCase());
      const manualLabel = manualEntry ? manualEntry.label : (profile.manual_label || "");
      const showInlineActs = Boolean(
        manualLabel === "unsure" ||
          (!manualLabel &&
            (window.BotAuditParser.hasHeaderCounts(profile) || scored.complete || scored.conclusive))
      );
      const stateKey = `${profile.tier}|${scored.botLike}|${scored.conclusive}|${scored.scorePts}|${scored.maxScorePts}|${manualLabel}|${showInlineActs ? 1 : 0}|${state.lang}`;

      const rowEl = findFollowerRowContainer(a, dialog);
      if (rowEl && rowEl.classList) {
        if (manualLabel === "bot" || manualLabel === "human") {
          rowEl.classList.add("botaudit-row-confirmed");
        } else {
          rowEl.classList.remove("botaudit-row-confirmed");
        }
      }

      if (a.dataset.botauditBadged === stateKey) return;
      a.dataset.botauditBadged = stateKey;
      while (
        a.nextElementSibling &&
        a.nextElementSibling.classList &&
        (a.nextElementSibling.classList.contains("botaudit-row-badge") ||
          a.nextElementSibling.classList.contains("botaudit-row-actions"))
      ) {
        a.nextElementSibling.remove();
      }
      const isRo = state.lang === "ro";
      const badge = document.createElement("span");
      const ptsSuffix = scored.complete || scored.conclusive ? ` · ${scored.scorePts}p` : "";

      if (manualLabel === "bot") {
        badge.className = "botaudit-row-badge botaudit-badge-confirmed-bot";
        badge.textContent = isRo
          ? `🤖 BOT CONFIRMAT${ptsSuffix}`
          : `🤖 CONFIRMED BOT${ptsSuffix}`;
      } else if (manualLabel === "human") {
        badge.className = "botaudit-row-badge botaudit-badge-confirmed-human";
        badge.textContent = isRo
          ? `👤 OM REAL ✓${ptsSuffix}`
          : `👤 REAL PERSON ✓${ptsSuffix}`;
      } else if (manualLabel === "unsure") {
        badge.className = "botaudit-row-badge botaudit-badge-warn";
        badge.textContent = `🏷️ ${isRo ? "NESIGUR" : "UNSURE"}${ptsSuffix}`;
      } else if (scored.botLike === true) {
        badge.className = "botaudit-row-badge botaudit-badge-bot";
        const burstTag = scored.flags.same_day_post_burst ? " · 1–6p" : "";
        const privTag = profile.account_private ? " · 🔒" : "";
        badge.textContent = `🔴 BOT · ${scored.scorePts}p${burstTag}${privTag}`;
      } else if (scored.botLike === false) {
        if (scored.scorePts >= 35) {
          badge.className = "botaudit-row-badge botaudit-badge-warn";
          badge.textContent = isRo
            ? `🟡 VERIFICĂ · ${scored.scorePts}p`
            : `🟡 CHECK · ${scored.scorePts}p`;
        } else {
          badge.className = "botaudit-row-badge botaudit-badge-real";
          badge.textContent = `🟢 REAL · ${scored.scorePts}p ✓`;
        }
      } else if (window.BotAuditParser.hasHeaderCounts(profile) && scored.scorePts >= 35) {
        badge.className = "botaudit-row-badge botaudit-badge-warn";
        badge.textContent = isRo
          ? `🟡 VERIFICĂ · ${scored.scorePts}p · Deschide ↗`
          : `🟡 CHECK · ${scored.scorePts}p · Open ↗`;
      } else if (scored.scorePts > 0) {
        badge.className = "botaudit-row-badge botaudit-badge-suspect";
        badge.textContent = isRo
          ? `⚡ +${scored.scorePts}p indiciu · Deschide ↗`
          : `⚡ +${scored.scorePts}p clue · Open ↗`;
      } else {
        badge.className = "botaudit-row-badge botaudit-badge-unknown";
        badge.textContent = isRo ? "⚪ Deschide pt. cifre ↗" : "⚪ Open to check ↗";
      }
      a.insertAdjacentElement("afterend", badge);

      if (showInlineActs) {
        const actWrap = document.createElement("span");
        actWrap.className = "botaudit-row-actions";
        const safeUname = escapeHtml(uname);
        const botTip = isRo
          ? "Confirmă BOT fără să deschizi fila"
          : "Confirm BOT without opening a tab";
        const humTip = isRo
          ? "Confirmă OM REAL fără să deschizi fila"
          : "Confirm HUMAN without opening a tab";
        actWrap.innerHTML =
          `<button type="button" class="botaudit-row-act botaudit-row-act-bot" data-ba-act="bot" data-ba-user="${safeUname}" title="${botTip}">🤖</button>` +
          `<button type="button" class="botaudit-row-act botaudit-row-act-human" data-ba-act="human" data-ba-user="${safeUname}" title="${humTip}">👤</button>`;
        if (typeof badge.insertAdjacentElement === "function") {
          badge.insertAdjacentElement("afterend", actWrap);
        }
      }
    });
  }

  if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
    document.addEventListener(
      "click",
      (e) => {
        const tgt = e && e.target && typeof e.target.closest === "function" ? e.target : null;
        const actBtn = tgt ? tgt.closest(".botaudit-row-act") : null;
        if (!actBtn || typeof actBtn.getAttribute !== "function") return;
        if (typeof e.preventDefault === "function") e.preventDefault();
        if (typeof e.stopPropagation === "function") e.stopPropagation();
        const uname = actBtn.getAttribute("data-ba-user") || "";
        const act = actBtn.getAttribute("data-ba-act") || "";
        if (uname && (act === "bot" || act === "human")) {
          confirmFollowerInline(uname, act);
        }
      },
      true
    );
  }

  function scrollFollowersModalDown() {
    const dialog = document.querySelector('div[role="dialog"]');
    if (!dialog) return false;

    let scrolledAny = false;
    const divs = dialog.querySelectorAll("div");
    for (const d of divs) {
      const style = window.getComputedStyle(d);
      const overflowY = style ? style.overflowY : "";
      if (
        (overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay") &&
        d.scrollHeight > d.clientHeight + 10
      ) {
        d.scrollTop += 520 + Math.floor(Math.random() * 220);
        d.dispatchEvent(new Event("scroll", { bubbles: true }));
        scrolledAny = true;
      }
    }

    const anchors = dialog.querySelectorAll('a[href^="/"][href$="/"]');
    if (anchors.length > 0) {
      const lastAnchor = anchors[anchors.length - 1];
      try {
        lastAnchor.scrollIntoView({ behavior: "smooth", block: "end" });
        lastAnchor.dispatchEvent(
          new WheelEvent("wheel", { deltaY: 600, bubbles: true, cancelable: true })
        );
        scrolledAny = true;
      } catch (_e) {
        // Ignore
      }
    }
    return scrolledAny;
  }

  async function jumpToNextSearchSlice(explicitSlice) {
    if (!isAutomationEnabled()) return false;
    const dialog = document.querySelector('div[role="dialog"]');
    if (!dialog) {
      state.statusMessage =
        state.lang === "ro"
          ? "Deschide fereastra Followers pe Instagram mai întâi!"
          : "Open the Instagram Followers modal first!";
      updateFloatingDock();
      return false;
    }

    const searchInput =
      dialog.querySelector('input[type="text"]') ||
      dialog.querySelector("input[placeholder]") ||
      dialog.querySelector("input");

    if (!searchInput) {
      state.statusMessage =
        state.lang === "ro"
          ? "Căsuța Search nu este vizibilă — se folosește Fast-Forward prin scroll."
          : "Modal search input not visible — using Fast-Forward scroll.";
      updateFloatingDock();
      return false;
    }

    if (state.activeSlice && !state.completedSlices.includes(state.activeSlice)) {
      state.completedSlices.push(state.activeSlice);
    }

    const nextSlice =
      explicitSlice || SEARCH_SLICES.find((s) => !state.completedSlices.includes(s)) || "a";
    state.activeSlice = nextSlice;

    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value"
    )?.set;
    if (nativeSetter) {
      nativeSetter.call(searchInput, nextSlice);
    } else {
      searchInput.value = nextSlice;
    }
    searchInput.dispatchEvent(new Event("input", { bubbles: true }));
    searchInput.dispatchEvent(new Event("change", { bubbles: true }));

    state.statusMessage =
      state.lang === "ro"
        ? `🔤 Salt instant la litera „${nextSlice}” (${state.completedSlices.length}/36 felii gata)`
        : `🔤 Jumped to slice "${nextSlice}" (${state.completedSlices.length}/36 slices done)`;
    savePersistedState(true);
    updateFloatingDock();
    return true;
  }

  async function runCollectionLoop(mode) {
    if (!isAutomationEnabled()) {
      state.runningMode = null;
      state.statusMessage =
        state.lang === "ro"
          ? "Mod manual: derularea automată este oprită. Derulează tu lista."
          : "Manual mode: auto-scroll is off. Scroll the list yourself.";
      updateFloatingDock();
      return;
    }
    if (state.runningMode) {
      state.runningMode = null;
      state.fastForwarding = false;
      state.statusMessage =
        state.lang === "ro"
          ? `Pauză (${state.profilesMap.size} salvate în total — poți continua oricând!)`
          : `Paused (${state.profilesMap.size} total saved — resume anytime or tomorrow!)`;
      savePersistedState(true);
      updateFloatingDock();
      return;
    }

    const rl = getRateLimitStatus();
    if (rl.zone === "red") {
      state.pausedByRateLimit = true;
      state.statusMessage = state.lang === "ro" ? rl.nextScanRo : rl.nextScanEn;
      updateFloatingDock();
      return;
    }

    state.runningMode = mode;
    state.sessionTarget = mode === "deep_research" ? 200 : 50;
    state.newThisSession = 0;
    let idleStallTicks = 0;

    while (state.runningMode === mode && state.newThisSession < state.sessionTarget) {
      const currentRl = getRateLimitStatus();
      if (currentRl.zone === "red") {
        state.runningMode = null;
        state.pausedByRateLimit = true;
        break;
      }

      const { newlyAdded, visibleSeen } = scanAndCollectDomFollowers();
      scrollFollowersModalDown();

      if (newlyAdded === 0 && visibleSeen > 0) {
        state.fastForwarding = true;
        idleStallTicks++;
        state.statusMessage =
          state.lang === "ro"
            ? `⏩ Salt peste cele ${state.profilesMap.size} deja salvate (0 cost)... Noi azi: +${state.newThisSession}/${state.sessionTarget}`
            : `⏩ Skipping ${state.profilesMap.size} already-saved (0 cost)... New today: +${state.newThisSession}/${state.sessionTarget}`;
        updateFloatingDock();

        if (idleStallTicks >= 8) {
          idleStallTicks = 0;
          await jumpToNextSearchSlice();
        }
        await new Promise((r) => setTimeout(r, 1100));
        continue;
      }

      state.fastForwarding = false;
      idleStallTicks = 0;

      const [minWait, maxWait] =
        mode === "fast_funnel" && currentRl.zone === "green"
          ? [1600, 2400]
          : currentRl.delayRangeMs;
      const waitMs = minWait + Math.floor(Math.random() * Math.max(400, maxWait - minWait));
      const remainingNew = Math.max(0, state.sessionTarget - state.newThisSession);
      state.etaSeconds = Math.round((remainingNew / 6) * ((minWait + maxWait) / 1000));
      state.statusMessage =
        state.lang === "ro"
          ? `Noi azi: +${state.newThisSession}/${state.sessionTarget} (Total: ${state.profilesMap.size}) · ETA ~${Math.max(1, Math.ceil(state.etaSeconds / 60))}m`
          : `New today: +${state.newThisSession}/${state.sessionTarget} (Total: ${state.profilesMap.size}) · ETA ~${Math.max(1, Math.ceil(state.etaSeconds / 60))}m`;

      updateFloatingDock();
      await new Promise((r) => setTimeout(r, waitMs));
    }

    if (state.runningMode === mode) {
      state.runningMode = null;
      state.statusMessage =
        state.lang === "ro"
          ? `Lotul de azi e gata! (+${state.newThisSession} noi · ${state.profilesMap.size} salvate în total)`
          : `Today's batch complete! (+${state.newThisSession} new · ${state.profilesMap.size} total saved)`;
      savePersistedState(true);
      updateFloatingDock();
    }
  }

  async function sendToLocalAuditApp() {
    scanAndCollectDomFollowers();
    const profiles = Array.from(state.profilesMap.values()).map(buildCompactLocalProfile);
    if (profiles.length === 0) {
      state.statusMessage =
        state.lang === "ro"
          ? "Deschide lista de Urmăritori (Followers) mai întâi!"
          : "Open the Followers list first so accounts are visible!";
      updateFloatingDock();
      return { ok: false, error: state.statusMessage };
    }

    for (const p of profiles) {
      if (p) p.artist = state.artist;
    }
    const payload = {
      artist: state.artist,
      profiles,
      include_control: true,
      merge_checkpoint: true,
      completed_slices: state.completedSlices,
      last_username: state.lastUsername,
      next_max_id: state.nextMaxId,
      rate_limit_status: getRateLimitStatus()
    };

    state.statusMessage =
      state.lang === "ro"
        ? "⏳ Se trimit profilurile către aplicația locală..."
        : "⏳ Sending profiles to Audit App...";
    updateFloatingDock();

    // 1. Try sending via background service worker (exempt from page CSP/mixed content & opens tab natively)
    try {
      const bgResp = await sendBackgroundMessage("SEND_TO_AUDIT_APP", payload);
      if (bgResp && bgResp.ok) {
        const checkedN = Array.from(state.profilesMap.values()).filter(
          (p) => classifyProfileVerdict(p).decided
        ).length;
        state.statusMessage =
          state.lang === "ro"
            ? `Trimis ${profiles.length} profiluri (${checkedN} evaluate) în Audit App! Fila s-a deschis.`
            : `Sent ${profiles.length} profiles (${checkedN} scored) to Audit App! Tab opened.`;
        updateFloatingDock();
        return { ok: true, count: profiles.length };
      } else if (bgResp && bgResp.error) {
        throw new Error(bgResp.error);
      }
    } catch (_bgErr) {
      // Fall through to direct fetch fallback
    }

    // 2. Direct fetch fallback (tries 127.0.0.1 then localhost)
    const hosts = ["http://127.0.0.1:8000", "http://localhost:8000"];
    let lastError = null;
    for (const host of hosts) {
      try {
        const resp = await fetch(`${host}/api/extension-import`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        if (resp.ok) {
          const checkedN = Array.from(state.profilesMap.values()).filter(
            (p) => classifyProfileVerdict(p).decided
          ).length;
          state.statusMessage =
            state.lang === "ro"
              ? `Trimis ${profiles.length} profiluri (${checkedN} evaluate) către ${host}!`
              : `Sent ${profiles.length} profiles (${checkedN} scored) to ${host}!`;
          updateFloatingDock();
          window.open(`${host}/?source=extension&artist=${encodeURIComponent(state.artist)}`, "_blank");
          return { ok: true, count: profiles.length };
        }
        let detail = `HTTP ${resp.status}`;
        try {
          const errBody = await resp.json();
          if (errBody && errBody.error) detail = errBody.error;
        } catch (_e) {}
        lastError = detail;
      } catch (directErr) {
        lastError = String(directErr.message || directErr);
      }
    }

    state.statusMessage =
      state.lang === "ro"
        ? `Serverul local nu răspunde pe portul 8000 (${lastError}). Descarcă CSV ca rezervă!`
        : `Local server not reachable on port 8000 (${lastError}). Use Download CSV as backup!`;
    updateFloatingDock();
    return { ok: false, error: lastError };
  }

  function downloadCollectedCsv() {
    scanAndCollectDomFollowers();
    const scorable = window.BotAuditParser.partitionProfiles(
      Array.from(state.profilesMap.values())
    ).scorable;
    if (scorable.length === 0) {
      state.statusMessage =
        state.lang === "ro"
          ? "Niciun profil nu are încă toate cifrele. Deschide profilurile ca să fie citite."
          : "No profile has all of its numbers yet. Open profiles so they can be read.";
      updateFloatingDock();
      return;
    }
    const csvText = window.BotAuditParser.profilesToCsv(scorable, state.artist);
    const blob = new Blob([csvText], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `botaudit_${state.artist}_${scorable.length}_checked.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function enableDockHeaderDrag(dock) {
    let dragging = false;
    let offsetX = 0;
    let offsetY = 0;

    dock.addEventListener("mousedown", (e) => {
      const hdr = e.target && e.target.closest ? e.target.closest(".botaudit-dock-header") : null;
      if (!hdr) return;
      if (e.target.closest("button, input, label, a")) return;
      const rect = dock.getBoundingClientRect();
      dragging = true;
      offsetX = e.clientX - rect.left;
      offsetY = e.clientY - rect.top;
      e.preventDefault();
    });

    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const rect = dock.getBoundingClientRect();
      const maxX = Math.max(8, window.innerWidth - rect.width - 8);
      const maxY = Math.max(8, window.innerHeight - 48);
      const nextLeft = Math.min(maxX, Math.max(8, e.clientX - offsetX));
      const nextTop = Math.min(maxY, Math.max(8, e.clientY - offsetY));
      dock.style.right = "auto";
      dock.style.bottom = "auto";
      dock.style.left = `${Math.round(nextLeft)}px`;
      dock.style.top = `${Math.round(nextTop)}px`;
    });

    window.addEventListener("mouseup", () => {
      dragging = false;
    });

    dock.addEventListener("dblclick", (e) => {
      const hdr = e.target && e.target.closest ? e.target.closest(".botaudit-dock-header") : null;
      if (!hdr || e.target.closest("button")) return;
      dock.style.left = "";
      dock.style.top = "";
      dock.style.right = "16px";
      dock.style.bottom = "16px";
    });
  }

  function showLiveConfirmBanner(text, tone) {
    state.lastConfirmBanner = String(text || "");
    state.lastConfirmTone = String(tone || "unsure");
    state.lastConfirmUntil = Date.now() + 5500;
    if (state._confirmBannerTimer) {
      clearTimeout(state._confirmBannerTimer);
      state._confirmBannerTimer = null;
    }
    state._confirmBannerTimer = setTimeout(() => {
      state._confirmBannerTimer = null;
      if (Date.now() >= (state.lastConfirmUntil || 0)) {
        state.lastConfirmBanner = "";
        state.lastConfirmTone = "";
        state.lastConfirmUntil = 0;
        updateFloatingDock(true);
      }
    }, 5600);
    if (state._confirmBannerTimer && typeof state._confirmBannerTimer.unref === "function") {
      state._confirmBannerTimer.unref();
    }
  }

  function bindDockDelegatedEvents(dock) {
    if (!dock || typeof dock.addEventListener !== "function" || dock.__botauditBound) return;
    dock.__botauditBound = true;

    dock.addEventListener("click", (e) => {
      const tgt = e && e.target && typeof e.target.closest === "function" ? e.target : null;
      if (!tgt) return;

      if (tgt.closest("#botaudit-lang-btn")) {
        state.lang = state.lang === "en" ? "ro" : "en";
        savePersistedState(false);
        updateFloatingDock(true);
        return;
      }
      if (tgt.closest("#botaudit-artist-btn")) {
        state.targetPickerOpen = !state.targetPickerOpen;
        if (state.targetPickerOpen) refreshKnownTargetAccounts();
        updateFloatingDock(true);
        return;
      }
      if (tgt.closest("#botaudit-target-go-btn")) {
        const inp = dock.querySelector("#botaudit-target-input");
        if (inp && inp.value) setActiveTargetArtist(inp.value);
        return;
      }
      const targetPickBtn = tgt.closest("[data-target-artist]");
      if (targetPickBtn && typeof targetPickBtn.getAttribute === "function") {
        const chosen = targetPickBtn.getAttribute("data-target-artist");
        if (chosen) setActiveTargetArtist(chosen);
        return;
      }
      if (tgt.closest("#botaudit-min-btn")) {
        state.minimized = !state.minimized;
        updateFloatingDock(true);
        return;
      }
      if (tgt.closest("#botaudit-tools-toggle")) {
        const dialogOpen = Boolean(document.querySelector('div[role="dialog"]'));
        const curShow = state.toolsOpen || (dialogOpen && isAutomationEnabled());
        state.toolsOpen = !curShow;
        updateFloatingDock(true);
        return;
      }
      if (tgt.closest("#botaudit-fast-btn")) {
        runCollectionLoop("fast_funnel");
        return;
      }
      if (tgt.closest("#botaudit-deep-btn")) {
        runCollectionLoop("deep_research");
        return;
      }
      if (tgt.closest("#botaudit-slice-btn")) {
        jumpToNextSearchSlice();
        return;
      }
      if (tgt.closest("#botaudit-send-btn")) {
        sendToLocalAuditApp();
        return;
      }
      if (tgt.closest("#botaudit-csv-btn")) {
        downloadCollectedCsv();
        return;
      }
      if (tgt.closest("#botaudit-save-inspected-btn") && state.inspectedPageProfile) {
        if (typeof e.preventDefault === "function") e.preventDefault();
        if (typeof e.stopPropagation === "function") e.stopPropagation();
        const p = stripTransientUiFields(state.inspectedPageProfile);
        p.artist = state.artist;
        const existingK = findProfileKeyInMap(p.username);
        if (!existingK) {
          state.newThisSession += 1;
        }
        state.profilesMap.set(existingK || p.username, p);
        state.dirtyUsernames.add(existingK || p.username);
        try {
          localStorage.setItem(
            LAST_EVENT_KEY,
            JSON.stringify({ artist: state.artist, username: p.username, profile: p, ts: Date.now() })
          );
        } catch (_evtErr) {}
        savePersistedState(true);
        updateFloatingDock(true);
        return;
      }
      if (tgt.closest("#botaudit-mark-bot-btn") && state.inspectedPageProfile) {
        if (typeof e.preventDefault === "function") e.preventDefault();
        if (typeof e.stopPropagation === "function") e.stopPropagation();
        postLabelAndCloseTab(state.inspectedPageProfile.username, "bot");
        return;
      }
      if (tgt.closest("#botaudit-mark-human-btn") && state.inspectedPageProfile) {
        if (typeof e.preventDefault === "function") e.preventDefault();
        if (typeof e.stopPropagation === "function") e.stopPropagation();
        postLabelAndCloseTab(state.inspectedPageProfile.username, "human");
        return;
      }
      if (tgt.closest("#botaudit-mark-unsure-btn") && state.inspectedPageProfile) {
        if (typeof e.preventDefault === "function") e.preventDefault();
        if (typeof e.stopPropagation === "function") e.stopPropagation();
        postLabelAndCloseTab(state.inspectedPageProfile.username, "unsure");
        return;
      }
      const sliceEl = tgt.closest("[data-slice]");
      if (sliceEl && typeof sliceEl.getAttribute === "function") {
        const s = sliceEl.getAttribute("data-slice");
        if (s) jumpToNextSearchSlice(s);
      }
    });

    dock.addEventListener("change", (e) => {
      const tgt = e && e.target;
      if (tgt && tgt.id === "botaudit-auto-toggle") {
        setAutomationEnabled(Boolean(tgt.checked));
        if (tgt.checked) state.toolsOpen = true;
        state.runningMode = null;
        updateFloatingDock(true);
      }
    });

    dock.addEventListener("keydown", (e) => {
      const tgt = e && e.target;
      if (tgt && tgt.id === "botaudit-target-input" && e.key === "Enter") {
        if (typeof e.preventDefault === "function") e.preventDefault();
        if (typeof e.stopPropagation === "function") e.stopPropagation();
        if (tgt.value) setActiveTargetArtist(tgt.value);
      }
    });
  }

  function createFloatingDock() {
    if (document.getElementById("botaudit-floating-dock")) return;
    const dock = document.createElement("div");
    dock.id = "botaudit-floating-dock";
    dock.className = "botaudit-floating-dock";
    document.body.appendChild(dock);
    enableDockHeaderDrag(dock);
    bindDockDelegatedEvents(dock);
    updateFloatingDock(true);
  }

  function buildInspectedFlagChips(ipOrFlags, isRo) {
    if (!ipOrFlags) return "";
    const hasFlagsProp = typeof ipOrFlags === "object" && ipOrFlags.flags;
    const flags = hasFlagsProp ? ipOrFlags.flags : ipOrFlags;
    if (!flags) return "";

    const items = [];
    if (flags.no_photo === true) {
      items.push({ text: isRo ? "🚫 Fără poză" : "🚫 No photo", pts: "+20", amber: false });
    }
    if (flags.zero_posts === true) {
      items.push({ text: isRo ? "📭 0 postări" : "📭 0 posts", pts: "+25", amber: false });
    }
    if (flags.same_day_post_burst === true) {
      items.push({ text: isRo ? "📸 1–6 postări/zi" : "📸 1–6 setup", pts: "+15", amber: true });
    }
    if (
      hasFlagsProp &&
      flags.zero_posts === true &&
      flags.mass_following === true &&
      typeof ipOrFlags.following_count === "number" &&
      ipOrFlags.following_count >= 1000
    ) {
      items.push({ text: isRo ? "🐚 Cont gol >1k" : "🐚 Blank shell", pts: "+15", amber: true });
    }
    if (flags.mass_following === true) {
      items.push({ text: "🕸️ Mass-follow", pts: "+25", amber: false });
    }
    if (flags.extreme_mass_following === true) {
      items.push({ text: isRo ? "🔥 Raport extrem" : "🔥 Extreme 15×+", pts: "+30", amber: false });
    }
    if (flags.follow_exchange_ring === true) {
      items.push({ text: isRo ? "🔁 Rețea 5K+/5K+" : "🔁 5K+/5K+ Ring", pts: "+20", amber: false });
    }
    if (flags.low_audience_imbalance === true) {
      items.push({ text: isRo ? "📉 Audiență mică" : "📉 Low audience", pts: "+15", amber: true });
    }
    if (flags.shared_post_bot_signal === true) {
      items.push({ text: isRo ? "🔁 Postări distribuite" : "🔁 Shared-post bot", pts: "+20", amber: false });
    }
    if (flags.empty_bio === true) {
      items.push({ text: isRo ? "📝 Fără bio" : "📝 Empty bio", pts: "+10", amber: true });
    }
    if (flags.digit_heavy_username === true) {
      items.push({ text: isRo ? "🔢 Nume suspect" : "🔢 Farm handle", pts: "+20", amber: false });
    }
    if (flags.stitched_username === true) {
      items.push({ text: isRo ? "🔤 Nume cusut" : "🔤 Stitched", pts: "+10", amber: true });
    }
    if (flags.known_registry_bot === true) {
      items.push({ text: isRo ? "🚨 Registru bot" : "🚨 Registry bot", pts: "+25", amber: false });
    }
    if (
      flags.ratio_cluster_capped === true ||
      (hasFlagsProp && ipOrFlags.ratio_cluster_capped === true)
    ) {
      const capDetail =
        hasFlagsProp && typeof ipOrFlags.ratio_cluster_raw_pts === "number"
          ? `${ipOrFlags.ratio_cluster_raw_pts}→60p`
          : "max 60p";
      items.push({
        text: isRo ? "⚖️ Plafon raport" : "⚖️ Ratio cap",
        pts: capDetail,
        amber: true
      });
    }

    if (items.length === 0) {
      if (hasFlagsProp && ipOrFlags.score_complete === true) {
        return `<div class="botaudit-clean-pill">✓ ${
          isRo ? "0 indicii de bot — profil curat" : "0 bot clues triggered — clean profile"
        }</div>`;
      }
      return "";
    }
    return `<div class="botaudit-inspect-flags">${items
      .map(
        (it) =>
          `<span class="botaudit-flag-tag${it.amber ? " is-amber" : ""}"><span>${it.text}</span><b class="botaudit-flag-pts">${it.pts}</b></span>`
      )
      .join("")}</div>`;
  }

  function buildInspectedProfileCardHtml(ip, opts) {
    if (!ip || !ip.username) return "";
    const isRo = Boolean(opts && opts.isRo);
    const artist = String((opts && opts.artist) || state.artist || "Audited_Account");
    const alreadySaved = Boolean(opts && opts.alreadySaved);
    const activeLabel = String((opts && opts.activeLabel) || ip.manual_label || "");
    const isMarkingClosing = Boolean(opts && opts.isMarkingClosing);
    const labelStatusText = String((opts && opts.labelStatusText) || "");
    const labelStatusColor = String((opts && opts.labelStatusColor) || "#e2e8f0");

    const ptsVal =
      typeof ip.score_pts === "number" ? Math.min(100, Math.max(0, ip.score_pts)) : 0;
    const isBot = ip.bot_like === true;
    const isConfirmedBot = activeLabel === "bot";
    const isConfirmedHuman = activeLabel === "human";
    const unknown = ip.score_complete === false && !isBot && !isConfirmedBot && !isConfirmedHuman;
    const isWarn = !unknown && !isBot && !isConfirmedBot && !isConfirmedHuman && ptsVal >= 35;

    const cardVariant = isConfirmedBot
      ? "botaudit-inspect-bot"
      : isConfirmedHuman
        ? "botaudit-inspect-real"
        : unknown
          ? "botaudit-inspect-unknown"
          : isBot
            ? "botaudit-inspect-bot"
            : isWarn
              ? "botaudit-inspect-warn"
              : "botaudit-inspect-real";
    const pillVariant = isConfirmedBot
      ? "botaudit-score-bot"
      : isConfirmedHuman
        ? "botaudit-score-real"
        : unknown
          ? "botaudit-score-unknown"
          : isBot
            ? "botaudit-score-bot"
            : isWarn
              ? "botaudit-score-warn"
              : "botaudit-score-real";
    const pillText = isConfirmedBot
      ? `🤖 ${isRo ? "BOT CONFIRMAT" : "CONFIRMED BOT"} · ${ptsVal}p`
      : isConfirmedHuman
        ? `👤 ${isRo ? "OM REAL ✓" : "REAL PERSON ✓"} · ${ptsVal}p`
        : unknown
          ? `⚪ ${isRo ? "PARȚIAL" : "PARTIAL"} · ${ptsVal}p`
          : isBot
            ? `🔴 BOT · ${ptsVal}p`
            : isWarn
              ? `🟡 ${isRo ? "VERIFICĂ" : "CHECK"} · ${ptsVal}p`
              : `🟢 REAL · ${ptsVal}p`;

    const botActive = activeLabel === "bot";
    const humanActive = activeLabel === "human";
    const unsureActive = activeLabel === "unsure";
    const disabledAttr = isMarkingClosing ? "disabled" : "";

    // Avatar ring
    const initialChar = escapeHtml(String(ip.username || "?").slice(0, 1).toUpperCase());
    const avatarInnerHtml = ip.avatar_url
      ? `<img class="botaudit-avatar-img" src="${escapeHtml(ip.avatar_url)}" alt="">`
      : ip.has_profile_photo === false
        ? `<span class="botaudit-avatar-fallback" title="${
            isRo ? "Fără poză de profil" : "No profile photo"
          }">🚫</span>`
        : `<span class="botaudit-avatar-fallback">${initialChar}</span>`;

    const privPillHtml =
      ip.account_private === true
        ? `<span class="botaudit-priv-pill is-private">🔒 ${isRo ? "Privat" : "Private"}</span>`
        : ip.account_private === false
          ? `<span class="botaudit-priv-pill">🌐 Public</span>`
          : "";

    // 3-Column Metric Bento Grid (Posts | Followers | Following)
    const postsTone =
      ip.flags && ip.flags.zero_posts === true
        ? "is-danger"
        : ip.flags && ip.flags.same_day_post_burst === true
          ? "is-warn"
          : typeof ip.post_count === "number"
            ? "is-ok"
            : "";
    const postsLbl =
      ip.post_count === 1 ? (isRo ? "Postare" : "Post") : isRo ? "Postări" : "Posts";

    const flwrsTone =
      typeof ip.followers_count === "number" &&
      ip.followers_count < 15 &&
      ip.flags &&
      ip.flags.mass_following === true
        ? "is-warn"
        : typeof ip.followers_count === "number"
          ? "is-ok"
          : "";
    const flwrsLbl =
      ip.followers_count === 1
        ? isRo
          ? "Urmăritor"
          : "Follower"
        : isRo
          ? "Urmăritori"
          : "Followers";

    const flwingTone =
      ip.flags && ip.flags.mass_following === true
        ? "is-danger"
        : typeof ip.following_count === "number"
          ? "is-ok"
          : "";
    const flwingLbl = isRo ? "Urmăriri" : "Following";

    // Forensic Detail Strip (Ratio, Photo, Bio Length, Digit %)
    const ratioPill = formatFollowRatioPill(
      ip.followers_count,
      ip.following_count,
      ip.flags,
      isRo
    );
    const photoPill =
      ip.has_profile_photo === true
        ? `<span class="botaudit-forensic-pill is-ok">🖼️ ${isRo ? "Poză ✓" : "Photo ✓"}</span>`
        : ip.has_profile_photo === false
          ? `<span class="botaudit-forensic-pill is-danger">🚫 ${
              isRo ? "Fără poză" : "No photo"
            }</span>`
          : "";
    const bioPill =
      typeof ip.bio_length === "number"
        ? ip.bio_length > 4
          ? `<span class="botaudit-forensic-pill is-ok">📝 Bio <b>${ip.bio_length}c</b></span>`
          : ip.bio_length === 0
            ? `<span class="botaudit-forensic-pill is-danger">🚫 ${
                isRo ? "Bio gol" : "Empty bio"
              }</span>`
            : `<span class="botaudit-forensic-pill is-warn">⚠️ Bio <b>${ip.bio_length}c</b></span>`
        : `<span class="botaudit-forensic-pill">📝 Bio ?</span>`;

    const digitPct =
      typeof ip.username_digit_ratio === "number"
        ? Math.round(ip.username_digit_ratio * 100)
        : 0;
    const digitPill =
      digitPct > 0 || (ip.flags && ip.flags.digit_heavy_username)
        ? `<span class="botaudit-forensic-pill ${
            ip.flags && ip.flags.digit_heavy_username ? "is-danger" : ""
          }">🔢 <b>${digitPct}%</b> ${isRo ? "cifre" : "digits"}</span>`
        : "";

    const bioPreviewHtml =
      ip.bio_preview && String(ip.bio_preview).trim().length > 0
        ? `<div class="botaudit-bio-preview" title="${escapeHtml(ip.bio_preview)}">“${escapeHtml(
            ip.bio_preview
          )}”</div>`
        : "";

    const galleryThumbs = Array.isArray(ip.gallery_thumbs)
      ? ip.gallery_thumbs.filter((u) => typeof u === "string" && u.length > 0).slice(0, 3)
      : [];
    const galleryStripHtml =
      galleryThumbs.length > 0
        ? `<div class="botaudit-photo-strip" title="${
            isRo ? "Ultimele postări vizibile pe profil" : "Latest visible photos on this profile"
          }">
            <span class="botaudit-photo-strip-lbl">🎞️ ${isRo ? "Galerie" : "Feed"}</span>
            <div class="botaudit-photo-frames">
              ${galleryThumbs
                .map(
                  (src) =>
                    `<div class="botaudit-photo-frame"><img class="botaudit-strip-thumb" src="${escapeHtml(
                      src
                    )}" alt=""></div>`
                )
                .join("")}
            </div>
          </div>`
        : "";

    // 100-Point Visual Score Gauge with 60p Cutoff Marker
    const gaugeBg = (isBot || isConfirmedBot)
      ? "linear-gradient(90deg, #f59e0b 0%, #fb7185 55%, #e11d48 100%)"
      : isConfirmedHuman
        ? "linear-gradient(90deg, #10b981 0%, #34d399 100%)"
        : isWarn
          ? "linear-gradient(90deg, #34d399 0%, #fbbf24 100%)"
          : "linear-gradient(90deg, #10b981 0%, #34d399 100%)";
    const cutoffText = (isBot || isConfirmedBot)
      ? isRo
        ? "Peste prag (>60p)"
        : "Over >60p bot line"
      : isRo
        ? "Prag bot: >60p"
        : "Bot line: >60p";

    const safeArtist = escapeHtml(artist);
    const hintHtml = labelStatusText
      ? `<span style="color:${escapeHtml(labelStatusColor)};font-weight:700;">${escapeHtml(
          labelStatusText
        )}</span>`
      : activeLabel
        ? `<span>🏷️ ${isRo ? "Confirmat" : "Confirmed"}: <b style="color:#f8fafc;">${escapeHtml(
            activeLabel.toUpperCase()
          )}</b> · ${isRo ? "click / taste B·H·U" : "click or keys B·H·U"}</span>`
        : `<span>⚡ ${
            isRo
              ? `Salvează în @${safeArtist} & închide fila · Taste: B / H / U`
              : `Saves to @${safeArtist} & closes tab · Keys: B / H / U`
          }</span>`;

    return `
      <div class="botaudit-inspect-card ${cardVariant}">
        <div class="botaudit-inspect-top">
          <div class="botaudit-ident-left">
            <div class="botaudit-avatar-ring" data-initial="${initialChar}">
              ${avatarInnerHtml}
            </div>
            <div class="botaudit-ident-col">
              <span class="botaudit-inspect-handle" title="@${escapeHtml(ip.username)}">@${formatHandleWithDigits(
                ip.username
              )}</span>
              <div class="botaudit-ident-meta">
                ${privPillHtml}
                <button id="botaudit-save-inspected-btn" class="botaudit-sync-chip ${
                  alreadySaved ? "is-synced" : ""
                }" title="${
                  alreadySaved
                    ? isRo
                      ? `Sincronizat în @${safeArtist} (click pt. re-salvare fără închidere)`
                      : `Synced in @${safeArtist} (click to re-save without closing)`
                    : isRo
                      ? `Salvează în @${safeArtist} fără a închide fila`
                      : `Save to @${safeArtist} without closing tab`
                }">
                  ${alreadySaved ? "✓ Synced" : "+ Save"}
                </button>
              </div>
            </div>
          </div>
          <span class="botaudit-score-pill ${pillVariant}">${pillText}</span>
        </div>

        <div class="botaudit-metric-bento">
          <div class="botaudit-metric-tile ${postsTone}">
            <span class="botaudit-metric-val">${fmtStat(ip.post_count)}</span>
            <span class="botaudit-metric-lbl">${postsLbl}</span>
          </div>
          <div class="botaudit-metric-tile ${flwrsTone}">
            <span class="botaudit-metric-val">${fmtStat(ip.followers_count)}</span>
            <span class="botaudit-metric-lbl">${flwrsLbl}</span>
          </div>
          <div class="botaudit-metric-tile ${flwingTone}">
            <span class="botaudit-metric-val">${fmtStat(ip.following_count)}</span>
            <span class="botaudit-metric-lbl">${flwingLbl}</span>
          </div>
        </div>

        <div class="botaudit-forensic-strip">
          ${ratioPill}
          ${photoPill}
          ${bioPill}
          ${digitPill}
        </div>

        ${bioPreviewHtml}
        ${galleryStripHtml}

        <div class="botaudit-score-gauge">
          <div class="botaudit-gauge-head">
            <span>${isRo ? "Scor Indicii" : "Checklist"}: <b>${ptsVal}</b>/100p</span>
            <span class="botaudit-gauge-cutoff-lbl">${cutoffText}</span>
          </div>
          <div class="botaudit-gauge-track">
            <div class="botaudit-gauge-fill" style="width:${ptsVal}%;background:${gaugeBg};"></div>
            <div class="botaudit-gauge-cutoff" title="60 pts Bot-Like Cutoff"></div>
          </div>
        </div>

        ${buildInspectedFlagChips(ip, isRo)}

        <div class="botaudit-verdict-row">
          <button id="botaudit-mark-bot-btn" class="botaudit-verdict-btn botaudit-verdict-bot ${
            botActive ? "is-selected" : ""
          }" ${disabledAttr} title="${
            isRo
              ? "Salvează ca BOT pe backend și închide această filă (Tasta B)"
              : "Save as BOT on backend & close this browser tab (Key: B)"
          }">
            <span>${botActive ? "✓ BOT" : isRo ? "🤖 BOT" : "🤖 Mark BOT"}</span>
            <kbd class="botaudit-kbd">B</kbd>
          </button>
          <button id="botaudit-mark-human-btn" class="botaudit-verdict-btn botaudit-verdict-human ${
            humanActive ? "is-selected" : ""
          }" ${disabledAttr} title="${
            isRo
              ? "Salvează ca OM REAL pe backend și închide această filă (Tasta H)"
              : "Save as HUMAN on backend & close this browser tab (Key: H)"
          }">
            <span>${humanActive ? "✓ HUMAN" : isRo ? "👤 OM REAL" : "👤 Mark HUMAN"}</span>
            <kbd class="botaudit-kbd">H</kbd>
          </button>
          <button id="botaudit-mark-unsure-btn" class="botaudit-verdict-btn botaudit-verdict-unsure ${
            unsureActive ? "is-selected" : ""
          }" ${disabledAttr} title="${
            isRo
              ? "Marchează Nesigur (?) pe backend și închide această filă (Tasta U)"
              : "Save as Unsure (?) on backend & close this browser tab (Key: U)"
          }">
            <span>${unsureActive ? "✓?" : "?"}</span>
            <kbd class="botaudit-kbd">U</kbd>
          </button>
        </div>

        <div id="botaudit-label-status" class="botaudit-inspect-hint">${hintHtml}</div>
      </div>
    `;
  }

  function postLabelAndCloseTab(uname, label) {
    if (!uname || state.isMarkingClosing) return;
    state.isMarkingClosing = true;
    const isRo = state.lang === "ro";

    // Refresh DOM profile inspection once more so all observed numbers are captured
    inspectCurrentOpenProfilePage();

    const key = uname.toLowerCase();
    const nowIso = new Date().toISOString().slice(0, 10);
    const entry = {
      username: key,
      label: label,
      artist: state.artist,
      status: "available",
      labeled_on: nowIso,
      labeler: "extension_hud",
      evidence_note: `Marked from Instagram HUD on @${state.artist}`
    };

    // 1. Update in-memory maps AND merge inspected profile's observed metrics into artist map
    state.manualLabelsMap.set(key, Object.assign({}, entry, { localTs: Date.now() }));
    state.recentLocalUpdates.set(key, Date.now());
    state.batchConfirmedCount += 1;
    let profileToSync = null;

    if (
      state.inspectedPageProfile &&
      (state.inspectedPageProfile.username || "").toLowerCase() === key
    ) {
      const ipCopy = stripTransientUiFields(
        Object.assign({}, state.inspectedPageProfile, {
          artist: state.artist,
          manual_label: label
        })
      );
      const existingKey = state.profilesMap.has(ipCopy.username)
        ? ipCopy.username
        : Array.from(state.profilesMap.keys()).find((k) => k.toLowerCase() === key);

      if (existingKey) {
        const merged = stripTransientUiFields(
          mergeObserved(state.profilesMap.get(existingKey), ipCopy)
        );
        merged.artist = state.artist;
        merged.manual_label = label;
        state.profilesMap.set(existingKey, merged);
        profileToSync = merged;
      } else {
        state.newThisSession += 1;
        state.profilesMap.set(ipCopy.username, ipCopy);
        profileToSync = ipCopy;
      }
      state.inspectedPageProfile.manual_label = label;
    } else {
      const existingKey = state.profilesMap.has(uname)
        ? uname
        : Array.from(state.profilesMap.keys()).find((k) => k.toLowerCase() === key);
      if (existingKey) {
        const existingP = stripTransientUiFields(state.profilesMap.get(existingKey));
        existingP.manual_label = label;
        state.profilesMap.set(existingKey, existingP);
        profileToSync = existingP;
      }
    }

    // 2. Merge-persist manual labels (never erasing other tabs' labels) and broadcast live delta
    mergeAndWriteLabelsStorage();
    savePersistedState(false);
    publishLiveDelta({
      type: "PROFILE_DELTA",
      artist: state.artist,
      username: key,
      label: label,
      profile: profileToSync,
      source: "mark"
    });

    // 3. Render instant visual confirmation before tab closes
    state.serverLiveCounts = null;
    const labelUpper = label.toUpperCase();
    state.labelStatusText = isRo
      ? `✓ Salvat ${labelUpper} · Se închide fila...`
      : `✓ Saved ${labelUpper} · Closing tab...`;
    state.labelStatusColor =
      label === "bot" ? "#fda4af" : label === "human" ? "#6ee7b7" : "#e2e8f0";
    updateFloatingDock(true);
    decorateFollowerRowsInDom();

    // Safety net: if Chrome keeps the tab open (e.g., last tab or user re-focused it),
    // restore button interactivity and keep the confirmed state visible.
    const closeUnlockTimer = setTimeout(() => {
      if (
        state.isMarkingClosing &&
        state.inspectedPageProfile &&
        (state.inspectedPageProfile.username || "").toLowerCase() === key
      ) {
        state.isMarkingClosing = false;
        state.labelStatusText = isRo
          ? `✓ Confirmat ${labelUpper} · Salvat în @${state.artist}`
          : `✓ Confirmed ${labelUpper} · Saved to @${state.artist}`;
        state.labelStatusColor =
          label === "bot" ? "#fda4af" : label === "human" ? "#6ee7b7" : "#e2e8f0";
        updateFloatingDock(true);
      }
    }, 900);
    if (closeUnlockTimer && typeof closeUnlockTimer.unref === "function") {
      closeUnlockTimer.unref();
    }

    // 4. Delegate Task 1 (atomic backend checkpoint + label sync) & Task 2 (close active tab) to background worker
    sendBackgroundMessage("MARK_PROFILE_AND_CLOSE", {
      artist: state.artist,
      profile: profileToSync,
      labelEntry: entry,
      closeTab: true
    })
      .then((bgResp) => {
        if (bgResp && bgResp.data && bgResp.data.live_counts) {
          applyIncomingLiveDelta({
            artist: state.artist,
            liveCounts: bgResp.data.live_counts,
            source: "live_counts_only"
          });
        }
        if (!bgResp || !bgResp.closedTab) {
          try {
            window.close();
          } catch (_e) {}
        }
      })
      .catch(() => {
        // Fallback if service worker needs reload: fire keepalive requests & close window
        if (profileToSync && profileToSync.username) {
          fetch("http://127.0.0.1:8000/api/checkpoint", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              artist: state.artist,
              profiles: [profileToSync],
              last_username: profileToSync.username
            }),
            keepalive: true
          }).catch(() => {});
        }
        fetch("http://127.0.0.1:8000/api/labels", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(entry),
          keepalive: true
        })
          .catch(() => {})
          .finally(() => {
            sendBackgroundMessage("CLOSE_CURRENT_TAB", {}).catch(() => {});
            const fallbackTimer = setTimeout(() => {
              clearTimeout(closeUnlockTimer);
              try {
                window.close();
              } catch (_e) {}
              // If browser prevented window.close() because extension wasn't reloaded yet, unlock UI
              state.isMarkingClosing = false;
              state.labelStatusText = isRo
                ? `✓ Salvat ${labelUpper} (reîncarcă extensia pt. auto-close)`
                : `✓ Saved ${labelUpper} (reload extension for auto-close)`;
              updateFloatingDock(true);
            }, 180);
            if (fallbackTimer && typeof fallbackTimer.unref === "function") {
              fallbackTimer.unref();
            }
          });
      });
  }

  function computeDisplayCounts() {
    const profiles = Array.from(state.profilesMap.values());
    let totalCount = profiles.length;
    const verdicts = profiles.map((p) => classifyProfileVerdict(p));
    const checkedProfiles = profiles.filter((_, i) => verdicts[i].decided);
    let checkedCount = checkedProfiles.length;
    let botsCount = verdicts.filter((v) => v.decided && v.source === "auto" && v.isBot).length;
    let suspiciousCount = verdicts.filter((v) => v.decided && v.verdict === "suspicious").length;
    let autoHumansCount = verdicts.filter(
      (v) => v.decided && v.source === "auto" && v.verdict === "human"
    ).length;
    let decidedConfirmedBots = verdicts.filter(
      (v) => v.decided && v.source === "manual" && v.isBot
    ).length;
    let decidedConfirmedHumans = verdicts.filter(
      (v) => v.decided && v.source === "manual" && !v.isBot
    ).length;
    const confirmedBotsCount = profiles.filter((p) => resolveManualLabel(p) === "bot").length;
    const confirmedHumansCount = profiles.filter((p) => resolveManualLabel(p) === "human").length;
    let scoredBotsCount = verdicts.filter((v) => v.decided && v.isBot).length;
    let scoredHumansCount = verdicts.filter(
      (v) => v.decided && !v.isBot && v.verdict !== "suspicious"
    ).length;
    const overriddenHumansCount = verdicts.filter((v) => v.decided && v.overriddenAutoBot).length;
    const overriddenBotsCount = verdicts.filter((v) => v.decided && v.overriddenAutoHuman).length;
    const unscoredConfirmedBots = profiles.filter(
      (p, i) => !verdicts[i].decided && resolveManualLabel(p) === "bot"
    ).length;
    const unscoredConfirmedHumans = profiles.filter(
      (p, i) => !verdicts[i].decided && resolveManualLabel(p) === "human"
    ).length;
    let burstCount = checkedProfiles.filter((p) => p.same_day_post_burst === true).length;
    let privBotsCount = checkedProfiles.filter(
      (p) => p.account_private === true && classifyProfileVerdict(p).isBot
    ).length;
    // Digit patterns only: the stitched-punctuation rule also flags many real people, so it is not counted here.
    const digitCount = profiles.filter(
      (p) => p.username_digit_ratio > 0.4 || /\d{5,}/.test(p.username)
    ).length;
    const massFollowCount = checkedProfiles.filter(
      (p) => p.flags && p.flags.mass_following === true
    ).length;
    const noPhotoCount = profiles.filter((p) => p.has_profile_photo === false).length;

    const srv = state.serverLiveCounts;
    if (
      srv &&
      srv.artist &&
      state.artist &&
      String(srv.artist).toLowerCase() === state.artist.toLowerCase()
    ) {
      totalCount = Math.max(totalCount, srv.total_saved || 0);
      const srvTotalConfirmed = (srv.confirmed_bots || 0) + (srv.confirmed_humans || 0);
      const locTotalConfirmed = decidedConfirmedBots + decidedConfirmedHumans;
      const srvAhead =
        (srv.checked_count || 0) > checkedCount ||
        ((srv.checked_count || 0) === checkedCount && srvTotalConfirmed > locTotalConfirmed);
      if (srvAhead) {
        checkedCount = srv.checked_count;
        scoredBotsCount = srv.bots_count;
        scoredHumansCount = srv.humans_count;
        if (typeof srv.suspicious_count === "number") suspiciousCount = srv.suspicious_count;
        decidedConfirmedBots = srv.confirmed_bots;
        botsCount = srv.auto_bots;
        decidedConfirmedHumans = srv.confirmed_humans;
        autoHumansCount = srv.auto_humans;
        if (typeof srv.burst_count === "number") burstCount = srv.burst_count;
        if (typeof srv.priv_bots_count === "number") privBotsCount = srv.priv_bots_count;
      }
    }

    const unscoredCount = Math.max(0, totalCount - checkedCount);
    const botsDenom = checkedCount;
    const botPct = botsDenom > 0 ? ((scoredBotsCount / botsDenom) * 100).toFixed(1) : "-";
    const suspiciousPct =
      botsDenom > 0 ? ((suspiciousCount / botsDenom) * 100).toFixed(1) : "-";
    const humanPct = botsDenom > 0 ? ((scoredHumansCount / botsDenom) * 100).toFixed(1) : "-";

    return {
      totalCount,
      checkedCount,
      unscoredCount,
      botsCount,
      suspiciousCount,
      autoHumansCount,
      decidedConfirmedBots,
      decidedConfirmedHumans,
      confirmedBotsCount,
      confirmedHumansCount,
      scoredBotsCount,
      scoredHumansCount,
      overriddenHumansCount,
      overriddenBotsCount,
      unscoredConfirmedBots,
      unscoredConfirmedHumans,
      botsDenom,
      botPct,
      suspiciousPct,
      humanPct,
      burstCount,
      privBotsCount,
      digitCount,
      massFollowCount,
      noPhotoCount
    };
  }

  function updateFloatingDock(force = false) {
    const dock = document.getElementById("botaudit-floating-dock");
    if (!dock) return;
    bindDockDelegatedEvents(dock);

    const {
      totalCount,
      checkedCount,
      unscoredCount,
      botsCount,
      suspiciousCount,
      autoHumansCount,
      decidedConfirmedBots,
      decidedConfirmedHumans,
      confirmedBotsCount,
      confirmedHumansCount,
      scoredBotsCount,
      scoredHumansCount,
      overriddenHumansCount,
      overriddenBotsCount,
      unscoredConfirmedBots,
      unscoredConfirmedHumans,
      botsDenom,
      botPct,
      suspiciousPct,
      humanPct,
      burstCount,
      privBotsCount,
      digitCount,
      massFollowCount,
      noPhotoCount
    } = computeDisplayCounts();
    const automationOn = isAutomationEnabled();

    const rl = getRateLimitStatus();
    const isRo = state.lang === "ro";

    if (state.minimized) {
      const minKey = `MIN|${EXT_VERSION}|${state.lang}|${state.artist}|${checkedCount}|${totalCount}`;
      if (!force && state._lastDockRenderKey === minKey && dock.innerHTML) {
        return;
      }
      state._lastDockRenderKey = minKey;
      dock.classList.add("botaudit-minimized");
      dock.innerHTML = `
        <div class="botaudit-dock-header">
          <span class="botaudit-dock-title">
            <span class="botaudit-brand-dot"></span>
            <span>@${escapeHtml(state.artist)}</span>
            <span class="botaudit-ver-pill" style="font-size:9px;padding:1px 5px;" title="${isRo ? "Versiune Extensie (Manifest)" : "Extension Build Version"}">v${EXT_VERSION}</span>
            <span style="color:#94a3b8;font-weight:600;font-size:11px;">(${checkedCount}/${totalCount})</span>
          </span>
          <button id="botaudit-min-btn" class="botaudit-icon-btn" title="${isRo ? "Extinde" : "Expand"}">▢</button>
        </div>
      `;
      return;
    }

    dock.classList.remove("botaudit-minimized");

    const speedClass =
      rl.zone === "red"
        ? "botaudit-speed-red"
        : rl.zone === "yellow"
          ? "botaudit-speed-yellow"
          : "botaudit-speed-green";
    const speedIcon = rl.zone === "red" ? "🔴" : rl.zone === "yellow" ? "🟡" : "🟢";

    const hrPct = Math.min(100, Math.round((rl.checksLastHour / 180) * 100));
    const dayPct = Math.min(100, Math.round((rl.checksLastDay / 600) * 100));
    const hrColor =
      rl.checksLastHour >= 180 ? "#fb7185" : rl.checksLastHour >= 120 ? "#fbbf24" : "#34d399";
    const dayColor =
      rl.checksLastDay >= 600 ? "#fb7185" : rl.checksLastDay >= 450 ? "#fbbf24" : "#60a5fa";

    const nextSafeClock = rl.msUntilSafe > 0 ? formatClockTime(Date.now() + rl.msUntilSafe) : "";
    const speedHeadline =
      rl.zone === "red"
        ? `${speedIcon} ${isRo ? "PAUZĂ" : "COOLDOWN"}${
            rl.msUntilSafe > 0
              ? ` · ${formatHoursMinutes(rl.msUntilSafe, isRo)}${nextSafeClock ? ` (${nextSafeClock})` : ""}`
              : ""
          }`
        : rl.zone === "yellow"
          ? `${speedIcon} ${isRo ? "ATENȚIE" : "CAUTION"} · +${rl.remHourSafe}/${isRo ? "oră" : "hr"}`
          : `${speedIcon} ${isRo ? "ZONĂ SIGURĂ" : "SAFE ZONE"} · +${rl.remHourSafe}/${isRo ? "oră" : "hr"}`;

    const sessionPct = Math.min(100, Math.round((state.newThisSession / state.sessionTarget) * 100));
    const sampleTarget = 383; // 95% confidence ±5% margin of error for 120K audience
    const samplePct = Math.min(100, Math.round((checkedCount / sampleTarget) * 100));

    // Render 36-slice visual map (a-z, 0-9)
    const sliceDotsHtml = SEARCH_SLICES.map((s) => {
      const isDone = state.completedSlices.includes(s);
      const isAct = state.activeSlice === s;
      const cls = isAct
        ? "botaudit-slice-dot botaudit-slice-active"
        : isDone
          ? "botaudit-slice-dot botaudit-slice-done"
          : "botaudit-slice-dot";
      return `<span class="${cls}" data-slice="${s}" title="Click to jump to '${s}'" style="cursor:pointer;">${s}</span>`;
    }).join("");

    const ip = state.inspectedPageProfile;
    const isInspectingFollower = Boolean(
      ip && ip.username && ip.username.toLowerCase() !== String(state.artist || "").toLowerCase()
    );
    const pageHandle = getPagePathHandle();
    const showSwitchToPage = Boolean(
      pageHandle && state.artist && pageHandle.toLowerCase() !== String(state.artist || "").toLowerCase()
    );
    let inspectedActiveLabel = "";
    let inspectedAlreadySaved = false;
    let inspectedBannerHtml = "";

    if (isInspectingFollower) {
      const currentUname = (ip.username || "").toLowerCase();
      const existingLabelEntry = state.manualLabelsMap.get(currentUname);
      inspectedActiveLabel = existingLabelEntry ? existingLabelEntry.label : ip.manual_label || "";
      inspectedAlreadySaved = Boolean(findProfileKeyInMap(ip.username));
    }

    const dialogOpen = Boolean(document.querySelector('div[role="dialog"]'));
    const showToolsBody = state.toolsOpen || (dialogOpen && automationOn);
    const hasCustomStatus =
      Boolean(state.statusMessage) && state.statusMessage !== DEFAULT_READY_STATUS;
    const hasActiveConfirmBanner = Boolean(
      state.lastConfirmBanner && Date.now() < (state.lastConfirmUntil || 0)
    );
    const confirmToastHtml = hasActiveConfirmBanner
      ? `<div class="botaudit-confirm-toast is-${escapeHtml(state.lastConfirmTone || "unsure")}">${escapeHtml(state.lastConfirmBanner)}</div>`
      : "";
    const openTabsCount = Number(state.openFollowerTabs || 0);
    const waveStripHtml =
      openTabsCount > 0
        ? `<div class="botaudit-wave-strip"><span>📑 <b>${openTabsCount}</b> ${
            isRo
              ? openTabsCount === 1
                ? "filă deschisă"
                : "file deschise"
              : openTabsCount === 1
                ? "tab open"
                : "tabs open"
          }</span>${
            state.batchConfirmedCount > 0
              ? `<span class="botaudit-wave-done">✓ ${state.batchConfirmedCount} ${
                  isRo ? "confirmate" : "confirmed"
                }</span>`
              : ""
          }</div>`
        : "";

    const ipKey = isInspectingFollower
      ? [
          ip.username,
          ip.post_count,
          ip.followers_count,
          ip.following_count,
          ip.bio_length,
          ip.score_pts,
          ip.bot_like,
          ip.score_complete,
          ip.account_private,
          ip.has_profile_photo,
          ip.avatar_url || "",
          ip.bio_preview || "",
          (ip.gallery_thumbs || []).length,
          inspectedAlreadySaved ? 1 : 0,
          inspectedActiveLabel,
          state.isMarkingClosing ? 1 : 0,
          state.labelStatusText || "",
          state.labelStatusColor || ""
        ].join(":")
      : "NONE";

    const renderKey = [
      "EXP",
      EXT_VERSION,
      state.lang,
      state.artist,
      speedHeadline,
      rl.checksLastHour,
      rl.checksLastDay,
      openTabsCount,
      state.batchConfirmedCount,
      scoredBotsCount,
      suspiciousCount,
      botsDenom,
      decidedConfirmedBots,
      botsCount,
      confirmedBotsCount,
      overriddenHumansCount,
      unscoredConfirmedBots,
      scoredHumansCount,
      decidedConfirmedHumans,
      autoHumansCount,
      confirmedHumansCount,
      overriddenBotsCount,
      unscoredConfirmedHumans,
      totalCount,
      unscoredCount,
      state.newThisSession,
      state.sessionTarget,
      burstCount,
      privBotsCount,
      samplePct,
      digitCount,
      massFollowCount,
      noPhotoCount,
      showToolsBody ? 1 : 0,
      automationOn ? 1 : 0,
      state.runningMode || "",
      state.completedSlices.join(","),
      state.activeSlice || "",
      state.lastUsername || "",
      state.lastScorePts || 0,
      hasCustomStatus ? state.statusMessage : "",
      hasActiveConfirmBanner ? `${state.lastConfirmTone}:${state.lastConfirmBanner}` : "",
      state.targetPickerOpen ? 1 : 0,
      pageHandle,
      state.knownTargetAccounts.length,
      ipKey
    ].join("|");

    if (!force && state._lastDockRenderKey === renderKey && dock.innerHTML) {
      return;
    }
    state._lastDockRenderKey = renderKey;

    if (isInspectingFollower) {
      inspectedBannerHtml =
        buildInspectedProfileCardHtml(ip, {
          isRo,
          artist: state.artist,
          alreadySaved: inspectedAlreadySaved,
          activeLabel: inspectedActiveLabel,
          isMarkingClosing: state.isMarkingClosing,
          labelStatusText: state.labelStatusText,
          labelStatusColor: state.labelStatusColor
        }) +
        `<div class="botaudit-section-divider"><span>${
          isRo ? "PROGRES ȚINTĂ" : "TARGET PROGRESS"
        } · @${escapeHtml(state.artist)}</span><span>${checkedCount} ${
          isRo ? "verificate" : "checked"
        }</span></div>`;
    }

    const targetPickerHtml = state.targetPickerOpen
      ? (() => {
          const accountsList = getMergedKnownAccounts();
          const currentPageBtnHtml = showSwitchToPage
            ? `<button type="button" class="botaudit-target-current-btn" data-target-artist="${escapeHtml(pageHandle)}">🎯 ${
                isRo ? `Auditează pagina curentă: @${escapeHtml(pageHandle)}` : `Audit current page: @${escapeHtml(pageHandle)}`
              }</button>`
            : "";
          const itemsHtml = accountsList
            .map((acc) => {
              const isCur = state.artist && acc.artist.toLowerCase() === state.artist.toLowerCase();
              const statsTxt = acc.checked > 0 ? `${acc.checked}/${acc.saved}` : acc.saved > 0 ? `${acc.saved}` : isRo ? "nou" : "new";
              return `<button type="button" class="botaudit-target-item${isCur ? " is-active" : ""}" data-target-artist="${escapeHtml(acc.artist)}"><span>${isCur ? "✓ " : ""}@${escapeHtml(acc.artist)}</span><span class="botaudit-target-meta">${statsTxt}</span></button>`;
            })
            .join("");
          return `<div id="botaudit-target-picker" class="botaudit-target-picker">
            ${currentPageBtnHtml}
            <div class="botaudit-target-input-row">
              <input id="botaudit-target-input" type="text" placeholder="${isRo ? "@cont_nou..." : "@username..."}" value="">
              <button id="botaudit-target-go-btn" type="button">${isRo ? "Setează" : "Switch"}</button>
            </div>
            <div class="botaudit-target-list">${itemsHtml}</div>
          </div>`;
        })()
      : showSwitchToPage
        ? `<div class="botaudit-switch-page-bar">
            <span>${isRo ? `Ești pe @${escapeHtml(pageHandle)}` : `Viewing @${escapeHtml(pageHandle)}`}</span>
            <button id="botaudit-switch-page-btn" type="button" data-target-artist="${escapeHtml(pageHandle)}">🎯 ${
              isRo ? `Auditează @${escapeHtml(pageHandle)}` : `Audit @${escapeHtml(pageHandle)}`
            }</button>
          </div>`
        : "";

    dock.innerHTML = `
      <div class="botaudit-dock-header" title="${isRo ? "Trage pentru mutare · Dublu-click pt. reset" : "Drag to move · Double-click to reset position"}">
        <span class="botaudit-dock-title">
          <span class="botaudit-brand-dot"></span>
          <span>Bot Microscope</span>
          <span class="botaudit-ver-pill" title="${isRo ? "Versiune Extensie (Manifest)" : "Extension Build Version"}">v${EXT_VERSION}</span>
          <button id="botaudit-artist-btn" type="button" class="botaudit-artist-chip is-clickable" title="${isRo ? "Schimbă contul țintă de auditat" : "Switch target account to audit"}">@${escapeHtml(state.artist)} ▾</button>
        </span>
        <div class="botaudit-hdr-btns">
          <button id="botaudit-lang-btn" class="botaudit-icon-btn">
            ${isRo ? "RO·EN" : "EN·RO"}
          </button>
          <button id="botaudit-min-btn" class="botaudit-icon-btn" title="${isRo ? "Minimizează" : "Minimize"}">—</button>
        </div>
      </div>

      ${targetPickerHtml}

      ${inspectedBannerHtml}

      <!-- COMPACT TELEMETRY & QUOTA BAR -->
      <div class="botaudit-speedometer ${speedClass}">
        <div class="botaudit-speed-top">
          <span>${speedHeadline}</span>
          <span class="botaudit-speed-quotas">1h <b>${rl.checksLastHour}</b>/180 · 24h <b>${rl.checksLastDay}</b>/600</span>
        </div>
        <div class="botaudit-dual-meters">
          <div class="botaudit-meter-track" title="${isRo ? "Limită Orară (180/oră)" : "Hourly Quota (180/hr)"}">
            <div class="botaudit-meter-fill" style="width:${hrPct}%;background:${hrColor};"></div>
          </div>
          <div class="botaudit-meter-track" title="${isRo ? "Limită Zilnică (600/zi)" : "Daily Quota (600/day)"}">
            <div class="botaudit-meter-fill" style="width:${dayPct}%;background:${dayColor};"></div>
          </div>
        </div>
      </div>

      ${waveStripHtml}
      ${confirmToastHtml}

      <!-- 2-COLUMN SYMMETRIC VERDICT KPI STRIP -->
      <div class="botaudit-verdict-kpis">
        <div class="botaudit-kpi-verdict botaudit-kpi-bot" title="${botsCount} auto (>60p) · ${confirmedBotsCount} ${isRo ? "confirmați manual" : "manually confirmed"}${overriddenHumansCount > 0 ? ` · ${overriddenHumansCount} ${isRo ? "trecuți manual la Om Real" : "overridden to Human"}` : ""}${unscoredConfirmedBots > 0 ? ` (+${unscoredConfirmedBots} ${isRo ? "fără cifre" : "unscored"})` : ""}">
          <span class="botaudit-stat-val" style="color:#fda4af;">🤖 ${scoredBotsCount} <span class="botaudit-kpi-pct">(${botPct}%)</span></span>
          <span class="botaudit-stat-lbl">${isRo ? `Boți / ${botsDenom}` : `Bots / ${botsDenom}`}</span>
          <span class="botaudit-kpi-sub" data-kpi-sub="bots">✓ ${decidedConfirmedBots} ${isRo ? "confirmați" : "confirmed"} · ${botsCount} auto</span>
        </div>
        <div class="botaudit-kpi-verdict botaudit-kpi-human" title="${autoHumansCount} auto (<35p) · ${confirmedHumansCount} ${isRo ? "confirmați manual" : "manually confirmed"}${overriddenBotsCount > 0 ? ` · ${overriddenBotsCount} ${isRo ? "trecuți manual la Bot" : "overridden to Bot"}` : ""}${unscoredConfirmedHumans > 0 ? ` (+${unscoredConfirmedHumans} ${isRo ? "fără cifre" : "unscored"})` : ""}">
          <span class="botaudit-stat-val" style="color:#6ee7b7;">👤 ${scoredHumansCount} <span class="botaudit-kpi-pct">(${humanPct}%)</span></span>
          <span class="botaudit-stat-lbl">${isRo ? `Oameni / ${botsDenom}` : `Humans / ${botsDenom}`}</span>
          <span class="botaudit-kpi-sub" data-kpi-sub="humans">✓ ${decidedConfirmedHumans} ${isRo ? "confirmați" : "confirmed"} · ${autoHumansCount} auto</span>
        </div>
      </div>
      ${
        suspiciousCount > 0
          ? `<div class="botaudit-suspicious-bar" title="${
              isRo
                ? "Profiluri complete 35–60p: necesită verificare umană (Om Real vs Bot Gen-2)"
                : "Complete profiles scoring 35–60p: need human check (Real Person vs Gen-2 Bot)"
            }"><span>🟡 ${isRo ? "Suspecte 35–60p (verifică)" : "Suspicious 35–60p (check)"}</span><b>${suspiciousCount} <span class="botaudit-kpi-pct">(${suspiciousPct}%)</span></b></div>`
          : ""
      }

      <!-- 3-COLUMN SECONDARY KPI STRIP -->
      <div class="botaudit-dock-stats is-three-col">
        <div class="botaudit-stat-box">
          <span class="botaudit-stat-val">${totalCount} <span style="font-size:9.5px;color:#6ee7b7;">+${state.newThisSession}</span></span>
          <span class="botaudit-stat-lbl">${isRo ? "Salvate" : "Saved"}</span>
        </div>
        <div class="botaudit-stat-box">
          <span class="botaudit-stat-val" style="color:#fcd34d;">${burstCount}</span>
          <span class="botaudit-stat-lbl">${isRo ? "1-6/Zi" : "1-6 Burst"}</span>
        </div>
        <div class="botaudit-stat-box">
          <span class="botaudit-stat-val" style="color:#c4b5fd;">${privBotsCount}</span>
          <span class="botaudit-stat-lbl">${isRo ? "🔒 Privați" : "🔒 Priv"}</span>
        </div>
      </div>

      <!-- UNIFIED SAMPLE PROGRESS & CLUES -->
      <div class="botaudit-progress-card">
        <div class="botaudit-progress-head">
          <span>${isRo ? `Eșantion ±5%: ${checkedCount}/${sampleTarget} (${samplePct}%)` : `Sample ±5%: ${checkedCount}/${sampleTarget} (${samplePct}%)`}</span>
          <span style="color:#94a3b8;font-weight:600;">${isRo ? `Azi +${state.newThisSession}` : `Today +${state.newThisSession}`}</span>
        </div>
        <div class="botaudit-meter-track" style="height:4.5px;">
          <div class="botaudit-meter-fill" style="width:${Math.max(sessionPct, samplePct)}%;background:linear-gradient(90deg,#60a5fa,#34d399);"></div>
        </div>
        <div class="botaudit-clue-pills">
          <span class="botaudit-clue-pill">🔢 ${isRo ? "Cifre" : "Digits"}: <b>${digitCount}</b></span>
          <span class="botaudit-clue-pill">👥 Mass-Flw: <b>${massFollowCount}</b></span>
          <span class="botaudit-clue-pill">🖼️ ${isRo ? "Fără Poză" : "No Pic"}: <b>${noPhotoCount}</b></span>
        </div>
      </div>

      <!-- COLLAPSIBLE COLLECTOR TOOLS DRAWER -->
      <button id="botaudit-tools-toggle" type="button" class="botaudit-tools-toggle">
        <span>${showToolsBody ? "▾" : "▸"} ⚙️ ${isRo ? "Unelte & Auto-Scroll" : "Collector Tools"}${automationOn ? " · AUTO" : ""}</span>
        <span>${
          unscoredCount > 0
            ? isRo
              ? `${unscoredCount} doar-nume`
              : `${unscoredCount} handle-only`
            : isRo
              ? "Complet"
              : "All read"
        }</span>
      </button>

      ${
        showToolsBody
          ? `<div class="botaudit-tools-body">
              <label style="display:flex;align-items:center;gap:6px;font-size:10px;color:#cbd5e1;cursor:pointer;margin-bottom:6px;">
                <input type="checkbox" id="botaudit-auto-toggle" ${automationOn ? "checked" : ""}>
                <span>${isRo ? "Auto-scroll experimental (oprit implicit)" : "Experimental auto-scroll (off by default)"}</span>
              </label>
              ${
                automationOn
                  ? `<div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:6px;">
                      <button id="botaudit-fast-btn" class="botaudit-btn" style="padding:4px 7px;font-size:10px;">
                        ⚡ ${state.runningMode === "fast_funnel" ? (isRo ? "Pauză" : "Pause") : "+50"}
                      </button>
                      <button id="botaudit-deep-btn" class="botaudit-btn" style="padding:4px 7px;font-size:10px;">
                        🔬 ${state.runningMode === "deep_research" ? (isRo ? "Pauză" : "Pause") : "+200"}
                      </button>
                      <button id="botaudit-slice-btn" class="botaudit-btn" style="flex:1 1 100%;padding:4px 7px;font-size:10px;color:#93c5fd;">
                        🔤 ${isRo ? "Salt la Următoarea Literă Search" : "Jump Next Search Letter"} (${state.completedSlices.length}/36)
                      </button>
                    </div>
                    <div class="botaudit-slice-bar">${sliceDotsHtml}</div>`
                  : `<div style="font-size:9.5px;color:#94a3b8;line-height:1.4;">
                      ${
                        isRo
                          ? "Mod manual activ: derulează lista Followers și deschide profilurile în file noi pentru verificare completă."
                          : "Manual mode: scroll the Followers list and open profiles in new tabs to read full numbers."
                      }
                      ${
                        state.lastUsername
                          ? `<div style="margin-top:3px;color:#93c5fd;">🎯 ${isRo ? "Ultimul" : "Last"}: <b>@${escapeHtml(state.lastUsername)}</b> (${state.lastScorePts}p)</div>`
                          : ""
                      }
                    </div>`
              }
            </div>`
          : ""
      }

      ${hasCustomStatus ? `<div class="botaudit-status-line" title="${escapeHtml(state.statusMessage)}">${escapeHtml(state.statusMessage)}</div>` : ""}

      <div class="botaudit-dock-actions">
        <button id="botaudit-send-btn" class="botaudit-btn botaudit-btn-primary">
          🚀 ${isRo ? "Trimite în Audit App" : "Send to Audit App"}
        </button>
        <button id="botaudit-csv-btn" class="botaudit-btn botaudit-btn-csv">
          📥 CSV
        </button>
      </div>
    `;

    // CSP-safe fallback if Instagram CDN blocks or expires an avatar or gallery thumbnail
    const avatarImgEl = dock.querySelector(".botaudit-avatar-img");
    if (avatarImgEl) {
      avatarImgEl.addEventListener("error", () => {
        const ring = avatarImgEl.closest(".botaudit-avatar-ring");
        const initChar = (ring && ring.getAttribute("data-initial")) || "?";
        if (ring) {
          ring.innerHTML = `<span class="botaudit-avatar-fallback">${escapeHtml(initChar)}</span>`;
        }
      });
    }
    dock.querySelectorAll(".botaudit-strip-thumb").forEach((thumbEl) => {
      thumbEl.addEventListener("error", () => {
        const frame = thumbEl.closest(".botaudit-photo-frame");
        if (frame) frame.remove();
      });
    });
  }

  // Ergonomic single-key verdict shortcuts (B = Bot, H = Human, U = Unsure) on open follower tabs
  window.addEventListener("keydown", (e) => {
    if (!e || e.defaultPrevented || e.repeat) return;
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    if (state.minimized || state.isMarkingClosing) return;
    if (isTypingInEditableElement(e.target) || isTypingInEditableElement(document.activeElement)) {
      return;
    }
    if (document.querySelector('div[role="dialog"]')) return;

    const ip = state.inspectedPageProfile;
    if (!ip || !ip.username || ip.username === state.artist) return;

    const key = String(e.key || "").toLowerCase();
    if (key === "b") {
      e.preventDefault();
      postLabelAndCloseTab(ip.username, "bot");
    } else if (key === "h") {
      e.preventDefault();
      postLabelAndCloseTab(ip.username, "human");
    } else if (key === "u") {
      e.preventDefault();
      postLabelAndCloseTab(ip.username, "unsure");
    }
  });

  // Live cross-tab sync: when an inspected follower tab marks BOT/HUMAN/? and closes,
  // or another tab confirms inline / collects stats, update badges and counters immediately!
  window.addEventListener("storage", (evt) => {
    if (!evt || !evt.key) return;
    const artistKey = getArtistStorageKey(state.artist);
    if (evt.key === LAST_EVENT_KEY && evt.newValue) {
      try {
        const msg = JSON.parse(evt.newValue);
        applyIncomingLiveDelta(msg);
      } catch (_e) {}
    } else if (evt.key === LABELS_STORAGE_KEY && evt.newValue) {
      try {
        const nowMs = Date.now();
        const obj = JSON.parse(evt.newValue);
        for (const [u, val] of Object.entries(obj || {})) {
          if (!u || !val) continue;
          const uLow = u.toLowerCase();
          const prev = state.manualLabelsMap.get(uLow);
          if (prev && prev.localTs && nowMs - prev.localTs < 20000 && prev.label !== val.label) {
            continue;
          }
          state.manualLabelsMap.set(uLow, val);
          const existingKey = findProfileKeyInMap(uLow);
          if (existingKey && val.label) {
            state.profilesMap.get(existingKey).manual_label = val.label;
          }
          if (
            state.inspectedPageProfile &&
            (state.inspectedPageProfile.username || "").toLowerCase() === uLow &&
            val.label
          ) {
            state.inspectedPageProfile.manual_label = val.label;
          }
        }
        decorateFollowerRowsInDom();
        updateFloatingDock();
      } catch (_e) {}
    } else if (evt.key === artistKey && evt.newValue) {
      try {
        const parsed = JSON.parse(evt.newValue);
        if (parsed && Array.isArray(parsed.profiles)) {
          for (const rawP of parsed.profiles) {
            const up = upgradeAndRescoreProfile(rawP);
            if (up) {
              up.artist = state.artist;
              upsertProfileInMap(up);
            }
          }
          decorateFollowerRowsInDom();
          updateFloatingDock();
        }
      } catch (_e) {}
    } else if (evt.key === ACTIVE_ARTIST_KEY && evt.newValue) {
      const nextTarget = normalizeTargetHandle(evt.newValue);
      if (nextTarget && nextTarget.toLowerCase() !== String(state.artist || "").toLowerCase()) {
        const pageHandle = getPagePathHandle();
        const dialog = document.querySelector('div[role="dialog"]');
        const onOtherFollowersModal = Boolean(
          dialog &&
            isFollowersModal(dialog) &&
            pageHandle &&
            pageHandle.toLowerCase() !== nextTarget.toLowerCase()
        );
        const isChildTabOfOther = Boolean(
          pageHandle &&
            (() => {
              const parent = findRecentClickArtist(pageHandle);
              return parent && parent.toLowerCase() !== nextTarget.toLowerCase();
            })()
        );
        if (!onOtherFollowersModal && !isChildTabOfOther) {
          setActiveTargetArtist(nextTarget, { fromRemote: true });
        }
      }
    }
  });

  // Message handler for toolbar popup.js and background service worker live delta broadcasts
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
      if (!msg || !msg.action) return false;
      if (msg.action === "BOTAUDIT_LIVE_DELTA") {
        applyIncomingLiveDelta(msg.payload || {});
        return false;
      }
      if (msg.action === "BOTAUDIT_TABS_COUNT") {
        if (msg.payload && typeof msg.payload.openFollowerTabs === "number") {
          state.openFollowerTabs = msg.payload.openFollowerTabs;
          updateFloatingDock();
        }
        return false;
      }
      if (msg.action === "getStatus") {
        scanAndCollectDomFollowers();
        const disp = computeDisplayCounts();
        const inspectedClean = state.inspectedPageProfile
          ? stripTransientUiFields(state.inspectedPageProfile)
          : null;
        if (inspectedClean && inspectedClean.username) {
          const inspLbl = resolveManualLabel(inspectedClean);
          if (inspLbl) inspectedClean.manual_label = inspLbl;
        }
        sendResponse({
          version: EXT_VERSION,
          artist: state.artist,
          pageHandle: getPagePathHandle(),
          knownAccounts: getMergedKnownAccounts(),
          lang: state.lang,
          count: disp.totalCount,
          newThisSession: state.newThisSession,
          lastUsername: state.lastUsername,
          completedSlices: state.completedSlices,
          botsCount: disp.scoredBotsCount,
          suspiciousCount: disp.suspiciousCount,
          humansCount: disp.scoredHumansCount,
          confirmedBotsCount: disp.decidedConfirmedBots,
          autoBotsCount: disp.botsCount,
          confirmedHumansCount: disp.decidedConfirmedHumans,
          autoHumansCount: disp.autoHumansCount,
          botPct: disp.botPct,
          suspiciousPct: disp.suspiciousPct,
          humanPct: disp.humanPct,
          burstCount: disp.burstCount,
          privBotsCount: disp.privBotsCount,
          checkedCount: disp.checkedCount,
          openFollowerTabs: state.openFollowerTabs,
          inspectedProfile: inspectedClean,
          automationEnabled: isAutomationEnabled(),
          runningMode: state.runningMode,
          statusMessage: state.statusMessage,
          rateLimit: getRateLimitStatus()
        });
        return true;
      }
      if (msg.action === "setTargetArtist") {
        const ok = setActiveTargetArtist(msg.artist);
        sendResponse({ ok, artist: state.artist });
        return true;
      }
      if (msg.action === "startFast") {
        runCollectionLoop("fast_funnel");
        sendResponse({ ok: isAutomationEnabled() });
        return true;
      }
      if (msg.action === "startDeep") {
        runCollectionLoop("deep_research");
        sendResponse({ ok: isAutomationEnabled() });
        return true;
      }
      if (msg.action === "nextSlice") {
        jumpToNextSearchSlice().then((ok) => sendResponse({ ok }));
        return true;
      }
      if (msg.action === "sendToApp") {
        sendToLocalAuditApp().then((res) => sendResponse(res));
        return true;
      }
      if (msg.action === "downloadCsv") {
        downloadCollectedCsv();
        sendResponse({ ok: true });
        return true;
      }
      if (msg.action === "clearData") {
        state.profilesMap.clear();
        state.dirtyUsernames.clear();
        state.completedSlices = [];
        state.lastUsername = "";
        state.newThisSession = 0;
        state.serverLiveCounts = null;
        state.batchConfirmedCount = 0;
        savePersistedState(false);
        sendBackgroundMessage("SAVE_CHECKPOINT", { artist: state.artist, clear: true }).catch(() => {
          fetch("http://127.0.0.1:8000/api/checkpoint", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ artist: state.artist, clear: true })
          }).catch(() => {});
        });
        updateFloatingDock();
        sendResponse({ ok: true });
        return true;
      }
      return false;
    });
  }

  window.__BOT_AUDIT_UI_HELPERS__ = {
    fmtStat,
    escapeHtml,
    formatHandleWithDigits,
    stripTransientUiFields,
    isTypingInEditableElement,
    formatFollowRatioPill,
    buildInspectedFlagChips,
    buildInspectedProfileCardHtml,
    classifyProfileVerdict,
    computeDisplayCounts,
    buildCompactLocalProfile,
    state,
    updateFloatingDock,
    postLabelAndCloseTab,
    applyIncomingLiveDelta,
    recordModalFollowerClick,
    wasRecentlyClickedFromActiveFollowersModal,
    confirmFollowerInline,
    decorateFollowerRowsInDom,
    setActiveTargetArtist,
    detectCurrentArtist,
    getMergedKnownAccounts
  };

  window.addEventListener("pointerdown", recordModalFollowerClick, true);
  window.addEventListener("auxclick", recordModalFollowerClick, true);

  let _lastFocusMemorySync = 0;
  let _lastFocusCheckpointSync = 0;
  function refreshMemoryBankOnFocus() {
    if (typeof document !== "undefined" && document.hidden) return;
    const now = Date.now();
    if (now - _lastFocusMemorySync >= 2500) {
      _lastFocusMemorySync = now;
      syncMemoryBankFromServer();
    }
    if (now - _lastFocusCheckpointSync >= 2500) {
      _lastFocusCheckpointSync = now;
      syncCheckpointFromServer();
    }
    sendBackgroundMessage("GET_OPEN_TABS_COUNT", { artist: state.artist })
      .then((resp) => {
        if (resp && typeof resp.openFollowerTabs === "number") {
          state.openFollowerTabs = resp.openFollowerTabs;
          updateFloatingDock();
        }
      })
      .catch(() => {});
  }
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("focus", refreshMemoryBankOnFocus);
  }
  if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
    document.addEventListener("visibilitychange", refreshMemoryBankOnFocus);
  }

  loadPersistedState();
  injectMainWorldListener();
  createFloatingDock();
  if (typeof process === "undefined") {
    console.info(`%c[Bot Microscope]%c Extension v${EXT_VERSION} active — safe passive collector ready`, "color:#38bdf8;font-weight:bold;", "color:#94a3b8;");
  }
  setInterval(scanAndCollectDomFollowers, 2500);
})();
