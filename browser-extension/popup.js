/**
 * Bot Microscope v3.0.0 — Popup Controller (Bilingual EN | RO + Live Inspector Preview)
 */
(function () {
  "use strict";
  let currentLang = "en";

  try {
    if (typeof chrome !== "undefined" && chrome.runtime && typeof chrome.runtime.getManifest === "function") {
      const manifest = chrome.runtime.getManifest();
      if (manifest && manifest.version) {
        const verEl = document.getElementById("ext-ver");
        if (verEl) verEl.textContent = `v${manifest.version}`;
      }
    }
  } catch (_e) {}

  function fmtNum(v) {
    return typeof v === "number" ? v.toLocaleString() : "—";
  }

  function sendToActiveTab(message, callback) {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs && tabs[0];
      if (!tab || !tab.id) return;
      chrome.tabs.sendMessage(tab.id, message, (resp) => {
        if (chrome.runtime.lastError) {
          document.getElementById("status-note").textContent =
            currentLang === "ro"
              ? "Deschide o pagină Instagram (instagram.com) și reîncarcă fila."
              : "Open an Instagram tab (instagram.com) and refresh the page.";
          return;
        }
        if (callback) callback(resp);
      });
    });
  }

  function applyLang() {
    const isRo = currentLang === "ro";
    document.getElementById("btn-lang").textContent = isRo ? "RO·EN" : "EN·RO";
    document.getElementById("lbl-total").textContent = isRo ? "Salvate" : "Saved";
    document.getElementById("lbl-bots").textContent = isRo ? "Boți" : "Bots";
    document.getElementById("lbl-humans").textContent = isRo ? "Oameni" : "Humans";
    document.getElementById("lbl-burst").textContent = isRo ? "1-6/Zi" : "1-6 Burst";
    document.getElementById("lbl-priv").textContent = isRo ? "🔒 Privați" : "🔒 Priv";
    document.getElementById("btn-fast").textContent = isRo ? "⚡ Continuă +50" : "⚡ Continue +50";
    document.getElementById("btn-deep").textContent = isRo ? "🔬 Continuă +200" : "🔬 Continue +200";
    document.getElementById("btn-slice").textContent = isRo
      ? "🔤 Salt la Următoarea Literă în Search (a-z)"
      : "🔤 Jump to Next Unscanned Search Letter (a-z)";
    document.getElementById("btn-send").textContent = isRo
      ? "🚀 Trimite Tot în Audit App (8000)"
      : "🚀 Send All Saved to Audit App (8000)";
    document.getElementById("btn-csv").textContent = isRo ? "📥 Descarcă CSV" : "📥 Download CSV";
    document.getElementById("btn-clear").textContent = isRo ? "🗑️ Resetează" : "🗑️ Reset Artist";
  }

  function refreshStatus() {
    sendToActiveTab({ action: "getStatus" }, (res) => {
      if (!res) return;
      const isRo = currentLang === "ro";
      const checked = Number(res.checkedCount || 0);
      const bots = Number(res.botsCount || 0);
      const suspicious = Number(res.suspiciousCount || 0);
      const humans =
        res.humansCount !== undefined
          ? Number(res.humansCount)
          : Math.max(0, checked - bots - suspicious);
      const botPct = checked > 0 ? ((bots / checked) * 100).toFixed(1) : "-";
      const suspiciousPct = checked > 0 ? ((suspicious / checked) * 100).toFixed(1) : "-";
      const humanPct = checked > 0 ? ((humans / checked) * 100).toFixed(1) : "-";
      document.getElementById("ui-title").textContent = `Bot Microscope · @${res.artist || "instagram"}`;
      const targetSel = document.getElementById("target-select");
      const auditCurBtn = document.getElementById("btn-audit-current");
      if (targetSel) {
        const list = Array.isArray(res.knownAccounts) ? res.knownAccounts.slice() : [];
        if (res.artist && !list.some((x) => String(x.artist || "").toLowerCase() === res.artist.toLowerCase())) {
          list.unshift({ artist: res.artist, checked: checked, saved: Number(res.count || 0) });
        }
        targetSel.innerHTML = "";
        list.forEach((acc) => {
          const opt = document.createElement("option");
          opt.value = acc.artist;
          const cntLabel = acc.checked > 0 ? ` (${acc.checked})` : acc.saved > 0 ? ` (${acc.saved})` : "";
          opt.textContent = `@${acc.artist}${cntLabel}`;
          if (res.artist && String(acc.artist).toLowerCase() === res.artist.toLowerCase()) {
            opt.selected = true;
          }
          targetSel.appendChild(opt);
        });
      }
      if (auditCurBtn) {
        const ph = String(res.pageHandle || "").trim();
        if (ph && res.artist && ph.toLowerCase() !== res.artist.toLowerCase()) {
          auditCurBtn.style.display = "";
          auditCurBtn.setAttribute("data-artist", ph);
          auditCurBtn.textContent = isRo ? `🎯 Auditează @${ph}` : `🎯 Audit @${ph}`;
        } else {
          auditCurBtn.style.display = "none";
          auditCurBtn.removeAttribute("data-artist");
        }
      }
      document.getElementById("val-total").textContent = `${res.count || 0} (+${res.newThisSession || 0})`;
      document.getElementById("val-bots").textContent = `${bots} (${botPct}%)`;
      document.getElementById("val-humans").textContent = `${humans} (${humanPct}%)`;
      const suspRow = document.getElementById("suspicious-row");
      if (suspRow) {
        suspRow.style.display = suspicious > 0 ? "flex" : "none";
        const suspLbl = document.getElementById("lbl-suspicious");
        const suspVal = document.getElementById("val-suspicious");
        if (suspLbl) {
          suspLbl.textContent = isRo ? "🟡 Suspecte 35–60p (verifică)" : "🟡 Suspicious 35–60p (check)";
        }
        if (suspVal) {
          suspVal.textContent = `${suspicious} (${suspiciousPct}%)`;
        }
      }
      document.getElementById("lbl-bots").textContent = isRo ? `Boți / ${checked}` : `Bots / ${checked}`;
      document.getElementById("lbl-humans").textContent = isRo
        ? `Oameni / ${checked}`
        : `Humans / ${checked}`;
      const confBots = Number(res.confirmedBotsCount || 0);
      const autoBots = Number(res.autoBotsCount ?? Math.max(0, bots - confBots));
      const confHumans = Number(res.confirmedHumansCount || 0);
      const autoHumans = Number(res.autoHumansCount ?? Math.max(0, humans - confHumans));
      const subBotsEl = document.getElementById("sub-bots");
      if (subBotsEl) {
        subBotsEl.textContent = isRo
          ? `✓ ${confBots} confirmați · ${autoBots} auto`
          : `✓ ${confBots} confirmed · ${autoBots} auto`;
      }
      const subHumansEl = document.getElementById("sub-humans");
      if (subHumansEl) {
        subHumansEl.textContent = isRo
          ? `✓ ${confHumans} confirmați · ${autoHumans} auto`
          : `✓ ${confHumans} confirmed · ${autoHumans} auto`;
      }
      document.getElementById("val-burst").textContent = String(res.burstCount || 0);
      document.getElementById("val-priv").textContent = String(res.privBotsCount || 0);
      ["btn-fast", "btn-deep", "btn-slice"].forEach((id) => {
        document.getElementById(id).style.display = res.automationEnabled === true ? "" : "none";
      });

      const miniBox = document.getElementById("inspect-mini");
      const ip = res.inspectedProfile;
      if (ip && ip.username && ip.username !== res.artist) {
        miniBox.style.display = "block";
        document.getElementById("inspect-mini-handle").textContent =
          `@${ip.username}${ip.account_private ? " 🔒" : ""}`;
        const pts = typeof ip.score_pts === "number" ? ip.score_pts : 0;
        const lbl = String(ip.manual_label || "").toLowerCase();
        const scoreEl = document.getElementById("inspect-mini-score");
        if (lbl === "bot") {
          scoreEl.textContent = `🤖 BOT · ${pts}p`;
          scoreEl.style.color = "#fda4af";
        } else if (lbl === "human") {
          scoreEl.textContent = `👤 HUMAN ✓ · ${pts}p`;
          scoreEl.style.color = "#6ee7b7";
        } else if (ip.bot_like === true) {
          scoreEl.textContent = `🔴 BOT · ${pts}p`;
          scoreEl.style.color = "#fda4af";
        } else if (ip.bot_like === null || pts >= 35) {
          scoreEl.textContent = `🟡 CHECK · ${pts}p`;
          scoreEl.style.color = "#fcd34d";
        } else {
          scoreEl.textContent = `🟢 REAL · ${pts}p`;
          scoreEl.style.color = "#6ee7b7";
        }
        document.getElementById("inspect-mini-posts").textContent = fmtNum(ip.post_count);
        document.getElementById("inspect-mini-posts-lbl").textContent =
          ip.post_count === 1 ? (isRo ? "Postare" : "Post") : isRo ? "Postări" : "Posts";
        document.getElementById("inspect-mini-flwrs").textContent = fmtNum(ip.followers_count);
        document.getElementById("inspect-mini-flwrs-lbl").textContent =
          ip.followers_count === 1
            ? isRo
              ? "Urmăritor"
              : "Follower"
            : isRo
              ? "Urmăritori"
              : "Followers";
        document.getElementById("inspect-mini-flwing").textContent = fmtNum(ip.following_count);
        document.getElementById("inspect-mini-flwing-lbl").textContent = isRo
          ? "Urmăriri"
          : "Following";
      } else {
        miniBox.style.display = "none";
      }

      if (res.lastUsername) {
        const pct = Math.min(100, Math.round(((res.checkedCount || 0) / 383) * 100));
        document.getElementById("cp-line").textContent = isRo
          ? `☑️ Verificate: ${res.checkedCount || 0}/${res.count || 0} (${pct}% țintă ±5%) · Ultimul: @${res.lastUsername}`
          : `☑️ Checked: ${res.checkedCount || 0}/${res.count || 0} (${pct}% of ±5% target) · Last: @${res.lastUsername}`;
      }
      if (res.statusMessage && res.statusMessage !== "Ready — Previously saved accounts are skipped automatically") {
        document.getElementById("status-note").textContent = res.statusMessage;
      }
      if (res.rateLimit) {
        const rl = res.rateLimit;
        const speedBox = document.getElementById("speed-box");
        speedBox.classList.remove("zone-yellow", "zone-red");
        if (rl.zone === "red") speedBox.classList.add("zone-red");
        else if (rl.zone === "yellow") speedBox.classList.add("zone-yellow");

        const icon = rl.zone === "red" ? "🔴" : rl.zone === "yellow" ? "🟡" : "🟢";
        document.getElementById("speed-headline").textContent = `${icon} ${isRo ? rl.labelRo : rl.labelEn}`;

        document.getElementById("hr-lbl").textContent = `1h: ${rl.checksLastHour}/180`;
        document.getElementById("hr-rem").textContent = `+${rl.remHourSafe}`;
        const hrFill = document.getElementById("hr-fill");
        hrFill.style.width = `${Math.min(100, Math.round((rl.checksLastHour / 180) * 100))}%`;
        hrFill.style.background =
          rl.checksLastHour >= 180 ? "#fb7185" : rl.checksLastHour >= 120 ? "#fbbf24" : "#34d399";

        document.getElementById("day-lbl").textContent = `24h: ${rl.checksLastDay}/600`;
        document.getElementById("day-rem").textContent = `+${rl.remDaySafe}`;
        const dayFill = document.getElementById("day-fill");
        dayFill.style.width = `${Math.min(100, Math.round((rl.checksLastDay / 600) * 100))}%`;
        dayFill.style.background =
          rl.checksLastDay >= 600 ? "#fb7185" : rl.checksLastDay >= 450 ? "#fbbf24" : "#60a5fa";

        document.getElementById("next-scan-pill").textContent = isRo ? rl.nextScanRo : rl.nextScanEn;
      }
    });
  }

  document.getElementById("btn-lang").addEventListener("click", () => {
    currentLang = currentLang === "en" ? "ro" : "en";
    applyLang();
    refreshStatus();
  });

  document.getElementById("btn-fast").addEventListener("click", () => {
    sendToActiveTab({ action: "startFast" }, refreshStatus);
  });

  document.getElementById("btn-deep").addEventListener("click", () => {
    sendToActiveTab({ action: "startDeep" }, refreshStatus);
  });

  document.getElementById("btn-slice").addEventListener("click", () => {
    sendToActiveTab({ action: "nextSlice" }, refreshStatus);
  });

  document.getElementById("btn-send").addEventListener("click", () => {
    sendToActiveTab({ action: "sendToApp" }, refreshStatus);
  });

  document.getElementById("btn-csv").addEventListener("click", () => {
    sendToActiveTab({ action: "downloadCsv" }, refreshStatus);
  });

  document.getElementById("btn-clear").addEventListener("click", () => {
    const msg =
      currentLang === "ro"
        ? "Resetezi datele locale colectate pentru acest cont?"
        : "Reset collected profile data for this target account?";
    if (!window.confirm(msg)) return;
    sendToActiveTab({ action: "clearData" }, refreshStatus);
  });

  const targetSelEl = document.getElementById("target-select");
  if (targetSelEl) {
    targetSelEl.addEventListener("change", () => {
      if (targetSelEl.value) {
        sendToActiveTab({ action: "setTargetArtist", artist: targetSelEl.value }, refreshStatus);
      }
    });
  }

  const auditCurBtnEl = document.getElementById("btn-audit-current");
  if (auditCurBtnEl) {
    auditCurBtnEl.addEventListener("click", () => {
      const ph = auditCurBtnEl.getAttribute("data-artist");
      if (ph) {
        sendToActiveTab({ action: "setTargetArtist", artist: ph }, refreshStatus);
      }
    });
  }

  applyLang();
  refreshStatus();
})();
