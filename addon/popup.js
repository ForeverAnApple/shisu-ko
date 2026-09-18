"use strict";

// Every setting has an input with the same id in popup.html (checked by addon/tests/settings.test.js).
const FIELDS = Object.keys(SHISUKO_DEFAULT_SETTINGS);

// Firefox MV3 treats host permissions as optional: nothing is granted at install, so the content
// script never runs until the user allows youtube.com (clicking the toolbar icon only grants the
// current tab, for that visit). The banner makes the missing grant visible and fixable.
const YOUTUBE_ORIGINS = ["*://www.youtube.com/*", "*://m.youtube.com/*", "*://youtube.com/*"];

let saveTimer = null;
let serverCheckPending = false;

function readField(el) {
  if (el.type === "checkbox") return el.checked;
  if (el.type === "range" || el.type === "number") return Number(el.value);
  // A colour well always reports a normalised "#rrggbb"; trimming it would be harmless but a lie.
  if (el.type === "color") return el.value;
  return el.value.trim();
}

function readForm() {
  const patch = {};
  for (const key of FIELDS) {
    const el = document.getElementById(key);
    if (el) patch[key] = readField(el);
  }
  return patch;
}

// Each range shows its value and paints the travelled part of its own track (the --fill custom
// property; see popup.css), so the slider carries the value twice: by position and by length.
const RANGES = {
  fontScale: (v) => `${Math.round(v * 100)}%`,
  lingerSeconds: (v) => `${v.toFixed(1)} s`,
  subPosition: (v) => `${v}%`,
  subBackgroundOpacity: (v) => `${v}%`,
  clipPaddingMs: (v) => `${v} ms`,
};

// "Reset style" restores these and nothing else, so a botched experiment costs one click.
const STYLE_KEYS = ["subPosition", "subFont", "subTextColor", "subBackgroundOpacity", "subOutline", "transcriptSide"];

function updateOutputs() {
  for (const [id, format] of Object.entries(RANGES)) {
    const el = document.getElementById(id);
    const value = Number(el.value);
    const min = Number(el.min);
    el.style.setProperty("--fill", `${((value - min) / (Number(el.max) - min)) * 100}%`);
    document.getElementById(`${id}Out`).textContent = format(value);
  }
  const on = document.getElementById("enabled").checked;
  document.getElementById("enabled-label").textContent = on ? "On" : "Off";
  document.body.classList.toggle("off", !on);
}

function setField(el, value) {
  if (el.type === "checkbox") el.checked = !!value;
  else el.value = value === undefined || value === null ? "" : value;
}

async function resetStyle() {
  const patch = {};
  for (const key of STYLE_KEYS) {
    patch[key] = SHISUKO_DEFAULT_SETTINGS[key];
    const el = document.getElementById(key);
    if (el) setField(el, patch[key]);
  }
  updateOutputs();
  // A pending edit would otherwise land after the reset and put the old value back.
  clearTimeout(saveTimer);
  serverCheckPending = false;
  await browser.runtime.sendMessage({ type: "saveSettings", settings: patch });
}

function onChange(ev) {
  updateOutputs();
  if (ev.target.id === "serverUrl") serverCheckPending = true;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const shouldCheckServer = serverCheckPending;
    serverCheckPending = false;
    await browser.runtime.sendMessage({ type: "saveSettings", settings: readForm() });
    if (shouldCheckServer) checkServer();
  }, 150);
}

// The status line answers the popup's first question: can it transcribe right now? The badge word
// and its dot carry the state, the detail line the evidence (which model, which device) or the fix.
async function checkServer() {
  const badge = document.getElementById("server-status");
  const detail = document.getElementById("server-detail");
  badge.textContent = "Checking server";
  badge.className = "badge";
  detail.textContent = "";
  const res = await browser.runtime.sendMessage({ type: "api", path: "/health" }).catch(() => null);
  if (res && res.ok && res.data) {
    const d = res.data;
    badge.textContent = "Server online";
    badge.className = "badge ok";
    detail.textContent = `${d.model} · ${d.device} · ${d.compute_type}`;
  } else {
    badge.textContent = "Server offline";
    badge.className = "badge bad";
    detail.textContent = "start server/run.cmd or docker/up.cmd";
  }
}

// Reloading the open YouTube tabs is what actually injects the content script; a freshly granted
// permission does not reach pages that are already loaded.
async function reloadYouTubeTabs() {
  try {
    // tabs.query with a url filter needs the "tabs" permission to match against URLs.
    const tabs = await browser.tabs.query({ url: YOUTUBE_ORIGINS });
    for (const tab of tabs) await browser.tabs.reload(tab.id);
  } catch (err) {
    /* nothing to reload if the query is refused */
  }
}

async function setupPermissionBanner() {
  const banner = document.getElementById("permission-banner");
  const button = document.getElementById("grant-permission");
  try {
    if (await browser.permissions.contains({ origins: YOUTUBE_ORIGINS })) return;
    banner.classList.remove("hidden");
  } catch (err) {
    return; // no permissions API (older Firefox): leave the banner hidden
  }
  button.addEventListener("click", async () => {
    // request() must be called straight from the click handler; it needs the user gesture.
    const granted = await browser.permissions.request({ origins: YOUTUBE_ORIGINS }).catch(() => false);
    if (!granted) return;
    banner.classList.add("hidden");
    await reloadYouTubeTabs();
  });
}

async function init() {
  setupPermissionBanner();
  const settings = await browser.runtime.sendMessage({ type: "getSettings" });
  for (const key of FIELDS) {
    const el = document.getElementById(key);
    if (el) setField(el, settings[key]);
  }
  updateOutputs();
  document.getElementById("reset-style").addEventListener("click", resetStyle);
  for (const key of FIELDS) {
    const el = document.getElementById(key);
    if (!el) continue;
    const eventName = el.type === "text" || el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(eventName, onChange);
  }
  checkServer();
}

document.addEventListener("DOMContentLoaded", init);
