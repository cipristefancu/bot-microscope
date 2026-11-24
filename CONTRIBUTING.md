# Contributing to Bot Microscope (`bot-microscope`)

Thank you for helping improve **Bot Microscope**! Everything here is built around honest, verifiable audience forensics.

## Core Principles

1. **Rule `R-NOFAKE` (Never Fabricate Numbers)**: Any unobserved profile count (`post_count`, `followers_count`, `following_count`, `bio_length`) must remain `null` / unobserved — never guessed or padded with defaults.
2. **Zero Scraping Libraries**: `tests/test_guardrails.py` strictly forbids headless browser/HTTP client packages (`selenium`, `playwright`, `requests`, `httpx`, `instaloader`, etc.) inside `src/botaudit/` and `requirements.txt`.
3. **Zero Third-Party Handle Leaks**: Personal checkpoints, `verified_bots.csv`, `verified_users.csv`, and private notes in `data/` are gitignored and must never be committed. Use synthetic test handles (`target_alpha`, `organic_human`, etc.) in unit tests.
4. **Bilingual Parity (`EN | RO`)**: Any new UI control or label in `index.html` or `browser-extension/` should support both English and Romanian cleanly.

## Local Development Setup

```bash
git clone https://github.com/cipristefancu/bot-microscope.git
cd bot-microscope
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS / Linux:      source .venv/bin/activate
pip install -r requirements.txt
```

## Pre-PR Verification Checklist

Run all checks locally before opening a pull request:

```bash
# 1. Lint
ruff check .

# 2. Full Python & JS contract suite
pytest -q

# 3. Standalone browser extension checks
node --check browser-extension/parser.js
node --check browser-extension/content.js
node --check browser-extension/background.js
node --check browser-extension/popup.js
node --check browser-extension/injected-interceptor.js
node tests/js/parser_contract.js >/dev/null
node tests/js/hud_inspector_contract.js >/dev/null
node tests/js/background_contract.js >/dev/null

# 4. Offline CLI smoke test on bundled example
python run_audit.py --input data/examples/_vlad.dumitrache_.csv --output output/check.html --lang en
```

After editing `browser-extension/` files, reload the unpacked extension in `chrome://extensions` and refresh Instagram (`F5`) to test live.

---

Made with love by [freestate.ro](https://freestate.ro)
