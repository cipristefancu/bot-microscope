/**
 * Bot Microscope v3.0.0 — Parser, Scorer & CSV Exporter (data schema v3)
 *
 * Rule R-NOFAKE: a number that was not observed is null, never a guess.
 *
 * Scoring is tri-state. A clue whose inputs are unknown is null. A profile is called
 * bot-like (true) when the clues already known exceed the cutoff, not bot-like (false)
 * only when every clue is known, and undetermined (null) otherwise.
 *
 * Clues (points):
 *   1. No Profile Photo (+20)
 *   2. Zero Posts (+25) OR 1-6 Same-Day Setup Posts (+15, strictly < 20)
 *   3. Mass Following (+25)
 *   4. Empty Bio (+10)
 *   5. Digit-Heavy / Farm Username (+20)
 */
(function (globalScope) {
  "use strict";

  const SCHEMA_VERSION = 3;
  const STAT_FIELDS = ["post_count", "followers_count", "following_count", "bio_length"];
  // Numbers that older builds invented for accounts they had not opened. Used only to recognise and remove them.
  const LEGACY_PLACEHOLDER_TUPLES = [[24, 340, 290, 42], [22, 320, 280, 40], [0, 18, 2150, 0], [3, 18, 1850, 0]];
  const DERIVED_FIELDS = ["bot_score", "score_pts", "bot_like", "flags", "same_day_post_burst"];
  const BOT_LIKE_CUTOFF_PTS = 60;
  // Option A 3-tier gray zone floor: complete <=60p profiles scoring >=35p are flagged Suspicious.
  const SUSPICIOUS_FLOOR_PTS = 35;
  // Passive hover profiles without bio_length must stay strictly below CHECK (>=35p) to auto-clear as Real.
  const REAL_CLEAR_CEILING_PTS = 34;

  const WORD_DOT_DIGITS_RE = /^[a-z]+\.\d{5,}$/i;
  const LONG_DIGIT_RUN_RE = /\d{5,}/;
  const ADJACENT_PUNCT_RE = /(\._|_\.|__|\.\.)/;
  const REPEATED_LETTER_RE = /([a-z])\1{3,}/i;
  const CONSONANT_CLUSTER_RE = /^[bcdfghjklmnpqrstvwxz]{2,4}$/i;
  const DEFAULT_AVATAR_HINTS = [
    "44884218_345707102882519_2446069589734326272_n",
    "default_profile",
    "anonymous_profile_pic"
  ];

  function isCount(v) {
    return typeof v === "number" && Number.isFinite(v) && v >= 0;
  }

  function asCount(v) {
    return isCount(v) ? Math.round(v) : null;
  }

  function asBool(v) {
    return typeof v === "boolean" ? v : null;
  }

  // Returns a whole number, or null when the text holds no readable number.
  function parseShorthandCount(raw) {
    if (typeof raw === "number") return asCount(raw);
    if (!raw || typeof raw !== "string") return null;
    let s = raw
      .trim()
      .toLowerCase()
      .replace(/\s+/g, "")
      .replace(/posts|postări|postari|followers|urmăritori|urmaritori|following|urmărește|urmareste/gi, "");

    let multiplier = 1;
    if (s.endsWith("k") || s.endsWith("mii")) {
      multiplier = 1000;
      s = s.replace(/(k|mii)$/i, "");
    } else if (s.endsWith("m") || s.endsWith("mil") || s.endsWith("mil.")) {
      multiplier = 1000000;
      s = s.replace(/(mil\.|mil|m)$/i, "");
    }

    if (multiplier > 1) {
      s = s.replace(",", ".");
      const val = parseFloat(s);
      return Number.isFinite(val) ? Math.round(val * multiplier) : null;
    }

    const digitsOnly = s.replace(/[^\d]/g, "");
    if (!digitsOnly) return null;
    const parsed = parseInt(digitsOnly, 10);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function computeDigitRatio(username) {
    const clean = String(username || "").trim();
    if (!clean) return 0;
    let digits = 0;
    for (let i = 0; i < clean.length; i++) {
      if (clean[i] >= "0" && clean[i] <= "9") digits++;
    }
    return Number((digits / clean.length).toFixed(4));
  }

  function isStitchedOrGlitchHandle(username) {
    const clean = String(username || "").trim().toLowerCase().replace(/^@/, "");
    if (!clean) return false;
    if (ADJACENT_PUNCT_RE.test(clean)) return true;
    if (REPEATED_LETTER_RE.test(clean)) return true;
    let punctCount = 0;
    for (let i = 0; i < clean.length; i++) {
      if (clean[i] === "." || clean[i] === "_") punctCount++;
    }
    if (punctCount >= 3) return true;
    const segments = clean.split(/[._]+/).filter(Boolean);
    if (punctCount >= 2) {
      const shortSegs = segments.filter((s) => s.length <= 2).length;
      const hasSingle = segments.some((s) => s.length === 1);
      const hasConsonantSeg = segments.some((s) => CONSONANT_CLUSTER_RE.test(s));
      if ((hasSingle && shortSegs >= 2) || hasConsonantSeg) return true;
    }
    if ((clean.startsWith("_") || clean.endsWith("_")) && punctCount >= 2) {
      return true;
    }
    if (
      clean.endsWith("_") &&
      segments.some((s) => s.length === 2 && CONSONANT_CLUSTER_RE.test(s))
    ) {
      return true;
    }
    return false;
  }

  function analyzeHandleForensics(username, digitThreshold = 0.4) {
    const clean = String(username || "").trim();
    const ratio = computeDigitRatio(clean);
    const isStitched = isStitchedOrGlitchHandle(clean);
    return {
      digit_ratio: ratio,
      is_digit_heavy: ratio > digitThreshold,
      is_word_dot_digits: WORD_DOT_DIGITS_RE.test(clean),
      has_long_digit_run: LONG_DIGIT_RUN_RE.test(clean),
      is_stitched_handle: isStitched
    };
  }

  // true / false, or null when the numbers needed to decide were not observed.
  function detectSameDayPostBurst(
    postCount,
    postTimestamps,
    followersCount,
    followingCount,
    bioLength,
    username
  ) {
    const pCount = asCount(postCount);
    if (pCount === null) return null;
    if (pCount < 1 || pCount > 6) return false;

    const valid = Array.isArray(postTimestamps)
      ? postTimestamps.map((t) => Number(t)).filter((t) => Number.isFinite(t) && t > 0)
      : [];
    if (valid.length >= 2) {
      const spanSeconds = Math.max(...valid) - Math.min(...valid);
      return spanSeconds <= 172800; // Within 48h (1-day / same-day setup burst)
    }

    const flwrs = asCount(followersCount);
    const flwing = asCount(followingCount);
    if (flwrs === null || flwing === null) return null;

    const ratio = flwing / Math.max(1, flwrs);
    if ((flwing >= 350 && ratio >= 8.0) || (flwrs <= 40 && flwing >= 80)) return true;
    const hf = analyzeHandleForensics(username);
    const suspectHandle =
      hf.is_word_dot_digits ||
      hf.is_digit_heavy ||
      hf.has_long_digit_run ||
      hf.is_stitched_handle;
    if (!suspectHandle) return false;
    const bio = asCount(bioLength);
    if (bio === null) return null;
    return bio <= 4;
  }

  function scoreFollowerProfile(profile, customWeights, knownBotHandles) {
    const weights = Object.assign(
      {
        no_photo: 20,
        zero_posts: 25,
        same_day_post_burst: 15, // Strictly < 20 pts per Locked Requirement #1
        mass_following: 25,
        extreme_mass_following: 30,
        follow_exchange_ring: 20,
        low_audience_imbalance: 15,
        shared_post_bot_signal: 20,
        empty_bio: 10,
        digit_heavy_username: 20,
        stitched_username: 10,
        ratio_cluster_cap: 60,
        suspicious_cutoff_pts: SUSPICIOUS_FLOOR_PTS,
        known_registry_bot: 25,
        score_private_accounts: true
      },
      customWeights || {}
    );

    const uname = String(profile.username || "").trim();
    const hf = analyzeHandleForensics(uname);
    const hasPhoto = asBool(profile.has_profile_photo);
    const isPrivate = asBool(profile.account_private);
    const pCount = asCount(profile.post_count);
    const flwrs = asCount(profile.followers_count);
    const flwing = asCount(profile.following_count);
    const bioLen = asCount(profile.bio_length);
    const hidePrivateClues = weights.score_private_accounts === false;
    const skipProfileClues = isPrivate === true && hidePrivateClues;
    const unknownPrivateClues = isPrivate === null && hidePrivateClues;

    const isRegistryBot = Boolean(
      profile.known_registry_bot ||
      profile.is_known_bot ||
      (knownBotHandles && (
        typeof knownBotHandles.has === "function"
          ? knownBotHandles.has(uname.toLowerCase())
          : Boolean(knownBotHandles[uname.toLowerCase()])
      ))
    );

    const digitHeavy = Boolean(
      hf.is_digit_heavy ||
      hf.is_word_dot_digits ||
      hf.has_long_digit_run
    );
    const stitchedOnly = Boolean(hf.is_stitched_handle) && !digitHeavy;

    const flags = {
      no_photo: hasPhoto === null ? null : !hasPhoto,
      zero_posts: false,
      same_day_post_burst: false,
      mass_following: false,
      extreme_mass_following: false,
      follow_exchange_ring: false,
      low_audience_imbalance: false,
      shared_post_bot_signal: false,
      empty_bio: false,
      digit_heavy_username: digitHeavy,
      stitched_username: stitchedOnly,
      known_registry_bot: isRegistryBot
    };

    if (unknownPrivateClues) {
      flags.zero_posts = null;
      flags.same_day_post_burst = null;
      flags.mass_following = null;
      flags.extreme_mass_following = null;
      flags.follow_exchange_ring = null;
      flags.low_audience_imbalance = null;
      flags.shared_post_bot_signal = null;
      flags.empty_bio = null;
    } else if (!skipProfileClues) {
      flags.zero_posts = pCount === null ? null : pCount === 0;
      if (flags.zero_posts === true) {
        flags.same_day_post_burst = false;
      } else if (flags.zero_posts === null) {
        flags.same_day_post_burst = null;
      } else {
        flags.same_day_post_burst = detectSameDayPostBurst(
          pCount,
          profile.post_timestamps,
          flwrs,
          flwing,
          bioLen,
          uname
        );
      }
      flags.empty_bio = bioLen === null ? null : bioLen <= 4;
      if (flwrs === null || flwing === null) {
        flags.mass_following = null;
      } else {
        const fRatio = flwing / Math.max(1, flwrs);
        const lowPosts = pCount !== null && pCount <= 25;
        const veryLowPosts = pCount !== null && pCount <= 20;
        flags.mass_following =
          (flwing > 1500 && flwrs < 50) ||
          (flwing >= 1500 && fRatio >= 5.0) ||
          (flwing >= 200 && fRatio >= 8.0) ||
          (flwing >= 50 && flwrs <= 20 && fRatio >= 10.0) ||
          (flwing >= 5000 && flwrs >= 3000 && lowPosts) ||
          (flwing >= 6500 && veryLowPosts);
      }
      if (flwrs === null || flwing === null || pCount === null) {
        flags.extreme_mass_following = null;
        flags.follow_exchange_ring = null;
      } else {
        const fRatio = flwing / Math.max(1, flwrs);
        flags.extreme_mass_following =
          (flwing >= 200 && fRatio >= 15.0 && pCount <= 40) ||
          (flwing >= 100 && flwrs <= 25 && fRatio >= 12.0) ||
          (flwing >= 500 && flwrs <= 50 && pCount <= 40);
        flags.follow_exchange_ring = flwing >= 5000 && flwrs >= 5000 && pCount <= 25;
      }
      if (pCount === null || flwrs === null || flwing === null) {
        flags.low_audience_imbalance = null;
        flags.shared_post_bot_signal = null;
      } else {
        const fRatio = flwing / Math.max(1, flwrs);
        const nonBioLowAud =
          (pCount <= 25 && flwrs <= 80 && flwing >= 150) ||
          (pCount <= 25 && flwing >= 3500 && fRatio >= 2.5);
        const bioLowAudCond = pCount <= 25 && flwrs <= 100;
        if (nonBioLowAud) {
          flags.low_audience_imbalance = true;
        } else if (!bioLowAudCond) {
          flags.low_audience_imbalance = false;
        } else if (bioLen === null) {
          flags.low_audience_imbalance = null;
        } else {
          flags.low_audience_imbalance = bioLen <= 4;
        }

        const trailMatch = uname.match(/\d+$/);
        const trailDigits = trailMatch ? trailMatch[0].length : 0;
        const nonBioProfileSignal =
          pCount >= 1 && pCount <= 30 && flwrs <= 150 && flwing >= 300 && fRatio >= 4.0;
        const bioProfileCond =
          (pCount >= 1 && pCount <= 25 && flwing >= 1500 && fRatio >= 3.0) ||
          (pCount >= 7 &&
            pCount <= 35 &&
            flwrs <= 200 &&
            (trailDigits >= 3 || hf.digit_ratio > 0.25 || hf.is_stitched_handle));
        const obsPosts = asCount(profile.observed_posts_count);
        const avgLikes =
          typeof profile.avg_post_likes === "number" && Number.isFinite(profile.avg_post_likes)
            ? profile.avg_post_likes
            : null;
        const sharedRatio =
          typeof profile.shared_post_ratio === "number" && Number.isFinite(profile.shared_post_ratio)
            ? profile.shared_post_ratio
            : null;
        const feedSignal =
          obsPosts !== null &&
          obsPosts >= 3 &&
          ((sharedRatio !== null && sharedRatio >= 0.70 && pCount <= 60) ||
            (flwrs >= 500 &&
              pCount >= 5 &&
              avgLikes !== null &&
              avgLikes / Math.max(1, flwrs) < 0.003));
        if (nonBioProfileSignal || feedSignal) {
          flags.shared_post_bot_signal = true;
        } else if (!bioProfileCond) {
          flags.shared_post_bot_signal = false;
        } else if (bioLen === null) {
          flags.shared_post_bot_signal = null;
        } else {
          flags.shared_post_bot_signal = bioLen <= 4;
        }
      }
    }

    const shellBonusPts =
      flags.zero_posts === true && flags.mass_following === true && flwing >= 1000
        ? weights.same_day_post_burst
        : 0;
    const ratioClusterRawPts =
      (flags.mass_following === true ? weights.mass_following : 0) +
      (flags.extreme_mass_following === true ? weights.extreme_mass_following : 0) +
      (flags.low_audience_imbalance === true ? weights.low_audience_imbalance : 0) +
      (flags.shared_post_bot_signal === true ? weights.shared_post_bot_signal : 0);
    const ratioClusterPts = Math.min(weights.ratio_cluster_cap, ratioClusterRawPts);
    const ratioClusterCapped = ratioClusterRawPts > weights.ratio_cluster_cap;
    flags.ratio_cluster_capped = ratioClusterCapped;

    const rawScorePts =
      (flags.no_photo === true ? weights.no_photo : 0) +
      (flags.zero_posts === true ? weights.zero_posts : 0) +
      (flags.same_day_post_burst === true ? weights.same_day_post_burst : 0) +
      shellBonusPts +
      ratioClusterPts +
      (flags.follow_exchange_ring === true ? weights.follow_exchange_ring : 0) +
      (flags.empty_bio === true ? weights.empty_bio : 0) +
      (flags.digit_heavy_username ? weights.digit_heavy_username : 0) +
      (flags.stitched_username ? weights.stitched_username : 0) +
      (flags.known_registry_bot === true ? weights.known_registry_bot : 0);
    const totalPts = Math.min(100, rawScorePts);

    // Exact upper bound of points that unknown clues could still add (preserving C2 ratio cluster cap).
    let otherUnknownPts = 0;
    if (flags.no_photo === null) otherUnknownPts += weights.no_photo;
    if (flags.zero_posts === null) {
      const potentialShellBonus =
        flags.mass_following !== false && (flwing === null || flwing >= 1000)
          ? weights.same_day_post_burst
          : 0;
      const zeroBranchPts = weights.zero_posts + potentialShellBonus;
      const burstBranchPts = flags.same_day_post_burst === null ? weights.same_day_post_burst : 0;
      otherUnknownPts += Math.max(zeroBranchPts, burstBranchPts);
    } else {
      if (flags.same_day_post_burst === null) otherUnknownPts += weights.same_day_post_burst;
      if (
        flags.zero_posts === true &&
        flags.mass_following === null &&
        (flwing === null || flwing >= 1000)
      ) {
        otherUnknownPts += weights.same_day_post_burst;
      }
    }
    if (flags.follow_exchange_ring === null) otherUnknownPts += weights.follow_exchange_ring;
    if (flags.empty_bio === null) otherUnknownPts += weights.empty_bio;

    const ratioUnknownRawPts =
      (flags.mass_following === null ? weights.mass_following : 0) +
      (flags.extreme_mass_following === null ? weights.extreme_mass_following : 0) +
      (flags.low_audience_imbalance === null ? weights.low_audience_imbalance : 0) +
      (flags.shared_post_bot_signal === null ? weights.shared_post_bot_signal : 0);
    const ratioClusterMaxEff = Math.min(
      weights.ratio_cluster_cap,
      ratioClusterRawPts + ratioUnknownRawPts
    );
    const ratioUnknownEff = Math.max(0, ratioClusterMaxEff - ratioClusterPts);
    const unknownPts = otherUnknownPts + ratioUnknownEff;

    const maxScorePts = Math.min(100, totalPts + unknownPts);
    const unknownFlags = Object.keys(flags).filter((k) => flags[k] === null);
    const complete = unknownFlags.length === 0;
    const isFullComplete = complete && isScorableProfile(profile);
    // Call Bot (>60) once known clues exceed cutoff; call Real/Suspicious on full profiles when <=60,
    // or auto-clear passive hover profiles only when worst-case points stay strictly below 35 (no CHECK ambiguity).
    const botLike =
      totalPts > BOT_LIKE_CUTOFF_PTS
        ? true
        : isFullComplete
          ? false
          : hasHeaderCounts(profile) && maxScorePts <= REAL_CLEAR_CEILING_PTS
            ? false
            : null;
    const conclusive = botLike !== null;
    const suspFloor = Number.isFinite(Number(weights.suspicious_cutoff_pts))
      ? Number(weights.suspicious_cutoff_pts)
      : SUSPICIOUS_FLOOR_PTS;
    const verdictTier =
      botLike === true
        ? "bot"
        : botLike === null
          ? "unknown"
          : isFullComplete && totalPts >= suspFloor
            ? "suspicious"
            : "clean";

    return {
      scorePts: totalPts,
      rawScorePts,
      maxScorePts,
      ratioClusterRawPts,
      ratioClusterPts,
      ratioClusterCapped,
      verdictTier,
      botScore: Number((totalPts / 100).toFixed(4)),
      botLike,
      conclusive,
      complete,
      unknownFlags,
      flags,
      forensics: hf
    };
  }

  function parseProfileHeaderStats(rawText) {
    const text = String(rawText || "");
    if (!text) return null;
    const postsMatch = text.match(/([\d.,]+(?:\s*(?:k|m|mii|mil\.?))?)\s*(?:posts?|postări|postari)/i);
    const flwrsMatch = text.match(
      /([\d.,]+(?:\s*(?:k|m|mii|mil\.?))?)\s*(?:followers?|urmăritori|urmaritori)/i
    );
    const flwingMatch = text.match(
      /([\d.,]+(?:\s*(?:k|m|mii|mil\.?))?)\s*(?:following|urmăriri|urmariri|urmărește|urmareste)/i
    );
    if (!postsMatch && !flwrsMatch && !flwingMatch) return null;
    return {
      post_count: postsMatch ? parseShorthandCount(postsMatch[1]) : null,
      followers_count: flwrsMatch ? parseShorthandCount(flwrsMatch[1]) : null,
      following_count: flwingMatch ? parseShorthandCount(flwingMatch[1]) : null
    };
  }

  function hasRealAvatarUrl(url, hasAnonFlag) {
    if (hasAnonFlag === true) return false;
    if (!url || typeof url !== "string") return true;
    return !DEFAULT_AVATAR_HINTS.some((hint) => url.indexOf(hint) !== -1);
  }

  // Data tier from what was actually observed: 0 handle only, 1 + avatar/private flag,
  // 2 + all four numbers, 3 + post timestamps.
  function profileTier(p) {
    if (!p) return 0;
    if (STAT_FIELDS.every((f) => isCount(p[f]))) {
      return Array.isArray(p.post_timestamps) && p.post_timestamps.length > 0 ? 3 : 2;
    }
    if (typeof p.has_profile_photo === "boolean" || typeof p.account_private === "boolean") return 1;
    return 0;
  }

  const HEADER_STAT_FIELDS = ["post_count", "followers_count", "following_count"];

  // True when every input the scorer needs was observed.
  function isScorableProfile(p) {
    return (
      !!p &&
      STAT_FIELDS.every((f) => isCount(p[f])) &&
      typeof p.has_profile_photo === "boolean" &&
      typeof p.account_private === "boolean"
    );
  }

  // True when hover-card / header counts (posts, followers, following) were observed.
  function hasHeaderCounts(p) {
    return !!p && HEADER_STAT_FIELDS.every((f) => isCount(p[f]));
  }

  function partitionProfiles(profiles) {
    const scorable = [];
    const incomplete = [];
    for (const p of profiles || []) {
      (isScorableProfile(p) ? scorable : incomplete).push(p);
    }
    return { scorable, incomplete };
  }

  function isLegacyPlaceholder(p) {
    if (p.schema_version !== undefined && p.schema_version !== null) return false;
    if (p.deep_checked === true) return false;
    return LEGACY_PLACEHOLDER_TUPLES.some((t) => STAT_FIELDS.every((f, i) => p[f] === t[i]));
  }

  // Turns numbers invented by older builds back into "unknown" (null).
  function scrubLegacyPlaceholders(p) {
    if (!p || typeof p !== "object") return p;
    const out = Object.assign({}, p);
    if (isLegacyPlaceholder(out)) {
      STAT_FIELDS.forEach((f) => {
        out[f] = null;
      });
      out.account_private = null;
      out.deep_checked = false;
      out.legacy_scrubbed = true;
      DERIVED_FIELDS.forEach((f) => {
        delete out[f];
      });
    } else if (out.schema_version === undefined || out.schema_version === null) {
      if (STAT_FIELDS.some((f) => isCount(out[f]))) out.legacy_unverified = true;
    }
    out.tier = profileTier(out);
    return out;
  }

  const NON_FOLLOWER_SUBTREE_KEYS = new Set([
    "coauthor_producers",
    "usertags",
    "tagged_users",
    "likers",
    "comments",
    "preview_comments",
    "chaining_results",
    "suggested_users",
    "edge_related_profiles",
    "edge_chaining",
    "edge_mutual_followed_by",
    "edge_follow",
    "following",
    "tray",
    "reels_tray",
    "feed_items",
    "clips_items",
    "sponsor_tags",
    "sponsors",
    "edge_owner_to_timeline_media",
    "xdt_api__v1__feed__user_timeline_graphql_connection",
    "xdt_api__v1__feed__timeline__connection",
    "owner",
    "caption",
    "items"
  ]);

  function classifyInstagramInterceptUrl(url) {
    const u = String(url || "");
    if (!u) return "ignore";
    if (/\/api\/v1\/friendships\/[^/]+\/following\b/i.test(u)) return "following_list";
    if (/\/api\/v1\/friendships\/[^/]+\/followers\b/i.test(u)) return "followers_list";
    if (
      /(\/api\/v1\/users\/web_profile_info\/|\/api\/v1\/feed\/user\/|\/graphql\/query|\/api\/graphql)/i.test(
        u
      )
    ) {
      return "profile_detail";
    }
    return "ignore";
  }

  function isFollowersModalDialogText(headingOrText) {
    const lines = String(headingOrText || "")
      .split(/[\r\n]+/)
      .map((l) => l.trim())
      .filter(Boolean)
      .slice(0, 4);
    if (lines.length === 0) return false;
    const nonFollowersRe =
      /^(following|urmăriri|urmariri|urmărește|urmareste|likes|aprecieri|comments|comentarii|shares?|distribuiri)$/i;
    const searchRe = /^(search|căutare|cautare)$/i;
    const followersRe = /^(followers|urmăritori|urmaritori)$/i;
    let sawFollowers = false;
    for (const line of lines) {
      if (searchRe.test(line)) continue;
      if (nonFollowersRe.test(line)) return false;
      if (followersRe.test(line)) sawFollowers = true;
    }
    return sawFollowers;
  }

  function walkExtractInstagramJson(obj, artistHandle, store, lowerIndex, opts) {
    if (!obj || typeof obj !== "object") return store;

    if (Array.isArray(obj)) {
      for (let i = 0; i < obj.length; i++) {
        walkExtractInstagramJson(obj[i], artistHandle, store, lowerIndex, opts);
      }
      return store;
    }

    if (typeof obj.username === "string" && obj.username.trim()) {
      const uname = obj.username.trim().replace(/^@/, "");
      const uLow = uname.toLowerCase();
      const isSelfArtist = opts.excludeArtist && opts.artistLow && uLow === opts.artistLow;
      const existingKey = lowerIndex.get(uLow) || null;
      const isAllowed =
        existingKey !== null ||
        opts.allowNew ||
        (opts.allowedSet !== null && opts.allowedSet.has(uLow));

      if (!isSelfArtist && isAllowed) {
        const targetKey = existingKey || uname;
        const prior = store.get(targetKey);
        const row = prior
          ? Object.assign({}, prior)
          : {
              schema_version: SCHEMA_VERSION,
              artist: artistHandle || "Audited_Account",
              username: targetKey,
              has_profile_photo: null,
              account_private: null,
              post_count: null,
              followers_count: null,
              following_count: null,
              bio_length: null,
              post_timestamps: []
            };
        row.src = Object.assign({}, row.src || {});
        const observed = (field, value) => {
          row[field] = value;
          row.src[field] = "api_json";
        };

        if (obj.has_anonymous_profile_picture !== undefined || obj.profile_pic_url) {
          observed(
            "has_profile_photo",
            hasRealAvatarUrl(obj.profile_pic_url, obj.has_anonymous_profile_picture)
          );
        }
        if (typeof obj.is_private === "boolean") {
          observed("account_private", obj.is_private);
        }
        if (isCount(obj.media_count)) {
          observed("post_count", obj.media_count);
        }
        const mediaContainer =
          (obj.edge_owner_to_timeline_media && typeof obj.edge_owner_to_timeline_media === "object"
            ? obj.edge_owner_to_timeline_media
            : null) ||
          (obj.xdt_api__v1__feed__user_timeline_graphql_connection &&
          typeof obj.xdt_api__v1__feed__user_timeline_graphql_connection === "object"
            ? obj.xdt_api__v1__feed__user_timeline_graphql_connection
            : null);
        if (mediaContainer) {
          if (isCount(mediaContainer.count)) {
            observed("post_count", mediaContainer.count);
          }
          const edges = Array.isArray(mediaContainer.edges)
            ? mediaContainer.edges
            : Array.isArray(mediaContainer.items)
              ? mediaContainer.items
              : null;
          if (edges) {
            const ts = [];
            const likeVals = [];
            let sharedHits = 0;
            let obsPosts = 0;
            for (const edge of edges) {
              const node = edge && edge.node && typeof edge.node === "object" ? edge.node : edge;
              if (!node || typeof node !== "object") continue;
              obsPosts++;
              const t = node.taken_at_timestamp || node.taken_at;
              if (typeof t === "number" && t > 0) ts.push(t);
              const lc =
                typeof node.like_count === "number"
                  ? node.like_count
                  : node.edge_liked_by && typeof node.edge_liked_by.count === "number"
                    ? node.edge_liked_by.count
                    : null;
              if (typeof lc === "number" && lc >= 0) likeVals.push(lc);
              const ownerObj = node.owner || node.user;
              const ownerName =
                ownerObj && typeof ownerObj.username === "string"
                  ? ownerObj.username.trim().toLowerCase()
                  : "";
              const isShared = Boolean(
                node.is_repost ||
                  node.repost_context ||
                  (Array.isArray(node.coauthor_producers) && node.coauthor_producers.length > 0) ||
                  (ownerName && ownerName !== uLow)
              );
              if (isShared) sharedHits++;
            }
            if (ts.length > 0) {
              row.post_timestamps = ts;
              row.src.post_timestamps = "api_json";
            }
            if (obsPosts > 0) {
              row.observed_posts_count = obsPosts;
              if (likeVals.length > 0) {
                row.avg_post_likes = Number(
                  (likeVals.reduce((a, b) => a + b, 0) / likeVals.length).toFixed(2)
                );
              }
              row.shared_post_ratio = Number((sharedHits / obsPosts).toFixed(4));
              row.src.feed_signals = "api_json";
            }
          }
        }
        if (isCount(obj.follower_count)) {
          observed("followers_count", obj.follower_count);
        } else if (obj.edge_followed_by && isCount(obj.edge_followed_by.count)) {
          observed("followers_count", obj.edge_followed_by.count);
        }
        if (isCount(obj.following_count)) {
          observed("following_count", obj.following_count);
        } else if (obj.edge_follow && isCount(obj.edge_follow.count)) {
          observed("following_count", obj.edge_follow.count);
        }
        if (typeof obj.biography === "string") {
          observed("bio_length", obj.biography.trim().length);
        } else if (
          obj.biography_with_entities &&
          typeof obj.biography_with_entities.raw_text === "string"
        ) {
          observed("bio_length", obj.biography_with_entities.raw_text.trim().length);
        }

        // Every number now comes from Instagram's own data, so an older row is no longer "legacy".
        if (STAT_FIELDS.every((f) => row.src[f] === "api_json")) {
          row.schema_version = SCHEMA_VERSION;
          delete row.legacy_unverified;
          delete row.legacy_scrubbed;
        }

        const hf = analyzeHandleForensics(targetKey);
        row.username_digit_ratio = hf.digit_ratio;
        row.deep_checked = STAT_FIELDS.every((f) => isCount(row[f]));
        row.tier = profileTier(row);
        row.collected_at = row.collected_at || new Date().toISOString().slice(0, 10);

        const scored = scoreFollowerProfile(row);
        row.same_day_post_burst = scored.flags.same_day_post_burst;
        row.bot_score = scored.botScore;
        row.score_pts = scored.scorePts;
        row.raw_score_pts = scored.rawScorePts;
        row.max_score_pts = scored.maxScorePts;
        row.ratio_cluster_raw_pts = scored.ratioClusterRawPts;
        row.ratio_cluster_pts = scored.ratioClusterPts;
        row.ratio_cluster_capped = scored.ratioClusterCapped;
        row.verdict_tier = scored.verdictTier;
        row.bot_like = scored.botLike;
        row.score_conclusive = scored.conclusive;
        row.score_complete = scored.complete;
        row.flags = scored.flags;

        lowerIndex.set(uLow, targetKey);
        store.set(targetKey, row);
      }
    }

    const isParentUserOrMedia = Boolean(
      (typeof obj.username === "string" && obj.username.trim()) ||
        obj.taken_at ||
        obj.taken_at_timestamp ||
        obj.media_type ||
        obj.code
    );
    for (const key of Object.keys(obj)) {
      if (opts.strictFollowerOnly) {
        if (NON_FOLLOWER_SUBTREE_KEYS.has(key)) continue;
        if (key === "user" && isParentUserOrMedia) continue;
      }
      const val = obj[key];
      if (val && typeof val === "object") {
        walkExtractInstagramJson(val, artistHandle, store, lowerIndex, opts);
      }
    }
    return store;
  }

  function extractProfilesFromInstagramJson(obj, artistHandle, mapOut, options) {
    const store = mapOut || new Map();
    if (!obj || typeof obj !== "object") return store;
    const rawOpts = options && typeof options === "object" ? options : {};
    const artistClean = String(artistHandle || "").trim().replace(/^@/, "");
    const artistLow = artistClean.toLowerCase();
    const opts = {
      allowNew: rawOpts.allowNew !== false,
      allowedSet:
        rawOpts.allowedUsernames instanceof Set
          ? new Set(Array.from(rawOpts.allowedUsernames, (u) => String(u).toLowerCase()))
          : null,
      excludeArtist:
        rawOpts.excludeArtist !== undefined
          ? Boolean(rawOpts.excludeArtist)
          : Boolean(artistLow && artistLow !== "audited_account"),
      strictFollowerOnly: rawOpts.strictFollowerOnly === true,
      artistLow
    };
    const lowerIndex = new Map();
    for (const k of store.keys()) {
      lowerIndex.set(String(k).toLowerCase(), k);
    }
    return walkExtractInstagramJson(obj, artistHandle, store, lowerIndex, opts);
  }

  // Only profiles whose numbers were all observed are exported; unknown numbers are never filled in.
  function profilesToCsv(profiles, defaultArtist = "Audited_Account") {
    const headers = [
      "artist",
      "username",
      "has_profile_photo",
      "post_count",
      "followers_count",
      "following_count",
      "bio_length",
      "account_private",
      "username_digit_ratio"
    ];
    const lines = [headers.join(",")];
    for (const p of partitionProfiles(profiles).scorable) {
      const uname = String(p.username || "").replace(/,/g, "");
      if (!uname) continue;
      const artist = String(p.artist || defaultArtist).replace(/,/g, "");
      lines.push(
        [
          artist,
          uname,
          p.has_profile_photo ? "true" : "false",
          p.post_count,
          p.followers_count,
          p.following_count,
          p.bio_length,
          p.account_private ? "true" : "false",
          computeDigitRatio(uname).toFixed(4)
        ].join(",")
      );
    }
    return lines.join("\n") + "\n";
  }

  globalScope.BotAuditParser = {
    SCHEMA_VERSION,
    parseShorthandCount,
    parseProfileHeaderStats,
    computeDigitRatio,
    isStitchedOrGlitchHandle,
    analyzeHandleForensics,
    detectSameDayPostBurst,
    scoreFollowerProfile,
    profileTier,
    isScorableProfile,
    hasHeaderCounts,
    partitionProfiles,
    scrubLegacyPlaceholders,
    classifyInstagramInterceptUrl,
    isFollowersModalDialogText,
    extractProfilesFromInstagramJson,
    profilesToCsv
  };
})(typeof window !== "undefined" ? window : globalThis);
