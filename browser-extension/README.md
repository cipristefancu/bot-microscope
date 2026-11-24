# Bot Microscope Browser Extension (`v3.0.0`)

> **Passive, read-only Chrome / Edge / Brave Manifest V3 extension** for inspecting Instagram follower audiences in your own browser session — with zero fabricated numbers (`Rule R-NOFAKE`), bilingual **EN | RO** UI, inline follower-list labeling, and real-time `<220ms` persistence to the local Bot Microscope dashboard (`http://127.0.0.1:8000`).

---

## 1. Architecture & Privacy Model

The extension **never runs headless scrapers**, **never initiates unauthorized background Instagram API requests**, and **never sends data outside your machine**. Every permission in `manifest.json` is strictly scoped:

* **`permissions`**: `["storage", "activeTab"]` only.
* **`host_permissions`**: `https://*.instagram.com/*`, `http://localhost:8000/*`, `http://127.0.0.1:8000/*`.
* **Local-only storage**: Working state is persisted locally in browser storage (`chrome.storage` / `localStorage`) and synced over localhost to the Python WSGI backend.

### Data Flow

```text
Instagram Web Tab (MAIN World)
  └─ injected-interceptor.js   (Passively clones fetch / XHR responses for follower lists & profiles; detects HTTP 429)
        │ CustomEvent: BotAuditInterceptedData / BotAuditHttp429
        ▼
Content Script (ISOLATED World)
  ├─ parser.js                 (Rule R-NOFAKE parser, 12-signal tri-state scorer, handle forensics, CSV exporter)
  └─ content.js + content.css  (Floating Glass HUD, Followers Modal badges, Passive Hover badges, Bento Profile Card, B/H/U shortcuts)
        │ chrome.runtime.sendMessage + BroadcastChannel + storage events
        ▼
MV3 Service Worker (background.js)
  ├─ Fast Tab Close (<220ms cap, 90ms glow window) while completing POST /api/checkpoint + POST /api/labels
  └─ Broadcasts BOTAUDIT_LIVE_DELTA + BOTAUDIT_TABS_COUNT back to all open Instagram tabs
        │ HTTP POST/GET (localhost:8000 / 127.0.0.1:8000)
        ▼
Local Python 4-Worker Engine (app.py -> index.html)
```

---

## 2. File-by-File Reference

| File | Role |
| :--- | :--- |
| `manifest.json` | Manifest V3 declaration (`v3.0.0`), permissions (`storage`, `activeTab`), host permissions, and MAIN-world resource injection. |
| `injected-interceptor.js` | Lightweight MAIN-world wrapper around `window.fetch` and `XMLHttpRequest.prototype.send`. Ignores `/following` endpoints so following lists never pollute follower audits; forwards relevant JSON payloads and HTTP `429` alerts via `CustomEvent`. |
| `parser.js` | Core deterministic scoring & extraction engine (`SCHEMA_VERSION = 3`). Enforces `Rule R-NOFAKE` (`null` for unobserved fields), strips legacy placeholder tuples, computes `stitched_username`, `digit_heavy_username`, `same_day_post_burst`, and 4 follow-ratio cluster rules with the **60p ratio-cluster cap**. |
| `content.js` | Manages the floating bottom-right HUD, **Target Account Switcher**, Followers Modal badge injection, inline `🤖`/`👤`/`?` quick-confirm buttons, passive hover-card pre-scoring, Bento Profile Inspector, `B`/`H`/`U` keyboard shortcuts, and the **180/hr & 600/24h Rate-Limit Speedometer**. |
| `content.css` | Dark glassmorphic styles for the floating dock, modal badges, inline confirm buttons, ratio pills, and 100-point gauge. |
| `background.js` | MV3 Service Worker relaying API calls past Instagram CSP/Mixed-Content restrictions, managing in-memory cache invalidation, keeping open follower-tab counts updated, focusing or opening `http://localhost:8000`, and running non-blocking fast tab-close persistence (`MARK_PROFILE_AND_CLOSE`). |
| `popup.html` & `popup.js` | Toolbar popup showing live audited target KPIs (`Saved`, `Bots`, `Suspicious 35–60p`, `Humans`, `1-6 Burst`, `Priv`), Target Account Switcher (`#target-select` + `🎯 Audit @current`), active profile mini-preview, Rate-Limit Speedometer bars, `EN·RO` toggle, and quick export/sync controls. |

---

## 3. Installation, Pinning & Updating

Works on **Google Chrome**, **Microsoft Edge**, and **Brave** (any Chromium Manifest V3 browser):

1. Open `chrome://extensions` (or `edge://extensions` / `brave://extensions`).
2. Toggle **Developer mode** to **ON** (top-right corner).
3. Click **Load unpacked** (top-left) and select the `browser-extension/` directory.
4. Click the browser puzzle-piece icon and **pin** **Bot Microscope Collector** to your toolbar.

> **Updating after code changes:** Open `chrome://extensions`, click **Reload** (`↻`) on the Bot Microscope card, then refresh your Instagram tab (`F5`) so the floating dock displays the active `v3.0.0` pill.
>
> **Sharing with others:** Zip the `browser-extension/` folder and send it — no build step, API keys, or store account required.

---

## 4. Complete Auditor Workflow

### A. Choosing or Switching the Target Account
Bot Microscope tracks *whose* audience is being audited separately from whichever follower profile tab you open:
* **Click-Lock Memory**: Middle-clicking (`Ctrl`+Click) a follower inside `@target_a`'s Followers modal automatically locks newly opened tabs to `@target_a`.
* **Manual Target Switcher**: If you navigate to a new account (e.g., `@freestate.ai`) and want to audit its audience instead, click the **`@target` pill** in the floating HUD header (or click **`Switch to @<page>`** / use the toolbar popup's **`🎯 Audit @<page>`** button or dropdown).
* **Cross-Account Safety**: Profile tabs belonging to another target account never hijack the currently selected target.

### B. Scrolling the Followers Modal & Inline Quick-Confirm
1. Open an account's **Followers** dialog and scroll at natural human pace.
2. As Instagram loads rows, `content.js` decorates each row in real time:
   * **`🤖 CONFIRMED BOT`** (rose) or **`👤 REAL PERSON ✓`** (emerald) if already known in your local Memory Bank.
   * **Passive Hover Pre-Scoring**: When you hover over a username and Instagram fetches hover card metrics (`posts`, `followers`, `following`, photo/private status), `parser.js` computes:
     * **Auto-Bot** if known signals already exceed `60p`.
     * **Clean** only if worst-case missing clues (`maxScorePts`) stay **`≤ 34p`**.
     * **Unsure / `CHECK`** otherwise — never guessing missing bio/burst signals.
   * **Inline One-Click Confirm Buttons on `UNSURE` Rows**: Followers marked `UNSURE` display inline **`🤖`** (Mark Bot) and **`👤`** (Mark Human) action pills right in the Followers modal row so you can confirm obvious accounts immediately without opening a new tab.

### C. Inspecting a Profile Tab (`B` / `H` / `U` Shortcuts & Fast Close)
1. Middle-click (`Ctrl`+Click) a follower to open their profile in a new tab.
2. The **Profile Inspection Card** appears on the right:
   * **Bento Tiles**: Posts, Followers, Following (`1 Post` singular vs `Posts`).
   * **Ratio & Clue Badges**: Follow ratio (`e.g. 2,383× ratio`), profile photo status, bio character count, stitched/digit username flags, and 3-photo filmstrip preview.
   * **100-Point Gauge**: Exact checklist score with the `60p` cutoff marker and `35–60p` Suspicious zone indicator.
3. **Instant Keyboard Confirmation**:
   * Press **`B`** → Confirm **Bot**
   * Press **`H`** → Confirm **Human (Real Person)**
   * Press **`U`** → Mark **Unsure**
   *(Shortcuts automatically ignore keystrokes while typing inside search/input/textarea fields.)*
4. **How `<220ms` Fast Tab Close Works**:
   * `content.js` immediately updates `localStorage` and broadcasts the delta across open tabs.
   * `background.js` keeps a brief **`90 ms`** visual feedback glow (and guarantees closure within **`220 ms`** max even if the local Python backend is busy) while completing both `POST /api/checkpoint` (`skip_live_counts: true`, `defer_workers: true`) and `POST /api/labels` inside the Service Worker and broadcasting updated `liveCounts` (`BOTAUDIT_LIVE_DELTA`) back to remaining tabs.
   * Closing an inspected tab **without** pressing `B`/`H`/`U` still auto-saves its observed numbers to your artist checkpoint.

### D. Rate-Limit Safety Speedometer
Built directly into the HUD and popup (`1-hour` sliding window cap `180/hr`, `24-hour` window cap `600/24h`):

| Zone | Hourly Checks | Behavior |
| :--- | :--- | :--- |
| 🟢 **Safe Zone** | `0 – 119 / hr` | Normal operation. |
| 🟡 **Caution Zone** | `120 – 179 / hr` | Warning indicator + gentle cooldown guidance. |
| 🔴 **Cooldown Active** | `180+ / hr` or HTTP `429` | Automatic pause countdown to protect your Instagram session. |

### E. Sending Data & Exporting
* **`🚀 Send to Audit App`**: Syncs observed profiles and focuses (or opens) `http://localhost:8000/?source=extension&artist=<target>` without creating duplicate dashboard tabs.
* **`📥 CSV`**: Exports a strict 9-column CSV formatted for offline sharing or `run_audit.py` CLI reporting.

---

## 5. Scoring Rules Implemented in `parser.js`

Every rule in `browser-extension/parser.js` mirrors `src/botaudit/web.py`:

* **Two-Independent-Witnesses Ratio-Cluster Cap (`max 60p`)**:
  Follow-ratio signals (`mass_following +25p`, `extreme_mass_following +30p`, `low_audience_imbalance +15p`, `shared_post_bot_signal +20p`) cap together at **60 points** — so follow ratios alone never cross the `>60p` Bot threshold without at least one independent clue.
* **Independent Clues**:
  * `no_photo`: `+20p`
  * `zero_posts`: `+25p` (+ `+15p` `blank_shell_bonus` when `zero_posts` + `mass_following` + `following >= 1,000`) **or** `same_day_post_burst` (`1–6` posts within 48h or setup signature): `+15p`
  * `follow_exchange_ring` (`>=5,000` following & `>=5,000` followers & `<=25` posts): `+20p`
  * `empty_bio` (`bio_length <= 4`): `+10p`
  * `digit_heavy_username` (`>40%` digits, `word.12345`, or `5+` consecutive digits): `+20p` **or** `stitched_username` (non-digit stitched punctuation pattern): `+10p`
  * `known_registry_bot`: `+25p`
* **3-Tier Verdict**: **`0–34p` Clean** · **`35–60p` Suspicious** · **`61–100p` (or Confirmed Bot) Bot** — and manual labels always override automated scores (`human` precedence rule).

---

## 6. Developer Verification & Troubleshooting

Validate extension syntax and headless Node contracts anytime from the repo root:

```bash
node --check browser-extension/parser.js
node --check browser-extension/content.js
node --check browser-extension/background.js
node tests/js/hud_inspector_contract.js
node tests/js/background_contract.js
```

Or run the full integration suite:

```bash
pytest tests/test_extension_hud_ux.py tests/test_extension_parser.py -q
```

**Common fixes:**
* **"Local server (localhost:8000) offline"**: Run `python app.py` in the repository root and refresh Instagram.
* **Badges missing after an extension update**: Click **Reload (`↻`)** in `chrome://extensions` and press `F5` on Instagram.
* **Stuck on previous target account**: Click the `@target` badge in the top bar of the floating HUD or click **`🎯 Audit @<account>`** inside the toolbar popup.

---

Made with love by [freestate.ro](https://freestate.ro)
