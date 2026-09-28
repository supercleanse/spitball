"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadModelJs } = require("./model_loader");

const Model = loadModelJs();

test("emptyState returns the offline default shape", () => {
  const s = Model.emptyState();
  assert.equal(s.state, "offline");
  assert.equal(s.app, "");
  assert.equal(s.started_at, 0);
  assert.equal(s.auto_record, false);
  assert.equal(s.message, "");
  assert.equal(s.last_call, null);
  assert.equal(s.updated_at, 0);
});

test("parseState: missing/unparseable text falls back to offline", () => {
  assert.deepEqual(Model.parseState(""), Model.emptyState());
  assert.deepEqual(Model.parseState("not json"), Model.emptyState());
  assert.deepEqual(Model.parseState(undefined), Model.emptyState());
  assert.deepEqual(Model.parseState("null"), Model.emptyState());
  assert.deepEqual(Model.parseState("42"), Model.emptyState());
});

test("parseState: unknown state string falls back to offline", () => {
  const s = Model.parseState(JSON.stringify({ state: "not-a-real-state" }));
  assert.equal(s.state, "offline");
});

test("parseState: each known state round-trips", () => {
  for (const state of ["offline", "idle", "detected", "recording", "processing", "error"]) {
    const s = Model.parseState(JSON.stringify({ state, app: "Zoom" }));
    assert.equal(s.state, state);
    assert.equal(s.app, "Zoom");
  }
});

test("parseState: missing fields are filled from the default", () => {
  const s = Model.parseState(JSON.stringify({ state: "idle" }));
  assert.equal(s.auto_record, false);
  assert.equal(s.message, "");
  assert.equal(s.last_call, null);
});

test("parseState: non-object last_call is coerced to null", () => {
  const s = Model.parseState(JSON.stringify({ state: "idle", last_call: "not-an-object" }));
  assert.equal(s.last_call, null);
});

test("parseState: object last_call passes through", () => {
  // The parsed last_call is a plain object created inside Model.js's own vm
  // context (a different realm than this test file), so compare by JSON
  // shape rather than deepEqual's cross-realm prototype check.
  const lc = { dir: "/x", title: "T", ended_at: 1, summary: "/x/summary.md" };
  const s = Model.parseState(JSON.stringify({ state: "idle", last_call: lc }));
  assert.equal(JSON.stringify(s.last_call), JSON.stringify(lc));
});

test("elapsedLabel: zero/unset started_at is 0:00", () => {
  assert.equal(Model.elapsedLabel(0, Date.now()), "0:00");
  assert.equal(Model.elapsedLabel(undefined, Date.now()), "0:00");
});

test("elapsedLabel: m:ss under an hour", () => {
  const started = 1000;
  const now = (1000 + 65) * 1000; // 1:05 elapsed
  assert.equal(Model.elapsedLabel(started, now), "1:05");
});

test("elapsedLabel: single-digit seconds are zero-padded", () => {
  const started = 1000;
  const now = (1000 + 5) * 1000;
  assert.equal(Model.elapsedLabel(started, now), "0:05");
});

test("elapsedLabel: h:mm:ss at or past one hour", () => {
  const started = 1000;
  const now = (1000 + 3725) * 1000; // 1:02:05
  assert.equal(Model.elapsedLabel(started, now), "1:02:05");
});

test("elapsedLabel: never goes negative for a clock that hasn't caught up", () => {
  const started = 1000;
  const now = 999 * 1000; // now before started
  assert.equal(Model.elapsedLabel(started, now), "0:00");
});

test("tooltipFor: detected names the app", () => {
  assert.equal(Model.tooltipFor({ state: "detected", app: "Zoom" }),
    "Call detected in Zoom: click to record");
  assert.equal(Model.tooltipFor({ state: "detected", app: "" }),
    "Call detected in an app: click to record");
});

test("tooltipFor: recording names the app", () => {
  assert.equal(Model.tooltipFor({ state: "recording", app: "Zoom" }), "Recording Zoom: click to stop");
  assert.equal(Model.tooltipFor({ state: "recording", app: "" }), "Recording call: click to stop");
});

test("tooltipFor: processing/error use message, with fallbacks", () => {
  assert.equal(Model.tooltipFor({ state: "processing", message: "Transcribing…" }), "Transcribing…");
  assert.equal(Model.tooltipFor({ state: "processing", message: "" }), "Processing…");
  assert.equal(Model.tooltipFor({ state: "error", message: "Deepgram error: 401" }), "Deepgram error: 401");
  assert.equal(Model.tooltipFor({ state: "error", message: "" }), "Spitball error");
});

test("tooltipFor: idle and offline/default", () => {
  assert.equal(Model.tooltipFor({ state: "idle" }), "No call in progress: click to record");
  assert.equal(Model.tooltipFor({ state: "offline" }), "Spitball: daemon not running");
  assert.equal(Model.tooltipFor({ state: "garbage" }), "Spitball: daemon not running");
});

test("truncate: short text is untouched, long text gets an ellipsis", () => {
  assert.equal(Model.truncate("short", 20), "short");
  assert.equal(Model.truncate("", 20), "");
  const long = "x".repeat(40);
  const out = Model.truncate(long, 20);
  assert.equal(out.length, 20);
  assert.ok(out.endsWith("…"));
});

test("lastCallLabel: null/untitled falls back to a generic label", () => {
  assert.equal(Model.lastCallLabel(null), "Open last summary");
  assert.equal(Model.lastCallLabel({}), "Open last summary");
});

test("lastCallLabel: titled call includes a truncated title", () => {
  const label = Model.lastCallLabel({ title: "Weekly Sync With Morgan About The Roadmap" });
  assert.ok(label.startsWith("Open last summary: "));
  assert.ok(label.length <= "Open last summary: ".length + 34);
});
