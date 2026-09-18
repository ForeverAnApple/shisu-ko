"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { loadContent } = require("./_loadContent");

// Arrays built inside the vm sandbox belong to another realm, so deepEqual rejects them on
// prototype identity alone; a JSON round trip compares them in this one.
const plain = (value) => JSON.parse(JSON.stringify(value));

const NOW = 1_000_000;

// A paused viewer on a video the server has fully transcribed around the playhead.
function idle(patch) {
  return Object.assign(
    { paused: true, t: 100, status: "ready", covered: [[0, 1200]], duration: 1200, lastSyncAt: NOW - 1000 },
    patch
  );
}

// ------------------------------------------------------------------ shouldSync

test("shouldSync keeps the one second cadence while the video plays", () => {
  const { api } = loadContent();
  assert.equal(api.shouldSync(idle({ paused: false }), NOW), true);
});

test("shouldSync skips the request when a paused video needs nothing", () => {
  const { api } = loadContent();
  assert.equal(api.shouldSync(idle(), NOW), false);
});

test("shouldSync keeps asking while the server still has work around the playhead", () => {
  const { api } = loadContent();
  // Covered only to 200 s of a 1200 s video, and the server transcribes 900 s ahead of 100 s.
  assert.equal(api.shouldSync(idle({ covered: [[0, 200]] }), NOW), true);
  // The playhead is not inside any covered range at all.
  assert.equal(api.shouldSync(idle({ t: 900, covered: [[0, 200]] }), NOW), true);
});

test("shouldSync keeps asking while the server is not ready", () => {
  const { api } = loadContent();
  for (const status of ["connecting", "pending", "downloading", "decoding", "error", "offline"]) {
    assert.equal(api.shouldSync(idle({ status }), NOW), true, status);
  }
});

test("shouldSync still beats every five seconds while paused", () => {
  const { api } = loadContent();
  assert.equal(api.shouldSync(idle({ lastSyncAt: NOW - 4999 }), NOW), false);
  assert.equal(api.shouldSync(idle({ lastSyncAt: NOW - 5000 }), NOW), true);
  assert.equal(api.shouldSync(idle({ lastSyncAt: 0 }), NOW), true);
});

test("shouldSync asks while the duration is still unknown", () => {
  const { api } = loadContent();
  assert.equal(api.shouldSync(idle({ duration: 0 }), NOW), true);
});

test("shouldSync stops once the covered range reaches the end of a short video", () => {
  const { api } = loadContent();
  // Shorter than the server's lookahead: the end of the video is the target, not playhead + 900 s.
  assert.equal(api.shouldSync(idle({ t: 30, duration: 60, covered: [[0, 60]] }), NOW), false);
  assert.equal(api.shouldSync(idle({ t: 30, duration: 60, covered: [[0, 45]] }), NOW), true);
});

// ------------------------------------------------------------------ coveredEnd

test("coveredEnd returns the end of the range holding the playhead, else null", () => {
  const { api } = loadContent();
  assert.equal(api.coveredEnd([[0, 40], [60, 90]], 20), 40);
  assert.equal(api.coveredEnd([[0, 40], [60, 90]], 70), 90);
  assert.equal(api.coveredEnd([[0, 40], [60, 90]], 50), null);
  assert.equal(api.coveredEnd(null, 10), null);
});

// ------------------------------------------------------------------ cue bookkeeping

test("mergeCues indexes cues by id, ignores repeats and keeps them in order", () => {
  const { api } = loadContent();
  api.mergeCues([
    { id: 0, start: 0, end: 1, text: "いち" },
    { id: 1, start: 1, end: 2, text: "に" },
  ]);
  api.mergeCues([
    { id: 1, start: 1, end: 2, text: "に" }, // already known
    { id: 2, start: 2, end: 3, text: "さん" },
  ]);
  assert.deepEqual(plain(api.state.cues.map((c) => c.id)), [0, 1, 2]);
  assert.equal(api.cueById(2).text, "さん");
  assert.equal(api.cueById(0).text, "いち"); // id 0 is a real cue, not "no cue"
  assert.equal(api.cueById(99), null);
});

test("mergeCues sorts a cue that arrives out of order", () => {
  const { api } = loadContent();
  api.mergeCues([{ id: 0, start: 10, end: 11, text: "あと" }]);
  api.mergeCues([{ id: 1, start: 2, end: 3, text: "さき" }]);
  assert.deepEqual(plain(api.state.cues.map((c) => c.start)), [2, 10]);
});

test("findActiveCue picks the cue at the playhead and lingers past the last one", () => {
  const { api } = loadContent();
  api.mergeCues([
    { id: 0, start: 0, end: 2, text: "いち" },
    { id: 1, start: 5, end: 7, text: "に" },
  ]);
  assert.equal(api.findActiveCue(1).id, 0);
  assert.equal(api.findActiveCue(6).id, 1);
  assert.equal(api.findActiveCue(4), null); // the blank between them is long enough to be a blank
  assert.equal(api.findActiveCue(7.2).id, 1); // lingerSeconds keeps the last cue up for a moment
  assert.equal(api.findActiveCue(9), null);
});

// ------------------------------------------------------------------ jumpTarget

// Three lines with a gap between the second and the third.
const JUMP_CUES = [
  { id: 0, start: 0, end: 2, text: "いち" },
  { id: 1, start: 5, end: 7, text: "に" },
  { id: 2, start: 20, end: 22, text: "さん" },
];

test("jumpTarget replays the current line once the viewer is a second into it", () => {
  const { api } = loadContent();
  assert.equal(api.jumpTarget(JUMP_CUES, 6.5, -1), 4.85); // 5 - the 0.15 s lead-in
  assert.equal(api.jumpTarget(JUMP_CUES, 9, -1), 4.85); // still the last line that started
});

test("jumpTarget steps back to the line before when the current one just started", () => {
  const { api } = loadContent();
  assert.equal(api.jumpTarget(JUMP_CUES, 5.5, -1), 0); // 0.5 s in: the viewer meant the line before
  assert.equal(api.jumpTarget(JUMP_CUES, 6, -1), 0); // exactly 1.0 s in is not yet a replay
  assert.equal(api.jumpTarget(JUMP_CUES, 20.5, -1), 4.85);
});

test("jumpTarget lands on the start of the video before the first line", () => {
  const { api } = loadContent();
  assert.equal(api.jumpTarget(JUMP_CUES, 0.5, -1), 0); // inside the first cue, less than a second
  assert.equal(api.jumpTarget(JUMP_CUES, 1.5, -1), 0); // replaying cue 0 clamps to 0 as well
  assert.equal(api.jumpTarget(JUMP_CUES, -1, -1), 0); // before every cue
});

test("jumpTarget moves to the next line, or reports none ahead", () => {
  const { api } = loadContent();
  assert.equal(api.jumpTarget(JUMP_CUES, 0, 1), 4.85);
  assert.equal(api.jumpTarget(JUMP_CUES, 6, 1), 19.85);
  assert.equal(api.jumpTarget(JUMP_CUES, 10, 1), 19.85); // in the gap: the next line still counts
  assert.equal(api.jumpTarget(JUMP_CUES, 20, 1), null); // on the last line, nothing ahead
  assert.equal(api.jumpTarget(JUMP_CUES, 60, 1), null);
});

test("jumpTarget with no cues rewinds to the start and reports nothing ahead", () => {
  const { api } = loadContent();
  assert.equal(api.jumpTarget([], 42, -1), 0);
  assert.equal(api.jumpTarget([], 42, 1), null);
});

// ------------------------------------------------------------------ Anki polling

test("ankiPollAllowed polls a playing video and stops on a hidden tab", () => {
  const { api, sandbox } = loadContent();
  api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }]);
  api.state.videoId = "abcdef1234";
  assert.equal(api.ankiPollAllowed(), true);
  sandbox.document.visibilityState = "hidden";
  assert.equal(api.ankiPollAllowed(), false);
});

test("ankiPollAllowed gives up on a video left paused, and resumes for a reader", () => {
  const { api } = loadContent();
  api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }]);
  api.state.videoId = "abcdef1234";
  api.state.pausedSince = Date.now() - 10_000;
  assert.equal(api.ankiPollAllowed(), true); // ten seconds: the viewer is probably still looking
  api.state.pausedSince = Date.now() - 180_000;
  assert.equal(api.ankiPollAllowed(), false);
  api.state.hoverPaused = true; // the pointer is on the subtitle: a lookup is in progress
  assert.equal(api.ankiPollAllowed(), true);
});

test("ankiPollAllowed stays quiet with nothing to attach or the feature off", () => {
  const { api } = loadContent();
  api.state.videoId = "abcdef1234";
  assert.equal(api.ankiPollAllowed(), false); // no cues yet
  api.mergeCues([{ id: 0, start: 0, end: 2, text: "いち" }]);
  assert.equal(api.ankiPollAllowed(), true);
  api.state.settings.autoMine = false;
  assert.equal(api.ankiPollAllowed(), false);
  api.state.settings.autoMine = true;
  api.state.offline = true;
  assert.equal(api.ankiPollAllowed(), false);
});
