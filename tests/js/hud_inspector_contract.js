// Contract harness for HUD Forensic Bento Inspector, Ratio Pill, Score Gauge & Shortcut Safety.
// Executed by pytest via tests/test_extension_hud_ux.py.
"use strict";

const storageStore = new Map();
const windowListeners = {};

const dummyEl = {
  id: "",
  className: "",
  innerHTML: "",
  textContent: "",
  style: {},
  dataset: {},
  classList: {
    add() {},
    remove() {},
    contains() {
      return false;
    }
  },
  addEventListener() {},
  querySelector() {
    return { onclick: null, onchange: null, addEventListener() {} };
  },
  querySelectorAll() {
    return [];
  },
  getAttribute() {
    return "";
  },
  closest() {
    return null;
  }
};

globalThis.localStorage = {
  getItem(k) {
    return storageStore.has(k) ? storageStore.get(k) : null;
  },
  setItem(k, v) {
    storageStore.set(k, String(v));
  },
  removeItem(k) {
    storageStore.delete(k);
  }
};

globalThis.document = {
  body: {
    innerText: "",
    appendChild() {}
  },
  head: {
    appendChild() {}
  },
  documentElement: {
    appendChild() {}
  },
  activeElement: { tagName: "BODY" },
  createElement() {
    return Object.assign({}, dummyEl, { style: {} });
  },
  getElementById() {
    return Object.assign({}, dummyEl, { style: {} });
  },
  querySelector() {
    return null;
  }
};

globalThis.window = globalThis;
globalThis.location = { pathname: "/kamchor_shyr_143/" };
globalThis.addEventListener = (type, fn) => {
  windowListeners[type] = windowListeners[type] || [];
  windowListeners[type].push(fn);
};
globalThis.setInterval = () => 1;
globalThis.fetch = async () => ({ ok: false, status: 503, async json() { return {}; } });
globalThis.chrome = {
  runtime: {
    lastError: null,
    getURL(p) {
      return `chrome-extension://test/${p}`;
    },
    sendMessage(_msg, cb) {
      if (cb) cb({ ok: false });
    },
    onMessage: {
      addListener() {}
    }
  }
};

require("../../browser-extension/parser.js");
require("../../browser-extension/content.js");

const parser = globalThis.BotAuditParser;
const ui = globalThis.__BOT_AUDIT_UI_HELPERS__;

function buildScoredProfile(raw) {
  const hf = parser.analyzeHandleForensics(raw.username);
  const p = Object.assign(
    {
      schema_version: parser.SCHEMA_VERSION,
      artist: "target_alpha",
      username_digit_ratio: hf.digit_ratio,
      post_timestamps: []
    },
    raw
  );
  const scored = parser.scoreFollowerProfile(p);
  p.same_day_post_burst = scored.flags.same_day_post_burst;
  p.bot_score = scored.botScore;
  p.score_pts = scored.scorePts;
  p.bot_like = scored.botLike;
  p.score_complete = scored.complete;
  p.flags = scored.flags;
  return p;
}

// 1. Exact screenshot profile: @kamchor_shyr_143 (1 post, 2 followers, 4,766 following, Private, Bio)
const kamchor = buildScoredProfile({
  username: "kamchor_shyr_143",
  has_profile_photo: true,
  account_private: true,
  post_count: 1,
  followers_count: 2,
  following_count: 4766,
  bio_length: 51,
  avatar_url: "https://cdn.example/pic.jpg",
  bio_preview: "heart touching shyr · true love · support me guys"
});
const kamchorHtml = ui.buildInspectedProfileCardHtml(kamchor, {
  isRo: false,
  artist: "target_alpha",
  alreadySaved: true
});

// 2. Clean human profile: @reea.404 (49 posts, 1,515 followers, 513 following)
const reea = buildScoredProfile({
  username: "reea.404",
  has_profile_photo: true,
  account_private: true,
  post_count: 49,
  followers_count: 1515,
  following_count: 513,
  bio_length: 84
});
const reeaHtml = ui.buildInspectedProfileCardHtml(reea, {
  isRo: false,
  artist: "target_alpha",
  alreadySaved: true
});

// 3. Partial / unobserved profile: null counts must stay "—" and omit ratio pill
const partial = buildScoredProfile({
  username: "zebra.9825848",
  has_profile_photo: false,
  account_private: null,
  post_count: null,
  followers_count: null,
  following_count: null,
  bio_length: null
});
const partialHtml = ui.buildInspectedProfileCardHtml(partial, {
  isRo: false,
  artist: "target_alpha",
  alreadySaved: false
});

// 4. Transient UI fields stripped cleanly before persistence
const stripped = ui.stripTransientUiFields(kamchor);

// 5. Manually confirmed dressed-up bot renders CONFIRMED BOT pill even under 60p checklist
const confirmedDressedBotHtml = ui.buildInspectedProfileCardHtml(
  buildScoredProfile({
    username: "dressed_bot_demo",
    has_profile_photo: true,
    account_private: false,
    post_count: 12,
    followers_count: 500,
    following_count: 450,
    bio_length: 40
  }),
  {
    isRo: false,
    artist: "target_alpha",
    alreadySaved: true,
    activeLabel: "bot"
  }
);

// 6. Keyboard shortcut editable element guard
const guardChecks = {
  bodySafe: ui.isTypingInEditableElement({ tagName: "BODY" }) === false,
  divSafe: ui.isTypingInEditableElement({ tagName: "DIV" }) === false,
  inputBlocked: ui.isTypingInEditableElement({ tagName: "INPUT" }) === true,
  textareaBlocked: ui.isTypingInEditableElement({ tagName: "TEXTAREA" }) === true,
  contentEditableBlocked:
    ui.isTypingInEditableElement({ tagName: "DIV", isContentEditable: true }) === true,
  roleTextboxBlocked:
    ui.isTypingInEditableElement({
      tagName: "SPAN",
      closest(sel) {
        return sel.includes('[role="textbox"]') ? {} : null;
      }
    }) === true
};

console.log(
  JSON.stringify({
    kamchorScore: kamchor.score_pts,
    kamchorHtml,
    reeaScore: reea.score_pts,
    reeaHtml,
    partialHtml,
    confirmedDressedBotHtml,
    strippedHasAvatar: Object.prototype.hasOwnProperty.call(stripped, "avatar_url"),
    strippedHasBioPreview: Object.prototype.hasOwnProperty.call(stripped, "bio_preview"),
    strippedFollowing: stripped.following_count,
    guardChecks
  })
);

