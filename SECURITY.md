# Security Policy — Bot Microscope (`bot-microscope`)

## Local-First Security Architecture

Bot Microscope is designed to run **100% locally** on your machine without third-party scraping servers or remote telemetry:

1. **Loopback Binding (`127.0.0.1:8000`)**: The Python WSGI server (`app.py`) binds to `127.0.0.1` by default and never exposes a public internet endpoint.
2. **Restricted Origin / CORS Policy**: Local API endpoints (`src/botaudit/web.py`) reflect CORS headers **only** for `http://127.0.0.1:*`, `http://localhost:*`, `chrome-extension://*`, and `moz-extension://*` origins. Untrusted cross-origin `OPTIONS`, `POST`, and `DELETE` requests from unrelated websites are rejected with `403 Forbidden`.
3. **Bounded Payload Sizes**: JSON request bodies are capped at `25 MB` (`413 Payload Too Large`), preventing memory-exhaustion attacks from malformed requests.
4. **Minimal Extension Permissions**: `browser-extension/manifest.json` requests only `storage` and `activeTab` permissions, with host access strictly scoped to `https://*.instagram.com/*`, `http://localhost:8000/*`, and `http://127.0.0.1:8000/*`.
5. **No Remote Scraping Libraries**: `tests/test_guardrails.py` enforces zero banned network/browser-automation libraries (`selenium`, `playwright`, `requests`, `instaloader`, etc.) in `src/botaudit/` and `requirements.txt`.

## Reporting a Vulnerability

If you discover a security or privacy issue (such as cross-origin bypass, HTML/XSS injection in `index.html`, or accidental credential/handle leakage):

* **Do not** open a public issue with exploit details or private handles.
* Open a **Private Security Advisory** on GitHub (`Security -> Advisories -> Report a vulnerability`) or contact the maintainer via [freestate.ro](https://freestate.ro).
* Include clear reproduction steps, affected file paths, and impact assessment. Confirmed issues will be patched promptly on `main`.
