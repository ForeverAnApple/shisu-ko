"use strict";

/*
 * Background (event page). Jobs:
 *  1. Proxy API calls from content scripts to the local Whisper server.
 *  2. Own the settings object in browser.storage.local.
 *  3. Sentence mining: fetch the audio clip for a cue from the server and attach it, together
 *     with the screenshot taken by the content script, to an Anki card via AnkiConnect, or save
 *     both to the Downloads folder.
 *  4. Watch AnkiConnect for a note Yomitan has just added, so the content script can attach the
 *     material without the viewer pressing anything.
 */

const DEFAULT_SETTINGS = SHISUKO_DEFAULT_SETTINGS; // from settings.js

const REQUEST_TIMEOUT_MS = 10000;

// Auto-mining watcher: poll AnkiConnect for a note Yomitan has just created.
const ANKI_POLL_THROTTLE_MS = 250;   // several tabs may poll; one request per interval is enough
const ANKI_PERMISSION_RECHECK_MS = 60000;
const ANKI_POLL_TIMEOUT_MS = 5000;      // a hung poll would otherwise block the watcher for good
const ANKI_BASELINE_MAX_AGE_MS = 10000; // a gap this long means the baseline can no longer be trusted

const ankiWatch = { baseline: null, lastPollAt: 0, lastOk: false, permission: null, permissionCheckedAt: 0 };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function getSettings() {
  const stored = await browser.storage.local.get("settings");
  return Object.assign({}, DEFAULT_SETTINGS, stored.settings || {});
}

async function saveSettings(patch) {
  const current = await getSettings();
  const next = Object.assign({}, current, patch || {});
  await browser.storage.local.set({ settings: next });
  return next;
}

function normalizeBase(url, fallback) {
  const value = String(url || fallback).trim().replace(/\/+$/, "");
  return /^https?:\/\//i.test(value) ? value : fallback;
}

// ------------------------------------------------------------------ Whisper server proxy

async function apiRequest(path, body) {
  if (typeof path !== "string" || !path.startsWith("/")) {
    return { ok: false, error: "Invalid API path" };
  }
  const settings = await getSettings();
  const base = normalizeBase(settings.serverUrl, DEFAULT_SETTINGS.serverUrl);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const init = { method: body === undefined ? "GET" : "POST", signal: controller.signal, headers: {} };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(base + path, init);
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (err) {
      return { ok: false, error: "Server returned a non-JSON response" };
    }
    if (!res.ok) {
      return { ok: false, error: (data && data.error) || `HTTP ${res.status}`, data };
    }
    return { ok: true, data };
  } catch (err) {
    const timedOut = err && err.name === "AbortError";
    return { ok: false, offline: true, error: timedOut ? "Server timed out" : "Server unreachable" };
  } finally {
    clearTimeout(timer);
  }
}

// ------------------------------------------------------------------ mining helpers

function bytesToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function fetchClip(settings, videoId, start, end) {
  const base = normalizeBase(settings.serverUrl, DEFAULT_SETTINGS.serverUrl);
  const format = settings.clipFormat === "wav" ? "wav" : "mp3";
  const url = `${base}/clip?video_id=${encodeURIComponent(videoId)}&start=${start.toFixed(3)}&end=${end.toFixed(3)}&format=${format}`;
  for (let attempt = 0; attempt < 4; attempt++) {
    let res;
    try {
      res = await fetch(url);
    } catch (err) {
      return { ok: false, error: "Whisper server unreachable" };
    }
    if (res.status === 503) {
      await sleep(1500);
      continue;
    }
    if (!res.ok) {
      let message = `HTTP ${res.status}`;
      try {
        message = (await res.json()).error || message;
      } catch (err) {
        /* keep the status text */
      }
      return { ok: false, error: message };
    }
    const mime = (res.headers.get("Content-Type") || "audio/mpeg").split(";")[0].trim();
    const buffer = await res.arrayBuffer();
    return { ok: true, base64: bytesToBase64(buffer), mime, ext: mime === "audio/wav" ? "wav" : "mp3" };
  }
  return { ok: false, error: "The server is still fetching this video's audio, try again in a moment" };
}

async function anki(url, action, params, timeoutMs) {
  // No Content-Type header on purpose: a "simple" request needs no CORS preflight, which
  // matters for the very first requestPermission call from a not-yet-allowed origin.
  // requestPermission blocks until the viewer answers Anki's dialog, so it gets no timeout.
  const controller = new AbortController();
  const timer = timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const res = await fetch(url, {
      method: "POST",
      body: JSON.stringify({ action, version: 6, params: params || {} }),
      signal: controller.signal,
    });
    const data = await res.json();
    if (data && data.error) throw new Error(data.error);
    return data ? data.result : null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// Strip markup and whitespace so two spellings of the same sentence compare equal: Yomitan wraps
// the looked-up word in <b> and may use &nbsp;, and Whisper's spacing need not match.
function normalizeSentence(text) {
  return String(text === undefined || text === null ? "" : text)
    .replace(/<[^>]*>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/\s+/g, "");
}

async function ankiPermission(url) {
  if (ankiWatch.permission === "granted") return true;
  const now = Date.now();
  if (ankiWatch.permissionCheckedAt && now - ankiWatch.permissionCheckedAt < ANKI_PERMISSION_RECHECK_MS) return false;
  ankiWatch.permissionCheckedAt = now;
  const perm = await anki(url, "requestPermission", {});
  ankiWatch.permission = (perm && perm.permission) || "denied";
  return ankiWatch.permission === "granted";
}

// Report a note that appeared since the previous poll. Reports nothing whenever the baseline could
// be stale (first poll, previous poll failed, long gap) or when several notes arrived at once, so a
// card added while Anki was closed, or an import, is never touched.
async function ankiPoll() {
  const settings = await getSettings();
  if (!settings.autoMine || settings.mineTarget !== "anki") return { ok: true, newNoteId: null };
  const now = Date.now();
  const previousPollAt = ankiWatch.lastPollAt;
  if (now - previousPollAt < ANKI_POLL_THROTTLE_MS) return { ok: true, newNoteId: null };
  ankiWatch.lastPollAt = now;
  const url = normalizeBase(settings.ankiUrl, DEFAULT_SETTINGS.ankiUrl);
  try {
    if (!(await ankiPermission(url))) {
      ankiWatch.lastOk = false;
      return { ok: false, error: "AnkiConnect denied access. Click Yes in Anki's permission dialog." };
    }
    const ids = await anki(url, "findNotes", { query: "added:1" }, ANKI_POLL_TIMEOUT_MS);
    const list = (Array.isArray(ids) ? ids : []).map(Number).filter(Number.isFinite);
    const maxId = list.length ? Math.max(...list) : 0;
    const baseline = ankiWatch.baseline;
    const stale = baseline === null || !ankiWatch.lastOk || now - previousPollAt > ANKI_BASELINE_MAX_AGE_MS;
    ankiWatch.lastOk = true;
    if (stale || maxId > baseline) ankiWatch.baseline = maxId;
    if (stale || maxId <= baseline) return { ok: true, newNoteId: null };
    const added = list.filter((id) => id > baseline);
    return { ok: true, newNoteId: added.length === 1 ? maxId : null };
  } catch (err) {
    ankiWatch.lastOk = false;
    const network = err && err.name === "TypeError";
    return {
      ok: false,
      offline: true,
      error: network ? "Anki is not running or AnkiConnect is not installed" : String((err && err.message) || err),
    };
  }
}

// Anki may rename an uploaded file (recent versions lowercase it, and clashes get a suffix), so the
// field must reference the name storeMediaFile reports, not the one we asked for.
async function storeMedia(url, filename, base64) {
  const stored = await anki(url, "storeMediaFile", { filename, data: base64 });
  return typeof stored === "string" && stored ? stored : filename;
}

async function addToAnki(settings, cue, image, audio, explicitNoteId) {
  const url = normalizeBase(settings.ankiUrl, DEFAULT_SETTINGS.ankiUrl);
  const wanted = Number(explicitNoteId);
  const targetId = Number.isFinite(wanted) && wanted > 0 ? wanted : null;
  const what = targetId === null ? "newest" : "new";
  try {
    const perm = await anki(url, "requestPermission", {});
    if (!perm || perm.permission !== "granted") {
      return { ok: false, error: "AnkiConnect denied access. Click Yes in Anki's permission dialog, then mine again." };
    }
    let noteId = targetId;
    if (noteId === null) {
      const ids = await anki(url, "findNotes", { query: "added:1" });
      if (!Array.isArray(ids) || !ids.length) {
        return { ok: false, error: "No card was added today. Create the card with Yomitan first, then mine." };
      }
      noteId = Math.max(...ids);
    }
    const infos = await anki(url, "notesInfo", { notes: [noteId] });
    const fields = (infos && infos[0] && infos[0].fields) || {};
    if (targetId !== null) {
      // The note was picked by id, not by the viewer: make sure it really is about this subtitle
      // before writing media into it.
      const guardName = String(settings.ankiSentenceField || "").trim() || "Sentence";
      const written = normalizeSentence((fields[guardName] && fields[guardName].value) || "");
      const spoken = normalizeSentence(cue.text);
      if (written && spoken && !written.includes(spoken) && !spoken.includes(written)) {
        return { ok: false, mismatch: true, error: "The new card's sentence does not match the subtitle; nothing attached" };
      }
    }
    const update = {};
    const missing = [];
    if (image) {
      if (settings.ankiImageField in fields) {
        const stored = await storeMedia(url, image.filename, image.base64);
        update[settings.ankiImageField] = `<img src="${stored}">`;
      } else {
        missing.push(settings.ankiImageField);
      }
    }
    if (audio) {
      if (settings.ankiAudioField in fields) {
        const stored = await storeMedia(url, audio.filename, audio.base64);
        update[settings.ankiAudioField] = `[sound:${stored}]`;
      } else {
        missing.push(settings.ankiAudioField);
      }
    }
    const sentenceField = String(settings.ankiSentenceField || "").trim();
    if (sentenceField && sentenceField in fields) {
      const existing = ((fields[sentenceField] && fields[sentenceField].value) || "").trim();
      if (!existing) update[sentenceField] = cue.text;
    }
    if (!Object.keys(update).length) {
      return { ok: false, error: `The ${what} card has none of the fields ${missing.join(", ")}. Check the field names in the popup.` };
    }
    await anki(url, "updateNoteFields", { note: { id: noteId, fields: update } });
    let message = `Added ${Object.keys(update).join(" + ")} to the ${what} Anki card`;
    if (missing.length) message += ` (no field named ${missing.join(", ")})`;
    return { ok: true, target: "anki", noteId, message };
  } catch (err) {
    const network = err && err.name === "TypeError";
    return { ok: false, error: network ? "Anki is not running or AnkiConnect is not installed" : String((err && err.message) || err) };
  }
}

async function downloadFiles(image, audio) {
  const jobs = [];
  const names = [];
  if (image) {
    names.push(image.filename);
    jobs.push(browser.downloads.download({
      url: "data:image/jpeg;base64," + image.base64,
      filename: "shisu-ko-mining/" + image.filename,
      conflictAction: "uniquify",
      saveAs: false,
    }));
  }
  if (audio) {
    names.push(audio.filename);
    jobs.push(browser.downloads.download({
      url: `data:${audio.mime};base64,` + audio.base64,
      filename: "shisu-ko-mining/" + audio.filename,
      conflictAction: "uniquify",
      saveAs: false,
    }));
  }
  if (!jobs.length) return { ok: false, error: "Nothing to save" };
  try {
    await Promise.all(jobs);
    return { ok: true, target: "download", message: `Saved ${names.join(" and ")} to Downloads/shisu-ko-mining` };
  } catch (err) {
    return { ok: false, error: "Download failed: " + String((err && err.message) || err) };
  }
}

async function mineCue(msg) {
  const settings = await getSettings();
  const cue = msg && msg.cue;
  if (!cue || typeof cue.start !== "number" || typeof cue.end !== "number") {
    return { ok: false, error: "No subtitle to mine" };
  }
  const pad = Math.max(0, Number(settings.clipPaddingMs) || 0) / 1000;
  const start = Math.max(0, cue.start - pad);
  const end = Math.max(start + 0.3, cue.end + pad);
  const base = `shisuko_${msg.videoId}_${Math.round(cue.start * 1000)}`;

  const image = msg.imageDataUrl && msg.imageDataUrl.includes(",")
    ? { base64: msg.imageDataUrl.split(",")[1], filename: `${base}.jpg` }
    : null;

  const clip = await fetchClip(settings, msg.videoId, start, end);
  const audio = clip.ok ? { base64: clip.base64, filename: `${base}.${clip.ext}`, mime: clip.mime } : null;
  if (!image && !audio) {
    return { ok: false, error: clip.error || "Neither screenshot nor audio could be captured" };
  }
  const warnings = [];
  if (!audio) warnings.push(`no audio (${clip.error})`);
  if (!image) warnings.push("no screenshot (blocked for this video)");

  let result;
  if (settings.mineTarget === "anki") {
    result = await addToAnki(settings, cue, image, audio, msg.noteId);
    // Automatic mining never writes files: a failure the viewer did not ask for must stay quiet.
    if (!result.ok && !msg.auto && settings.mineFallbackDownload) {
      const fallback = await downloadFiles(image, audio);
      if (fallback.ok) {
        fallback.message = `Anki: ${result.error} Saved to Downloads instead.`;
        fallback.warning = true;
      }
      result = fallback;
    }
  } else {
    result = await downloadFiles(image, audio);
  }
  if (result.ok && warnings.length) result.message += ` (${warnings.join("; ")})`;
  return result;
}

// ------------------------------------------------------------------ messaging

browser.runtime.onMessage.addListener((msg) => {
  if (!msg || typeof msg !== "object") return undefined;
  switch (msg.type) {
    case "api":
      return apiRequest(msg.path, msg.body);
    case "getSettings":
      return getSettings();
    case "saveSettings":
      return saveSettings(msg.settings);
    case "mine":
      return mineCue(msg);
    case "ankiPoll":
      return ankiPoll();
    default:
      return undefined;
  }
});

browser.commands.onCommand.addListener(async (name) => {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  for (const tab of tabs) {
    if (tab.id === undefined) continue;
    browser.tabs.sendMessage(tab.id, { type: "command", name }).catch(() => {});
  }
});
