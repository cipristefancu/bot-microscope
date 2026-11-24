# Bot Microscope (`bot-microscope`) — Complete User & Developer Guide (`v3.0.0`)

> **End-to-end guide to installing, using, auditing, and reading Bot Microscope.**  
> Everything runs locally on your machine with **zero shared third-party data** and strict adherence to **Rule `R-NOFAKE`** (no guessed or synthetic numbers).

---

## Table of Contents

1. [Core Philosophy & Privacy](#1-core-philosophy--privacy)
2. [Full Setup Guide (Fresh Clone -> Running App)](#2-full-setup-guide-fresh-clone---running-app)
3. [Browser Extension Workflow (`v3.0.0`)](#3-browser-extension-workflow-v300)
4. [Reading the Web Dashboard (`index.html`)](#4-reading-the-web-dashboard-indexhtml)
5. [Bot Connection Mindmap & Single-Account Full Map](#5-bot-connection-mindmap--single-account-full-map)
6. [The 12-Signal Checklist, 60p Ratio Cap & 3-Tier Math](#6-the-12-signal-checklist-60p-ratio-cap--3-tier-math)
7. [Local Data Storage & Offline CLI Tools](#7-local-data-storage--offline-cli-tools)
8. [Troubleshooting & FAQ](#8-troubleshooting--faq)

---

## 1. Core Philosophy & Privacy

* **You see every account yourself**: Open Instagram normally in Chrome, Edge, or Brave and scroll naturally.
* **Rule `R-NOFAKE` (Strict Observation)**: If a follower has not been opened (and has no conclusive passive hover card), their counts stay `null` and never enter the scored denominator.
* **Zero Remote Servers / Zero Data Leakage**: The server binds locally to `127.0.0.1:8000` (`app.py`), and all local audit CSVs, checkpoints, and label logs in `data/` are excluded via `.gitignore` so every user starts clean from scratch.
* **Bilingual (`EN · English` | `RO · Română`)**: Switch languages anytime across the extension HUD, toolbar popup, web dashboard, and exported HTML reports.

---

## 2. Full Setup Guide (Fresh Clone -> Running App)

### Step 1: Start the Local Python Web Server
From the repository folder, activate your virtual environment and launch `app.py`:

```bash
# Create & activate virtual environment
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS / Linux:      source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

The threaded WSGI server starts on **`http://127.0.0.1:8000`** (`http://localhost:8000`) along with its 4 background workers ([`src/botaudit/workers.py`](./src/botaudit/workers.py)):
1. **MemoryBankWorker**: Pre-warms confirmed Bots, Humans, and observed profile stats in RAM.
2. **GroundTruthSyncWorker**: Safely debounces and syncs `verified_bots.csv`, `verified_users.csv`, and `verified_benchmarks.csv` off HTTP request threads.
3. **AuditPrecomputeWorker**: Refreshes History breakdowns and active artist audits in the background.
4. **LivePulseTracker**: Tracks monotonic revisions and live activity events for zero-refresh dashboard updates.

### Step 2: Install the Chrome / Edge / Brave Extension
1. Open `chrome://extensions` (or `edge://extensions` / `brave://extensions`).
2. Turn **ON** **Developer mode** (top-right).
3. Click **Load unpacked** and select [`browser-extension/`](./browser-extension/).
4. Pin **Bot Microscope Collector** in your browser toolbar.

For deep architectural details and troubleshooting, see [**`browser-extension/README.md`**](./browser-extension/README.md).

### Step 3: Open the Dashboard
Visit [**`http://127.0.0.1:8000`**](http://127.0.0.1:8000). On a fresh clone with no personal checkpoints yet, the top status bar reads `Connected to Python Server` and automatically loads the bundled **Starter Bot-Farm Example (`@_vlad.dumitrache_`, `https://www.instagram.com/_vlad.dumitrache_/`)** from `data/examples/` so you can immediately explore real clue distributions, 3-tier charts, and the Profile Inspector.

---

## 3. Browser Extension Workflow (`v3.0.0`)

1. **Pick Your Target Account**: Navigate to an Instagram profile, open **Followers**, and verify the target badge at the top of the bottom-right **Bot Microscope HUD** (switch targets anytime via the HUD target picker or the popup's **`🎯 Audit @<account>`** button).
2. **Scroll & Tag**: As rows appear, Bot Microscope highlights known accounts (`🤖 CONFIRMED BOT`, `👤 REAL PERSON ✓`), pre-scores passive hovers (`<=34p` Clean, `>60p` Bot, or `CHECK`), and renders inline **`🤖` / `👤`** buttons directly on `UNSURE` follower rows for fast one-click confirmation.
3. **Inspect Open Tabs (`B` / `H` / `U` + `<220ms` Fast Close)**:
   * Middle-click (`Ctrl`+Click) followers to inspect them in new tabs (`botaudit_recent_click_v1` remembers which target account owns the click).
   * The **Bento Profile Card** shows `Posts / Followers / Following`, follow ratio (`e.g. 2,383×`), 3-photo strip, triggered clues, and the 100-point gauge.
   * Tap **`B`** (Bot), **`H`** (Human), or **`U`** (Unsure): the tab closes in **`90–220 ms`** while the MV3 Service Worker finishes saving both `/api/checkpoint` and `/api/labels` and syncs live counts across remaining tabs.
   * Closing a tab *without* pressing `B`/`H`/`U` still saves its observed numbers into your artist checkpoint.
4. **Rate-Limit Speedometer**: Monitors your hourly (`180/hr`) and 24-hour (`600/24h`) inspection rate (🟢 `<120/hr`, 🟡 `120–179/hr`, 🔴 `>=180/hr` or HTTP `429` cooldown).
5. **Send to Audit App**: Click **`🚀 Send to Audit App`** to focus (or open) `http://localhost:8000` with your newly checked target loaded.

---

## 4. Reading the Web Dashboard (`index.html`)

* **Step-by-Step Guide (EN/RO)**: Expandable 6-card guide explaining sampling (`±5.6%` at `300 / 120,000`), split handle forensics, the 3-tier partition, Gen-1 vs. Gen-2 bots, and empirical out-of-sample accuracy.
* **Live Engine Status Bar**: Shows worker pulse, global confirmed DB counts, latest event ticker, and `🔄 Sync Now`.
* **Audited Accounts History**: Interactive frosted-glass card per scanned account with checked ratio, 3-tier bar, confirmed count badges, and `✕` remove control.
* **Bot Connection Mindmap**: Interactive cross-account & single-account network explorer (see Section 5).
* **What the Numbers Say + Ground-Truth Banner + 4 KPI Cards**: Plain-English executive summary, manual confirmation accuracy vs. automated score, Wilson 95% confidence bounds, and Ranking Comparison Table.
* **Visual Comparison Chart & Clue Impact Cards**: Single-account 3-tier breakdown (`Clean / Suspicious / Bots`) + Industry Risk Gauge (0–10% Safe, 10–30% Mixed, 30%+ Farm Danger), multi-account comparison bars, and per-clue breakdown cards.
* **Every Checked Follower & Their Score (Profile Inspector)**: Searchable, sortable, paginated table with quick filter pills (`All Bots`, `Suspicious 35–60p`, `Clean <35p`, `Confirmed Bots`, `Dressed-Up Bots <=60p`, `Private Bots`, `Marked · Numbers Missing`, etc.) and inline `Bot` / `Real` buttons on unmarked rows.
* **Bottom Tools Tabs**: `1. Browser Extension & .HAR / .JSON` (with live Sampling Margin Calculator) · `2. Upload Real 9-Column CSV` · `3. Change Point Rules`.

---

## 5. Bot Connection Mindmap & Single-Account Full Map

The Mindmap reveals whether audited accounts share identical `@bot` handles or operate from disjoint bot pools:

* **Modes**:
  * **🕸️ Bot Web** *(default)*: Unified Left Accounts Rail → Bridge Junction Hubs (`2-Way`, `3-Way`, ...) → Bridge-Grouped Bot Matrix, plus Independent & Clean Control lanes below.
  * **🌳 Flow Tree**: Interactive hierarchical tree with expandable archetype branches.
  * **📋 Matrix & Directory**: Sortable, searchable table split into **Shared Bots** and **Exclusive Bots**, plus bridge summary cards.
* **Single-Account Full Bot Map**:
  * By default, clicking a connected account isolates its *shared bridges* — and displays an unmistakable amber CTA banner + card pill showing `+N exclusive bots hidden in Shared-Only view`.
  * Use the toolbar dropdown **`#mmAccountScopeSelect`**, the **`🗺️ All`** button on any Account Hub Card, or the amber CTA banner to open that account's **Left-to-Right Single-Account Full Map** showing *every* bot (`Shared 🔗` + `Exclusive 🔒`) grouped by archetype across `Bot Web`, `Flow Tree`, `Matrix & Directory`, Stage Fullscreen (`⛶`), and CSV/SVG/PNG export.
* **Handle-Only Reconciliation**: Confirmed bots that have not had their profile tab opened yet (`handle-only`) are explicitly noted (`+N🏷️`) so `checked_bots + handle_only == shared + exclusive` reconciles to the exact integer.

---

## 6. The 12-Signal Checklist, 60p Ratio Cap & 3-Tier Math

1. **Follow-Ratio Cluster (`RatioCluster = min(60, sum)`) — Two-Independent-Witnesses Rule**:
   * `mass_following` (`+25p`): `>1,500 & <50`, `>=1,500 & ratio >= 5×`, `>=200 & ratio >= 8×`, `>=50 & <=20 & ratio >= 10×`, `>=5K & >=3K & <=25 posts`, or `>=6.5K & <=20 posts`.
   * `extreme_mass_following` (`+30p`): `>=200 & ratio >= 15× & <=40 posts`, `>=100 & <=25 & ratio >= 12×`, or `>=500 & <=50 & <=40 posts`.
   * `low_audience_imbalance` (`+15p`): `<=25 posts & <=80 flwrs & >=150 flwing`, `<=25 posts & <=100 flwrs & bio <= 4`, or `<=25 posts & >=3,500 flwing & ratio >= 2.5×`.
   * `shared_post_bot_signal` (`+20p`): repost/low-engagement bot patterns without penalizing normal active creators.
   * **Capped at `60p` combined** so ratio clues alone stop at **Suspicious (`60p`)** and require a second independent signal to cross `>60p`.
2. **Independent Clues**:
   * **No Profile Photo** (`+20p`)
   * **Zero Posts** (`+25p`, plus `+15p` `blank_shell_bonus` when combined with `mass_following` & `following >= 1,000`) **or** **1–6 Same-Day Setup Burst** (`+15p`)
   * **Follow-Exchange Ring** (`5K+/5K+ & <=25 posts`: `+20p`)
   * **Empty / Minimal Bio** (`bio_length <= 4`: `+10p`)
   * **Split Handle Forensics**: `digit_heavy_username` (`>40%` digits, `word.digits`, or `5+` consecutive digits: `+20p`) **or** mutually exclusive `stitched_username` (non-digit stitched punctuation: `+10p`)
   * **Known Bot Registry** (`+25p`)
3. **3-Tier Partition**:
   * **🔴 Bot**: Confirmed Bot manually **or** automated score **`61–100p`** (`>60p`, unless confirmed Human).
   * **🟡 Suspicious**: Unlabeled complete profile scoring **`35–60p`**.
   * **🟢 Clean**: Unlabeled conclusive profile scoring **`0–34p`** **or** Confirmed Human (human confirmation always overrides auto-scores and conflict resolution favors Human).
4. **Uncertainty Bounds**: Every audience percentage uses the **95% Wilson score confidence interval**, strictly bounded within `[0%, 100%]`.

---

## 7. Local Data Storage & Offline CLI Tools

All local working data lives under `data/` (personal checkpoints, backups, and manual label CSVs are gitignored, while `data/template.csv` and `data/examples/` ship with the repo):
* `data/examples/_vlad.dumitrache_.json` & `_vlad.dumitrache_.csv` — Bundled real bot-farm starter example (`https://www.instagram.com/_vlad.dumitrache_/` · `1,279` captured / `1,194` scored, pure automated baseline without pre-baked manual overrides).
* `data/checkpoints/<artist>.json` — Per-account resume checkpoints and observed follower metrics.
* `data/labels/label_notes.csv` — Timestamped manual label log (`username`, `label`, `bot_type`, `labeler`, `labeled_on`, `evidence_note`, `status`).
* `data/verified_bots.csv` & `data/verified_users.csv` — Canonical ground-truth handle lists (auto-created cleanly on fresh clones).
* `data/sample/verified_benchmarks.csv` — Auto-generated empirical benchmark dataset built solely from your locally confirmed labels that have real observed checkpoint numbers.

**CLI Tools:**
```bash
# Start interactive dashboard server
python app.py --host 127.0.0.1 --port 8000

# Offline CSV -> standalone HTML report (EN or RO) using the bundled bot-farm example
python run_audit.py --input data/examples/_vlad.dumitrache_.csv --output output/example_report.html --lang en

# Measure rule precision/specificity/sensitivity on your local labels
python run_evaluation.py --split test
```

---

## 8. Troubleshooting & FAQ

* **Server offline banner / extension cannot connect**: Ensure `python app.py` is running on port `8000`, then reload Instagram or click **Sync Now** on `index.html`.
* **Fresh clone starts with empty counts**: Expected! No third-party handles or synthetic presets are bundled. Open Instagram with the extension loaded or upload a 9-column CSV (`data/template.csv` format) to begin.
* **Accidental Bot / Real click**: Re-label directly inside `index.html` (**Every Checked Follower & Their Score**) or in the Followers modal / profile tab; `verified_bots.csv`, `verified_users.csv`, and `label_notes.csv` update automatically.

---

Made with love by [freestate.ro](https://freestate.ro)
