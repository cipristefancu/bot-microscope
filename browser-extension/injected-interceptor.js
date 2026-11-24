/**
 * Passive Page-Context Network Listener (MAIN World)
 * Clones Instagram JSON responses for follower lists and profile timeline metadata
 * without initiating any unrequested API calls. Also detects HTTP 429 rate limits.
 */
(function () {
  "use strict";
  if (window.__BOT_AUDIT_INTERCEPTOR_ACTIVE__) return;
  window.__BOT_AUDIT_INTERCEPTOR_ACTIVE__ = true;

  const RELEVANT_URL_PATTERNS = [
    "/api/v1/friendships/",
    "/api/v1/users/web_profile_info/",
    "/api/v1/feed/user/",
    "/graphql/query",
    "/api/graphql"
  ];

  function isRelevantUrl(url) {
    if (!url || typeof url !== "string") return false;
    // Never capture "Following" lists; we only audit an account's Followers list.
    if (/\/api\/v1\/friendships\/[^/]+\/following\b/i.test(url)) return false;
    return RELEVANT_URL_PATTERNS.some((pat) => url.indexOf(pat) !== -1);
  }

  function dispatchPayload(url, data) {
    try {
      window.dispatchEvent(
        new CustomEvent("BotAuditInterceptedData", {
          detail: { url: String(url || ""), payload: data, timestamp: Date.now() }
        })
      );
    } catch (_err) {
      // Ignore serialization issues
    }
  }

  function dispatchRateLimit(url, status) {
    try {
      window.dispatchEvent(
        new CustomEvent("BotAuditHttp429", {
          detail: { url: String(url || ""), status: status, timestamp: Date.now() }
        })
      );
    } catch (_err) {
      // Ignore
    }
  }

  // 1. Wrap window.fetch passively
  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async function (...args) {
      const response = await originalFetch.apply(this, args);
      try {
        const reqUrl = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
        if (response.status === 429) {
          dispatchRateLimit(reqUrl, 429);
        } else if (isRelevantUrl(reqUrl)) {
          const clone = response.clone();
          clone
            .json()
            .then((jsonData) => dispatchPayload(reqUrl, jsonData))
            .catch(() => {});
        }
      } catch (_e) {
        // Never break host page fetch
      }
      return response;
    };
  }

  // 2. Wrap XMLHttpRequest passively
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__botAuditUrl = typeof url === "string" ? url : String(url || "");
    return origOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", function () {
      try {
        const url = this.__botAuditUrl || "";
        if (this.status === 429) {
          dispatchRateLimit(url, 429);
          return;
        }
        if (isRelevantUrl(url) && this.responseText && this.responseText[0] === "{") {
          const parsed = JSON.parse(this.responseText);
          dispatchPayload(url, parsed);
        }
      } catch (_e) {
        // Ignore non-JSON XHR
      }
    });
    return origSend.apply(this, args);
  };
})();

