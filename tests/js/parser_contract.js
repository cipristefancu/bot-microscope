// Contract checks for browser-extension/parser.js. Prints one JSON object; pytest asserts on it
// (tests/test_extension_parser.py). Run by hand:  node tests/js/parser_contract.js
"use strict";
require("../../browser-extension/parser.js");
const P = globalThis.BotAuditParser;

const out = {};

// A followers-list row carries no numbers: they must stay unknown (null), never guessed.
const listStore = P.extractProfilesFromInstagramJson(
  { users: [{ username: "quiet.fan", is_private: false, profile_pic_url: "https://cdn.example/photo_n.jpg" }] },
  "artist_x"
);
out.list_row = listStore.get("quiet.fan");

// A row about which nothing is known except the handle.
out.bare_row = P.extractProfilesFromInstagramJson({ username: "bare_handle" }, "artist_x").get("bare_handle");

// A full profile payload: every number observed.
const fullStore = P.extractProfilesFromInstagramJson(
  {
    data: {
      user: {
        username: "zebra.9605848",
        is_private: false,
        has_anonymous_profile_picture: false,
        biography: "",
        edge_followed_by: { count: 18 },
        edge_follow: { count: 1950 },
        edge_owner_to_timeline_media: { count: 4, edges: [] }
      }
    }
  },
  "artist_x"
);
out.full_row = JSON.parse(JSON.stringify(fullStore.get("zebra.9605848")));

// A later bare sighting of the same account must not erase the observed numbers.
P.extractProfilesFromInstagramJson({ username: "zebra.9605848" }, "artist_x", fullStore);
out.merged_row = fullStore.get("zebra.9605848");

// Scoring is tri-state.
out.score_nothing_known = P.scoreFollowerProfile({ username: "plain.name" });
out.score_handle_and_photo_only = P.scoreFollowerProfile({ username: "zebra.9605848", has_profile_photo: false });
out.score_partial_but_conclusive = P.scoreFollowerProfile({
  username: "zebra.9605848",
  has_profile_photo: false,
  post_count: 0
});
out.score_complete_human = P.scoreFollowerProfile({
  username: "plain.name",
  has_profile_photo: true,
  account_private: false,
  post_count: 42,
  followers_count: 680,
  following_count: 410,
  bio_length: 58
});

// Numbers invented by older builds are turned back into "unknown"; observed ones are kept.
const base = {
  has_profile_photo: true,
  account_private: false,
  post_count: 24,
  followers_count: 340,
  following_count: 290,
  bio_length: 42,
  bot_like: false,
  flags: {}
};
out.scrub_placeholder = P.scrubLegacyPlaceholders(Object.assign({ username: "p1", deep_checked: false }, base));
out.scrub_keeps_deep_checked = P.scrubLegacyPlaceholders(Object.assign({ username: "p2", deep_checked: true }, base));
out.scrub_keeps_current_schema = P.scrubLegacyPlaceholders(
  Object.assign({ username: "p3", deep_checked: false, schema_version: 3 }, base)
);
out.scrub_keeps_real_numbers = P.scrubLegacyPlaceholders({
  username: "p4",
  has_profile_photo: true,
  account_private: false,
  post_count: 5,
  followers_count: 10,
  following_count: 20,
  bio_length: 1,
  deep_checked: false
});

// The CSV export contains only rows whose numbers were all observed.
out.csv = P.profilesToCsv([listStore.get("quiet.fan"), fullStore.get("zebra.9605848")], "artist_x");

out.counts = [
  P.parseShorthandCount("1.2K"),
  P.parseShorthandCount("1,2 mil."),
  P.parseShorthandCount("4,321"),
  P.parseShorthandCount("nothing"),
  P.parseShorthandCount("")
];
out.header_stats_partial = P.parseProfileHeaderStats("12 posts 340 followers");
out.burst_unknown = P.detectSameDayPostBurst(4, [], null, null, null, "x");
out.burst_from_timestamps = P.detectSameDayPostBurst(3, [1715000000, 1715000300, 1715000600], null, null, null, "x");

// Enhancement A: ratio-based mass following in parser (200 following / 25 followers = 8.0x)
out.score_enhancement_a_ratio = P.scoreFollowerProfile({
  username: "plain.name",
  has_profile_photo: true,
  account_private: false,
  post_count: 4,
  followers_count: 25,
  following_count: 200,
  bio_length: 20
});

// Enhancement B: local registry match in parser (+25 pts boost pushes 45 pt cjrave74317 to 70 pts)
out.score_enhancement_b_registry = P.scoreFollowerProfile(
  {
    username: "cjrave74317",
    has_profile_photo: true,
    account_private: false,
    post_count: 0,
    followers_count: 7,
    following_count: 13,
    bio_length: 20
  },
  {},
  new Set(["cjrave74317"])
);

// 7K+/7K+ low-post follow-exchange ring bot (@kiso8157: 9 posts, 7192 followers, 7351 following -> 65p)
out.score_follow_exchange_kiso = P.scoreFollowerProfile({
  username: "kiso8157",
  has_profile_photo: true,
  account_private: false,
  post_count: 9,
  followers_count: 7192,
  following_count: 7351,
  bio_length: 58
});

// Active real human creator with high follow/follower counts (1059 posts, 7753 followers, 9098 following -> 0p)
out.score_follow_exchange_real_creator = P.scoreFollowerProfile({
  username: "creator_alpha",
  has_profile_photo: true,
  account_private: false,
  post_count: 1059,
  followers_count: 7753,
  following_count: 9098,
  bio_length: 371
});

// Extreme mass-following bot with 12 setup posts (ratio 20x + empty bio + 12 posts -> >60p)
out.score_extreme_mass_12_posts = P.scoreFollowerProfile({
  username: "andrei_m99",
  has_profile_photo: true,
  account_private: false,
  post_count: 12,
  followers_count: 50,
  following_count: 1000,
  bio_length: 0
});

// Modern GraphQL timeline with shared/coauthored reposts and dead likes
const sharedFeedStore = P.extractProfilesFromInstagramJson(
  {
    data: {
      user: {
        username: "repost_engaged_bot",
        is_private: false,
        has_anonymous_profile_picture: false,
        biography: "music",
        edge_followed_by: { count: 900 },
        edge_follow: { count: 3800 },
        xdt_api__v1__feed__user_timeline_graphql_connection: {
          count: 18,
          edges: [
            { node: { taken_at: 1715000000, like_count: 1, coauthor_producers: [{ username: "viralpage1" }] } },
            { node: { taken_at: 1715100000, like_count: 2, is_repost: true } },
            { node: { taken_at: 1715200000, like_count: 1, owner: { username: "other_brand" } } },
            { node: { taken_at: 1715300000, like_count: 2, coauthor_producers: [{ username: "viralpage2" }] } }
          ]
        }
      }
    }
  },
  "artist_x"
);
out.shared_post_feed_row = JSON.parse(JSON.stringify(sharedFeedStore.get("repost_engaged_bot")));

// Strict follower-only extraction and URL / modal classification guards
out.url_classifications = {
  followers: P.classifyInstagramInterceptUrl("https://www.instagram.com/api/v1/friendships/12345/followers/?count=12"),
  following: P.classifyInstagramInterceptUrl("https://www.instagram.com/api/v1/friendships/12345/following/?count=12"),
  graphql: P.classifyInstagramInterceptUrl("https://www.instagram.com/graphql/query"),
  web_profile: P.classifyInstagramInterceptUrl("https://www.instagram.com/api/v1/users/web_profile_info/?username=fan1"),
  other: P.classifyInstagramInterceptUrl("https://www.instagram.com/api/v1/feed/reels_tray/")
};

out.modal_text_checks = {
  en_followers: P.isFollowersModalDialogText("Followers"),
  ro_followers: P.isFollowersModalDialogText("Urmăritori"),
  followers_with_search: P.isFollowersModalDialogText("Followers\nSearch\nfan_one"),
  en_following: P.isFollowersModalDialogText("Following"),
  ro_following: P.isFollowersModalDialogText("Urmăriri"),
  likes: P.isFollowersModalDialogText("Likes"),
  comments: P.isFollowersModalDialogText("Comments")
};

const strictStore = new Map();
P.extractProfilesFromInstagramJson(
  {
    data: {
      user: {
        username: "artist_x",
        follower_count: 7998
      },
      viewer: {
        username: "known_fan",
        is_private: false,
        has_anonymous_profile_picture: false,
        media_count: 14,
        follower_count: 220,
        following_count: 190,
        biography: "music lover",
        xdt_api__v1__feed__user_timeline_graphql_connection: {
          count: 14,
          edges: [
            { node: { taken_at: 1715000000, like_count: 12, coauthor_producers: [{ username: "viralpage1" }] } }
          ]
        }
      },
      chaining_results: [{ username: "leomessi", follower_count: 500000000 }],
      suggested_users: { users: [{ username: "cristiano", follower_count: 600000000 }] }
    }
  },
  "artist_x",
  strictStore,
  {
    allowNew: false,
    allowedUsernames: new Set(["known_fan"]),
    strictFollowerOnly: true,
    excludeArtist: true
  }
);
out.strict_extract_keys = Array.from(strictStore.keys());
out.strict_known_fan = strictStore.has("known_fan")
  ? JSON.parse(JSON.stringify(strictStore.get("known_fan")))
  : null;

// Passive hover-card profiles (bio_length: null) with header counts + photo + privacy
const hoverBotProfile = {
  schema_version: P.SCHEMA_VERSION,
  artist: "_creator.beta_",
  username: "nawabayan111",
  has_profile_photo: true,
  account_private: true,
  post_count: 0,
  followers_count: 240,
  following_count: 5799,
  bio_length: null
};
const hoverHumanProfile = {
  schema_version: P.SCHEMA_VERSION,
  artist: "_creator.beta_",
  username: "denisa.stancu",
  has_profile_photo: true,
  account_private: false,
  post_count: 381,
  followers_count: 1450,
  following_count: 620,
  bio_length: null
};
const hoverBorderlineProfile = {
  schema_version: P.SCHEMA_VERSION,
  artist: "_creator.beta_",
  username: "user_12345",
  has_profile_photo: false,
  account_private: false,
  post_count: 10,
  followers_count: 90,
  following_count: 120,
  bio_length: null
};
out.hover_conclusive_bot = P.scoreFollowerProfile(hoverBotProfile);
out.hover_conclusive_human = P.scoreFollowerProfile(hoverHumanProfile);
out.hover_borderline = P.scoreFollowerProfile(hoverBorderlineProfile);
// Edge cases requested by owner:
// 1) Hover CHECK account (0 posts, 171 flwrs, 260 flwng, bio=null -> 45p..55p) stays uncommitted (null)
const hoverCheckProfile = {
  schema_version: P.SCHEMA_VERSION,
  artist: "_creator.beta_",
  username: "rafa.a.14",
  has_profile_photo: true,
  account_private: false,
  post_count: 0,
  followers_count: 171,
  following_count: 260,
  bio_length: null
};
// 2) Hover clear BOT even when privacy & photo flags are missing (null) STILL counts as Bot
const hoverBotMissingFlags = {
  schema_version: P.SCHEMA_VERSION,
  artist: "_creator.beta_",
  username: "cutlaibapathan",
  has_profile_photo: null,
  account_private: null,
  post_count: 0,
  followers_count: 7,
  following_count: 6149,
  bio_length: null
};
// 3) Hover clear REAL even when photo flag is null (max 30p < 35p) STILL counts as Human
const hoverHumanMissingPhoto = {
  schema_version: P.SCHEMA_VERSION,
  artist: "_creator.beta_",
  username: "denisa.stancu",
  has_profile_photo: null,
  account_private: false,
  post_count: 381,
  followers_count: 1450,
  following_count: 620,
  bio_length: null
};
// 4) Hover stitched handle with missing photo (20p + 30p unknown = 50p >= 35p) stays uncommitted
const hoverHumanMissingPhotoStitched = {
  schema_version: P.SCHEMA_VERSION,
  artist: "_creator.beta_",
  username: "_denisa.stancu_",
  has_profile_photo: null,
  account_private: false,
  post_count: 381,
  followers_count: 1450,
  following_count: 620,
  bio_length: null
};
out.hover_check_ambiguous = P.scoreFollowerProfile(hoverCheckProfile);
out.hover_bot_missing_flags = P.scoreFollowerProfile(hoverBotMissingFlags);
out.hover_human_missing_photo = P.scoreFollowerProfile(hoverHumanMissingPhoto);
out.hover_human_missing_photo_stitched = P.scoreFollowerProfile(hoverHumanMissingPhotoStitched);
out.hover_flags = {
  bot_has_header: P.hasHeaderCounts(hoverBotProfile),
  bot_is_scorable: P.isScorableProfile(hoverBotProfile),
  bot_missing_flags_has_header: P.hasHeaderCounts(hoverBotMissingFlags),
  human_has_header: P.hasHeaderCounts(hoverHumanProfile),
  list_has_header: P.hasHeaderCounts(listStore.get("quiet.fan"))
};

console.log(JSON.stringify(out));
