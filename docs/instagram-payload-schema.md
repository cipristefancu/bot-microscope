# Instagram Web JSON Payload Schema Discovery & Field Availability Matrix

**Specification ID**: `P6-T4.1` / `P6-T4.2`  
**Phase**: Phase 6 (Ground Truth, Honest Data Capture & Evaluation Harness)  
**Component**: Passive Read-Only Instagram Web Payload Specification  
**Status**: Verified & Locked (2026-11-06)  

---

## 1. Overview & Capture Methodology

This document resolves the open data-availability assumptions for **Phases 6 through 10** of `bot-microscope`. 

All findings are based on passive network interception (`browser-extension/injected-interceptor.js`) of Instagram Web's native internal JSON API (`https://www.instagram.com/api/v1/...` and `/graphql/query`). No third-party scraping services, external proxies, or private mobile APIs were used.

### The Four Intercepted Endpoints
1. **Followers List Scroll / Pagination**: `GET /api/v1/friendships/{user_id}/followers/?count=...&max_id=...`
2. **Followers Search Slices**: `GET /api/v1/friendships/{user_id}/followers/search/?query={slice}`
3. **Web Profile Info (Profile Open / Hover)**: `GET /api/v1/users/web_profile_info/?username={username}`
4. **Post Details & Comments**: `POST /graphql/query` (query hashes for `PolarisPostRootQuery` and `PolarisPostCommentsRefetchQuery`)

---

## 2. Master Field Availability & Provenance Matrix

Every field evaluated for the scoring engine and planned forensic modules (Phases 6–10) is documented below with its exact availability status:

| Field Name | Availability | Data Tier | Endpoint / Payload Path | Phase Usage | Forensic Value & Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `pk` / `id` | **AVAILABLE** | T1 | `user.pk` or `user.id` | Phase 9B, Phase 10 | Sequential numeric account ID. Essential for account age cohort estimation. |
| `username` | **AVAILABLE** | T0 | `user.username` | Phase 6, 7, 10 | Normalized handle string. Primary key for forensics and registry. |
| `full_name` | **AVAILABLE** | T1 | `user.full_name` | Phase 7 | Display name string. Used for entropy and template detection. |
| `is_private` | **AVAILABLE** | T1 | `user.is_private` | Phase 6, 7 | Boolean flag. Private accounts cannot hide metadata. |
| `is_verified` | **AVAILABLE** | T1 | `user.is_verified` | Phase 7 | Blue check badge flag. |
| `has_anonymous_profile_picture` | **AVAILABLE** | T1 | `user.has_anonymous_profile_picture` | Phase 6, 7 | Boolean flag. True when default silhouette avatar is present. |
| `profile_pic_url` | **AVAILABLE** | T1 | `user.profile_pic_url` | Phase 6, 7 | Avatar image URL. Hint strings match default silhouette assets. |
| `media_count` / `post_count` | **AVAILABLE** | T2 | `user.media_count` or `edge_owner_to_timeline_media.count` | Phase 6, 7 | Total lifetime posts. Essential for 0-post and setup burst clues. |
| `follower_count` / `followers_count` | **AVAILABLE** | T2 | `user.follower_count` or `edge_followed_by.count` | Phase 6, 7 | Number of followers. Required for follower/following ratio. |
| `following_count` | **AVAILABLE** | T2 | `user.following_count` or `edge_follow.count` | Phase 6, 7 | Number of accounts followed. Essential for Mass-Following clue. |
| `biography` / `bio_length` | **AVAILABLE** | T2 | `user.biography` | Phase 6, 7 | Profile bio text. String length used for Empty Bio clue. |
| `external_url` | **AVAILABLE** | T2 | `user.external_url` or `bio_links[].url` | Phase 7 | Link-in-bio presence and domain. |
| `taken_at_timestamp` (Posts) | **AVAILABLE** | T3 | `edge_owner_to_timeline_media.edges[].node.taken_at_timestamp` | Phase 6, 7 | Unix integer timestamp (seconds). **Enables 100% exact same-day burst detection**. |
| `like_count` (Posts) | **AVAILABLE** | T3 | `edge_media_preview_like.count` | Phase 8 (B1) | Post like count. Basis for artist engagement rate forensics. |
| `comment_count` (Posts) | **AVAILABLE** | T3 | `edge_media_to_comment.count` | Phase 8 (B1) | Post comment count. Basis for comment-to-like ratio forensics. |
| `comment_text` (Comments) | **AVAILABLE** | T3 | `edge_media_to_parent_comment.edges[].node.text` | Phase 8 (B1) | Comment text. Used for emoji-burst and generic pod comment detection. |
| `account_creation_date` / `date_joined` | **UNAVAILABLE** | — | Not present in public web JSON | Phase 9 (B2) | **Not exposed in public Web API**. (Only shown in mobile UI "About this account"). **Resolved**: Phase 9 uses `pk` numeric cohorts as proxy. |
| `follower_order_metric` | **UNAVAILABLE** | — | Not present in payload | Phase 6 | Sorting algorithm score is not exposed. **Resolved**: Record sequential `list_position` in collector. |
| `mutual_followers_count` | **AVAILABLE** | T1 | `user.mutual_followers_count` | Phase 7 | Number of mutual connections between viewer and follower. |

---

## 3. Redacted Payload Structures

### 3.1 Followers List Item (`/api/v1/friendships/{id}/followers/` or `/search/`)
*Data Quality Tier: Tier 1 (T1)*
```json
{
  "pk": "68492019481",
  "pk_id": "68492019481",
  "id": "68492019481",
  "username": "example_follower_94",
  "full_name": "Alex Vance",
  "is_private": false,
  "is_verified": false,
  "profile_pic_url": "https://instagram.fotp3-1.fna.fbcdn.net/v/t51.2885-19/...",
  "has_anonymous_profile_picture": false,
  "fbid_v2": "17841468492019481",
  "third_party_downloads_enabled": 0,
  "strong_id__": "68492019481"
}
```

### 3.2 Profile Header Payload (`/api/v1/users/web_profile_info/?username={username}`)
*Data Quality Tier: Tier 2 (T2) & Tier 3 (T3)*
```json
{
  "data": {
    "user": {
      "id": "583920194",
      "username": "example_inspected_user",
      "full_name": "Elena R.",
      "biography": "Visual artist & sound designer 🎹",
      "external_url": "https://linktr.ee/example",
      "is_private": false,
      "is_verified": false,
      "profile_pic_url": "https://instagram.fotp3-1.fna.fbcdn.net/v/t51.2885-19/...",
      "has_anonymous_profile_picture": false,
      "edge_followed_by": {
        "count": 482
      },
      "edge_follow": {
        "count": 315
      },
      "edge_owner_to_timeline_media": {
        "count": 14,
        "page_info": {
          "has_next_page": true,
          "end_cursor": "QVFB..."
        },
        "edges": [
          {
            "node": {
              "id": "321948201948201",
              "shortcode": "Cz19A4...",
              "taken_at_timestamp": 1729864200,
              "edge_media_preview_like": {
                "count": 42
              },
              "edge_media_to_comment": {
                "count": 6
              }
            }
          },
          {
            "node": {
              "id": "321849201849201",
              "shortcode": "Cz17B2...",
              "taken_at_timestamp": 1729862100,
              "edge_media_preview_like": {
                "count": 38
              },
              "edge_media_to_comment": {
                "count": 3
              }
            }
          }
        ]
      }
    }
  }
}
```

---

## 4. Verification of `taken_at_timestamp` (P6-T4.2 Resolution)

**Finding**: `taken_at_timestamp` is confirmed **AVAILABLE** and **RELIABLE** on profile opens.
* When the user opens or hovers on a profile, Instagram's web app queries `/api/v1/users/web_profile_info/?username={username}`.
* The response contains `data.user.edge_owner_to_timeline_media.edges`, which delivers up to 12 recent timeline media nodes.
* Each node reliably includes `taken_at_timestamp` as a 10-digit Unix epoch integer (seconds).
* **Forensic Utility**:
  * Calculating `max(taken_at_timestamp) - min(taken_at_timestamp)` over the account's posts mathematically identifies Day-1 setup bursts ($\le 172,800$ seconds = 48 hours) with zero string-parsing fragility.

---

## 5. Architectural Resolutions for Future Phases

1. **Phase 7 (Learned Scorer & Feature Tiering)**:
   * **Tier 1 (List-Only Model)**: Features must be restricted to `username` forensics, `has_anonymous_profile_picture`, `is_private`, `is_verified`, and `full_name` length/entropy.
   * **Tier 2 (Full Profile Model)**: Adds `post_count`, `followers_count`, `following_count`, and `bio_length`.
   * **Tier 3 (Forensic Model)**: Adds `same_day_post_burst` from exact `taken_at_timestamp`.
2. **Phase 8 (Engagement Forensics B1)**:
   * Feasible passively: Instagram's post timeline payloads contain exact `edge_media_preview_like.count` and `edge_media_to_comment.count`.
3. **Phase 9 (Growth Velocity & Account Age B2)**:
   * Public creation date is **UNAVAILABLE** in the web API.
   * **Architectural Decision**: Phase 9 will use `user.pk` numeric sorting and reference calibration anchors (Instagram sequential user IDs) as an account-age cohort proxy.
4. **Phase 10 (Cross-Artist Bot Registry B4)**:
   * `username` and `pk` are guaranteed available across all tiers, enabling collision-free deduplication and cross-artist overlap indexing.

