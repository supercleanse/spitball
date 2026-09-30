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
    "Call detected in Zoom: click for options");
  assert.equal(Model.tooltipFor({ state: "detected", app: "" }),
    "Call detected in an app: click for options");
});

test("tooltipFor: recording names the app and points at the menu, not stop", () => {
  // Per docs/SPEC-live-transcript.md: clicking the red dot while recording
  // must never stop it -- it opens the menu instead.
  assert.equal(Model.tooltipFor({ state: "recording", app: "Zoom" }), "Recording Zoom: click for options");
  assert.equal(Model.tooltipFor({ state: "recording", app: "" }), "Recording call: click for options");
});

test("tooltipFor: processing/error use message, with fallbacks", () => {
  assert.equal(Model.tooltipFor({ state: "processing", message: "Transcribing…" }), "Transcribing…");
  assert.equal(Model.tooltipFor({ state: "processing", message: "" }), "Processing…");
  assert.equal(Model.tooltipFor({ state: "error", message: "Deepgram error: 401" }), "Deepgram error: 401");
  assert.equal(Model.tooltipFor({ state: "error", message: "" }), "Spitball error");
});

test("tooltipFor: idle and offline/default", () => {
  assert.equal(Model.tooltipFor({ state: "idle" }), "No call in progress: click for options");
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

// ---------------------------------------------------------------- setup_needed

test("emptyState/parseState carry setup_needed, defaulting to empty", () => {
  assert.equal(Model.emptyState().setup_needed, "");
  const s = Model.parseState(JSON.stringify({ state: "idle" }));
  assert.equal(s.setup_needed, "");
});

test("parseState: non-string setup_needed is coerced to empty", () => {
  const s = Model.parseState(JSON.stringify({ state: "idle", setup_needed: 42 }));
  assert.equal(s.setup_needed, "");
});

test("parseState: setup_needed round-trips when idle", () => {
  const s = Model.parseState(JSON.stringify({ state: "idle", setup_needed: "Add a Deepgram key" }));
  assert.equal(s.setup_needed, "Add a Deepgram key");
});

test("needsSetup: true only when idle with a non-empty reason", () => {
  assert.equal(Model.needsSetup({ state: "idle", setup_needed: "Add a Deepgram key" }), true);
  assert.equal(Model.needsSetup({ state: "idle", setup_needed: "" }), false);
  assert.equal(Model.needsSetup({ state: "offline", setup_needed: "Add a Deepgram key" }), false);
  assert.equal(Model.needsSetup({ state: "recording", setup_needed: "Add a Deepgram key" }), false);
});

test("tooltipFor: needs-setup reason wins over the plain idle tooltip", () => {
  assert.equal(
    Model.tooltipFor({ state: "idle", setup_needed: "Download a local model" }),
    "Download a local model"
  );
  assert.equal(
    Model.tooltipFor({ state: "idle", setup_needed: "" }),
    "No call in progress: click for options"
  );
});

// ---------------------------------------------------------------- providers

test("providerOptions: Local leads (the voxtype default), then Deepgram -- phase 1 is exactly these two", () => {
  // opts (and anything .map() derives from it) is constructed in Model.js's
  // own vm realm, so compare by JSON shape rather than assert.deepEqual's
  // cross-realm prototype check (same reasoning as the last_call comparison
  // above) -- JSON.stringify flattens both sides to a plain string first.
  const opts = Model.providerOptions();
  const values = [], labels = [];
  for (let i = 0; i < opts.length; i++) { values.push(opts[i].value); labels.push(opts[i].label); }
  assert.equal(JSON.stringify(values), JSON.stringify(["local", "deepgram"]));
  assert.equal(JSON.stringify(labels), JSON.stringify(["Local", "Deepgram"]));
});

test("providerOptions: returns a fresh copy each call (caller can't mutate the source)", () => {
  const a = Model.providerOptions();
  a.push({ value: "x", label: "X" });
  assert.equal(Model.providerOptions().length, 2);
});

test("providerLabel: known values map to their display label", () => {
  assert.equal(Model.providerLabel("deepgram"), "Deepgram");
  assert.equal(Model.providerLabel("local"), "Local");
});

test("providerLabel: unknown/missing values fall back to the raw string", () => {
  assert.equal(Model.providerLabel("something-else"), "something-else");
  assert.equal(Model.providerLabel(""), "");
  assert.equal(Model.providerLabel(undefined), "");
});

// ---------------------------------------------------------------- key sources

test("keySourcePlaceholder: config/env source reads as Set", () => {
  assert.equal(Model.keySourcePlaceholder({ set: true, source: "config" }), "Set");
  assert.equal(Model.keySourcePlaceholder({ set: true, source: "env" }), "Set");
});

test("keySourcePlaceholder: command source reads as From command", () => {
  assert.equal(Model.keySourcePlaceholder({ set: true, source: "command" }), "From command");
});

test("keySourcePlaceholder: none/unset/missing reads as Not set", () => {
  assert.equal(Model.keySourcePlaceholder({ set: false, source: "none" }), "Not set");
  assert.equal(Model.keySourcePlaceholder(null), "Not set");
  assert.equal(Model.keySourcePlaceholder(undefined), "Not set");
  assert.equal(Model.keySourcePlaceholder({}), "Not set");
});

// ---------------------------------------------------------------- local (voxtype) info

test("engineTitle: title-cases a known engine name", () => {
  assert.equal(Model.engineTitle("whisper"), "Whisper");
  assert.equal(Model.engineTitle("parakeet"), "Parakeet");
});

test("engineTitle: empty/missing input is blank", () => {
  assert.equal(Model.engineTitle(""), "");
  assert.equal(Model.engineTitle(undefined), "");
  assert.equal(Model.engineTitle(null), "");
});

test("localInfoLine: installed shows engine + model", () => {
  assert.equal(
    Model.localInfoLine({ installed: true, engine: "whisper", model: "base.en" }),
    "Using voxtype: Whisper base.en"
  );
});

test("localInfoLine: not installed (or missing/malformed) reads blank -- the caller shows its own install prompt instead", () => {
  assert.equal(Model.localInfoLine({ installed: false, engine: "whisper", model: "base.en" }), "");
  assert.equal(Model.localInfoLine(null), "");
  assert.equal(Model.localInfoLine(undefined), "");
  assert.equal(Model.localInfoLine("not-an-object"), "");
});

test("localInfoLine: installed but missing engine/model degrades gracefully", () => {
  assert.equal(Model.localInfoLine({ installed: true }), "Using voxtype");
  assert.equal(Model.localInfoLine({ installed: true, model: "base.en" }), "Using voxtype: base.en");
  assert.equal(Model.localInfoLine({ installed: true, engine: "whisper" }), "Using voxtype: Whisper");
});

// ---------------------------------------------------------------- local model picker

test("localModelLabel: every optional segment present", () => {
  assert.equal(
    Model.localModelLabel({
      name: "Parakeet v3 (int8)", languages: "25 European languages", size_mb: 640,
      recommended: true, installed: true
    }),
    "Parakeet v3 (int8) — 25 European languages · 640 MB · Recommended · ✓ installed"
  );
});

test("localModelLabel: no optional segments is just the name", () => {
  assert.equal(Model.localModelLabel({ name: "Whisper base.en" }), "Whisper base.en");
});

test("localModelLabel: only the segments actually present appear", () => {
  assert.equal(Model.localModelLabel({ name: "X", size_mb: 142 }), "X — 142 MB");
  assert.equal(Model.localModelLabel({ name: "X", recommended: true }), "X — Recommended");
});

test("localModelLabel: no name at all is blank", () => {
  assert.equal(Model.localModelLabel({}), "");
  assert.equal(Model.localModelLabel(null), "");
});

test("localModelOptions: value/label pairs, skipping nameless entries", () => {
  const models = [
    { name: "Parakeet v3 (int8)", recommended: true },
    { name: "" },
    { name: "Whisper base.en" }
  ];
  const opts = Model.localModelOptions(models);
  const values = [];
  for (let i = 0; i < opts.length; i++) values.push(opts[i].value);
  assert.equal(JSON.stringify(values), JSON.stringify(["Parakeet v3 (int8)", "Whisper base.en"]));
  assert.equal(opts[0].label, "Parakeet v3 (int8) — Recommended");
});

test("localModelOptions: non-array input reads as empty", () => {
  assert.equal(Model.localModelOptions(null).length, 0);
  assert.equal(Model.localModelOptions(undefined).length, 0);
});

test("activeModelName: finds the active: true entry", () => {
  const models = [{ name: "a" }, { name: "b", active: true }, { name: "c" }];
  assert.equal(Model.activeModelName(models), "b");
});

test("activeModelName: none active, or malformed input, reads as empty", () => {
  assert.equal(Model.activeModelName([{ name: "a" }, { name: "b" }]), "");
  assert.equal(Model.activeModelName(null), "");
  assert.equal(Model.activeModelName(undefined), "");
});

// ---------------------------------------------------------------- language

test("languageOptions: English / Auto-detect / Other, in that order", () => {
  const opts = Model.languageOptions();
  const values = [], labels = [];
  for (let i = 0; i < opts.length; i++) { values.push(opts[i].value); labels.push(opts[i].label); }
  assert.equal(JSON.stringify(values), JSON.stringify(["en", "auto", "other"]));
  assert.equal(JSON.stringify(labels), JSON.stringify(["English", "Auto-detect", "Other…"]));
});

test("languageChoiceFor: empty/missing/en all read as the English chip", () => {
  assert.equal(Model.languageChoiceFor(""), "en");
  assert.equal(Model.languageChoiceFor(undefined), "en");
  assert.equal(Model.languageChoiceFor("en"), "en");
});

test("languageChoiceFor: auto reads as the Auto-detect chip", () => {
  assert.equal(Model.languageChoiceFor("auto"), "auto");
});

test("languageChoiceFor: any other code reads as the Other chip", () => {
  assert.equal(Model.languageChoiceFor("es"), "other");
  assert.equal(Model.languageChoiceFor("fr"), "other");
});

// ---------------------------------------------------------------- model friendly names

test("modelFriendlyName: known voxtype slugs map to a friendly name", () => {
  assert.equal(Model.modelFriendlyName("parakeet-tdt-0.6b-v3-int8"), "Parakeet v3 (int8)");
  assert.equal(Model.modelFriendlyName("base.en"), "Whisper Base (English)");
});

test("modelFriendlyName: accepts a model object with .name", () => {
  assert.equal(Model.modelFriendlyName({ name: "parakeet-tdt-0.6b-v3" }), "Parakeet v3");
});

test("modelFriendlyName: unknown slug falls back to the raw string", () => {
  assert.equal(Model.modelFriendlyName("some-future-model"), "some-future-model");
  assert.equal(Model.modelFriendlyName(""), "");
  assert.equal(Model.modelFriendlyName(null), "");
});

test("localModelLabel: uses the friendly name, not the raw slug", () => {
  assert.equal(
    Model.localModelLabel({ name: "parakeet-tdt-0.6b-v3-int8", recommended: true, installed: true }),
    "Parakeet v3 (int8) — Recommended · ✓ installed"
  );
});

test("findModel: finds by name, or null", () => {
  const models = [{ name: "a" }, { name: "b" }];
  assert.equal(Model.findModel(models, "b"), models[1]);
  assert.equal(Model.findModel(models, "nope"), null);
  assert.equal(Model.findModel(null, "a"), null);
});

test("switchConfirmText: known model with a size", () => {
  const models = [{ name: "parakeet-tdt-0.6b-v3-int8", size_mb: 640 }];
  assert.equal(
    Model.switchConfirmText(models, "parakeet-tdt-0.6b-v3-int8"),
    "Switch to Parakeet v3 (int8)? Downloads about 640 MB and asks for your password once. Also changes Omarchy dictation."
  );
});

test("switchConfirmText: model not in the list still reads sensibly", () => {
  assert.equal(
    Model.switchConfirmText([], "base.en"),
    "Switch to Whisper Base (English)? Asks for your password once. Also changes Omarchy dictation."
  );
});

// ---------------------------------------------------------------- model switch (model.json)

test("parseModelState: missing/unparseable/non-object all read as null", () => {
  assert.equal(Model.parseModelState(""), null);
  assert.equal(Model.parseModelState("not json"), null);
  assert.equal(Model.parseModelState(undefined), null);
  assert.equal(Model.parseModelState("42"), null);
  assert.equal(Model.parseModelState("null"), null);
});

test("parseModelState: valid JSON object round-trips", () => {
  const s = Model.parseModelState(JSON.stringify({ name: "base.en", state: "downloading" }));
  assert.equal(s.name, "base.en");
  assert.equal(s.state, "downloading");
});

test("modelSwitchActive: true only for the in-progress states", () => {
  for (const state of ["switching-engine", "downloading", "activating", "restarting"]) {
    assert.equal(Model.modelSwitchActive({ state }), true);
  }
  for (const state of ["done", "error", "terminal"]) {
    assert.equal(Model.modelSwitchActive({ state }), false);
  }
  assert.equal(Model.modelSwitchActive(null), false);
});

test("modelSwitchFailed: true only for state error", () => {
  assert.equal(Model.modelSwitchFailed({ state: "error" }), true);
  assert.equal(Model.modelSwitchFailed({ state: "downloading" }), false);
  assert.equal(Model.modelSwitchFailed(null), false);
});

test("modelSwitchPercent: computes from done/total, clamped 0-100", () => {
  assert.equal(Model.modelSwitchPercent({ done_bytes: 50, total_bytes: 200 }), 25);
  assert.equal(Model.modelSwitchPercent({ done_bytes: 200, total_bytes: 200 }), 100);
  assert.equal(Model.modelSwitchPercent({ done_bytes: 0, total_bytes: 0 }), -1);
  assert.equal(Model.modelSwitchPercent(null), -1);
});

test("modelSwitchStatusText: downloading includes the friendly name and percent", () => {
  const text = Model.modelSwitchStatusText({
    name: "parakeet-tdt-0.6b-v3-int8", state: "downloading", done_bytes: 84, total_bytes: 200
  });
  assert.equal(text, "Downloading Parakeet v3 (int8)… 42%");
});

test("modelSwitchStatusText: downloading with no byte count yet omits the percent", () => {
  const text = Model.modelSwitchStatusText({ name: "base.en", state: "downloading", done_bytes: 0, total_bytes: 0 });
  assert.equal(text, "Downloading Whisper Base (English)…");
});

test("modelSwitchStatusText: error surfaces the message", () => {
  assert.equal(
    Model.modelSwitchStatusText({ name: "base.en", state: "error", message: "Password prompt canceled" }),
    "Model switch failed: Password prompt canceled"
  );
});

test("modelSwitchStatusText: null reads as blank", () => {
  assert.equal(Model.modelSwitchStatusText(null), "");
});

// ---------------------------------------------------------------- live transcript (live.json)

test("emptyLiveState/parseLiveState: missing or unparseable text falls back to the empty shape", () => {
  const empty = Model.emptyLiveState();
  assert.equal(empty.call_id, "");
  assert.equal(empty.started_at, 0);
  assert.equal(empty.status, "");
  assert.equal(empty.message, "");
  assert.equal(empty.utterances.length, 0);
  const emptyJson = JSON.stringify(empty);
  assert.equal(JSON.stringify(Model.parseLiveState("")), emptyJson);
  assert.equal(JSON.stringify(Model.parseLiveState("not json")), emptyJson);
  assert.equal(JSON.stringify(Model.parseLiveState(undefined)), emptyJson);
  assert.equal(JSON.stringify(Model.parseLiveState("null")), emptyJson);
  assert.equal(JSON.stringify(Model.parseLiveState("42")), emptyJson);
});

test("parseLiveState: known statuses round-trip", () => {
  for (const status of ["listening", "catching-up", "unavailable", "stopped"]) {
    const s = Model.parseLiveState(JSON.stringify({ status, call_id: "c1" }));
    assert.equal(s.status, status);
    assert.equal(s.call_id, "c1");
  }
});

test("parseLiveState: unknown status is blanked, not passed through", () => {
  const s = Model.parseLiveState(JSON.stringify({ status: "not-a-real-status" }));
  assert.equal(s.status, "");
});

test("parseLiveState: non-array utterances / non-string message are coerced", () => {
  const s = Model.parseLiveState(JSON.stringify({ status: "listening", utterances: "nope", message: 5 }));
  assert.equal(s.utterances.length, 0);
  assert.equal(s.message, "");
});

test("parseLiveState: utterances array passes through", () => {
  const utts = [{ channel: 0, start: 1, end: 2, transcript: "hi" }];
  const s = Model.parseLiveState(JSON.stringify({ status: "listening", utterances: utts }));
  assert.equal(JSON.stringify(s.utterances), JSON.stringify(utts));
});

test("liveStatusText: one line per status", () => {
  assert.equal(Model.liveStatusText({ status: "listening" }), "Listening…");
  assert.equal(Model.liveStatusText({ status: "catching-up" }), "Catching up…");
  assert.equal(Model.liveStatusText({ status: "stopped" }), "Live transcript stopped.");
});

test("liveStatusText: unavailable surfaces the reason, falling back if blank", () => {
  assert.equal(Model.liveStatusText({ status: "unavailable", message: "Live transcript needs voxtype" }),
    "Live transcript needs voxtype");
  assert.equal(Model.liveStatusText({ status: "unavailable", message: "" }), "Live transcript unavailable");
});

test("liveStatusText: null/unknown reads as blank", () => {
  assert.equal(Model.liveStatusText(null), "");
  assert.equal(Model.liveStatusText({ status: "" }), "");
});

test("liveBubbles: sorted by start regardless of input order", () => {
  const bubbles = Model.liveBubbles([
    { channel: 0, start: 5, end: 6, transcript: "second" },
    { channel: 1, start: 1, end: 2, transcript: "first" },
  ]);
  assert.equal(bubbles[0].transcript, "first");
  assert.equal(bubbles[1].transcript, "second");
});

test("liveBubbles: showLabel true on the first bubble and on every speaker change", () => {
  const bubbles = Model.liveBubbles([
    { channel: 1, speaker: 0, start: 0, end: 1, transcript: "hey" },
    { channel: 1, speaker: 0, start: 1, end: 2, transcript: "there" },
    { channel: 0, speaker: 0, start: 2, end: 3, transcript: "hi" },
    { channel: 1, speaker: 0, start: 3, end: 4, transcript: "again" },
  ]);
  assert.equal(JSON.stringify(bubbles.map(b => b.showLabel)), JSON.stringify([true, false, true, true]));
});

test("liveBubbles: non-array/garbage entries are ignored, never throw", () => {
  assert.equal(Model.liveBubbles(null).length, 0);
  assert.equal(Model.liveBubbles(undefined).length, 0);
  const bubbles = Model.liveBubbles([null, { channel: 0, start: 1, end: 2, transcript: "x" }, "garbage"]);
  assert.equal(bubbles.length, 1);
});

test("liveBubbleSide: channel 0 is right (mine), anything else is left (them)", () => {
  assert.equal(Model.liveBubbleSide(0), "right");
  assert.equal(Model.liveBubbleSide(1), "left");
});

test("liveSpeakerLabel: channel 0 uses my_name (or a fallback), channel 1 is Them", () => {
  assert.equal(Model.liveSpeakerLabel(0, "Morgan"), "Morgan");
  assert.equal(Model.liveSpeakerLabel(0, ""), "Me");
  assert.equal(Model.liveSpeakerLabel(1, "Morgan"), "Them");
});

test("liveTimestampLabel: m:ss, zero-padded, never negative", () => {
  assert.equal(Model.liveTimestampLabel(0), "0:00");
  assert.equal(Model.liveTimestampLabel(5), "0:05");
  assert.equal(Model.liveTimestampLabel(65), "1:05");
  assert.equal(Model.liveTimestampLabel(-3), "0:00");
});

test("liveShouldAutoScroll: true when already at (or within threshold of) the bottom", () => {
  // contentHeight 500, viewport 200 tall -- bottom is at contentY == 300.
  assert.equal(Model.liveShouldAutoScroll(300, 200, 500), true);
  assert.equal(Model.liveShouldAutoScroll(290, 200, 500), true);  // within default 24px threshold
  assert.equal(Model.liveShouldAutoScroll(200, 200, 500), false); // scrolled up 100px -- stay put
});

test("liveShouldAutoScroll: custom threshold is honored", () => {
  assert.equal(Model.liveShouldAutoScroll(250, 200, 500, 60), true);
  assert.equal(Model.liveShouldAutoScroll(250, 200, 500, 10), false);
});

test("liveBubbles: partial flag passes through, absent means final", () => {
  const out = Model.liveBubbles([
    { channel: 0, start: 1, transcript: "done" },
    { channel: 0, start: 5, transcript: "still talk", partial: true },
  ]);
  assert.equal(out[0].partial, false);
  assert.equal(out[1].partial, true);
});

// ---------------------------------------------------------------- settings overlay: sections

const SECTION_IDS = ["general", "recording", "transcription", "live", "audio",
  "speakers", "summary", "calendar", "storage", "about"];

test("settingsSections: the ten sections, in the spec's order, each with a label and page", () => {
  const s = Model.settingsSections();
  assert.equal(JSON.stringify(s.map(x => x.id)), JSON.stringify(SECTION_IDS));
  assert.equal(JSON.stringify(s.map(x => x.label)), JSON.stringify(["General", "Recording",
    "Transcription", "Live", "Audio", "Speakers", "Summary", "Calendar", "Storage", "About"]));
  for (const entry of s) {
    assert.ok(/^[A-Z][A-Za-z]+Page\.qml$/.test(entry.page), entry.page);
    assert.equal(typeof entry.placeholder, "boolean");
  }
});

test("settingsSections: Speakers is the one placeholder left (Calendar landed in phase 2, Audio in phase 3)", () => {
  const placeholders = Model.settingsSections().filter(x => x.placeholder).map(x => x.id);
  assert.equal(JSON.stringify(placeholders), JSON.stringify(["speakers"]));
});

test("settingsSections: every page file exists under settings/", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  for (const entry of Model.settingsSections()) {
    const file = path.join(__dirname, "..", "..", "settings", entry.page);
    assert.ok(fs.existsSync(file), "missing " + file);
  }
});

test("settingsSections: returns a fresh copy each call", () => {
  const a = Model.settingsSections();
  a.push({ id: "x" });
  a[0].label = "Mutated";
  assert.equal(Model.settingsSections().length, 10);
  assert.equal(Model.settingsSections()[0].label, "General");
});

test("settingsDefaultSection is General", () => {
  assert.equal(Model.settingsDefaultSection(), "general");
});

test("settingsSectionIndex: known ids map to their position, anything else is -1", () => {
  assert.equal(Model.settingsSectionIndex("general"), 0);
  assert.equal(Model.settingsSectionIndex("transcription"), 2);
  assert.equal(Model.settingsSectionIndex("about"), 9);
  assert.equal(Model.settingsSectionIndex("nope"), -1);
  assert.equal(Model.settingsSectionIndex(""), -1);
  assert.equal(Model.settingsSectionIndex(undefined), -1);
  assert.equal(Model.settingsSectionIndex(null), -1);
});

test("settingsSectionAt: in-range index gives the entry, out of range gives null", () => {
  assert.equal(Model.settingsSectionAt(0).id, "general");
  assert.equal(Model.settingsSectionAt(9).id, "about");
  assert.equal(Model.settingsSectionAt(2.7).id, "transcription");
  assert.equal(Model.settingsSectionAt(10), null);
  assert.equal(Model.settingsSectionAt(-1), null);
  assert.equal(Model.settingsSectionAt("x"), null);
  assert.equal(Model.settingsSectionAt(undefined), null);
});

test("settingsNavStep: j/k move one and clamp at both ends, never wrapping", () => {
  assert.equal(Model.settingsNavStep(0, 1), 1);
  assert.equal(Model.settingsNavStep(3, -1), 2);
  assert.equal(Model.settingsNavStep(0, -1), 0);
  assert.equal(Model.settingsNavStep(9, 1), 9);
  assert.equal(Model.settingsNavStep(5, 100), 9);
  assert.equal(Model.settingsNavStep(5, -100), 0);
});

test("settingsNavStep: a bad starting index reads as the first section", () => {
  assert.equal(Model.settingsNavStep(-4, 0), 0);
  assert.equal(Model.settingsNavStep(42, 0), 0);
  assert.equal(Model.settingsNavStep(undefined, 1), 1);
  assert.equal(Model.settingsNavStep("garbage", 0), 0);
});

test("settingsSectionForKey: digits 1-9 jump to sections, everything else is -1", () => {
  assert.equal(Model.settingsSectionForKey("1"), 0);
  assert.equal(Model.settingsSectionForKey("9"), 8);
  assert.equal(Model.settingsSectionForKey("0"), -1);
  assert.equal(Model.settingsSectionForKey("j"), -1);
  assert.equal(Model.settingsSectionForKey("12"), -1);
  assert.equal(Model.settingsSectionForKey(""), -1);
  assert.equal(Model.settingsSectionForKey(undefined), -1);
});

test("settingsSectionBadge: Transcription says Set up whenever the bar's gear would show", () => {
  const needs = { state: "idle", setup_needed: "Add a Deepgram key" };
  const fine = { state: "idle", setup_needed: "" };
  assert.equal(Model.settingsSectionBadge("transcription", needs, null), "Set up");
  assert.equal(Model.settingsSectionBadge("transcription", fine, { state: "error" }), "Set up");
  assert.equal(Model.settingsSectionBadge("transcription", fine, { state: "downloading" }), "");
  assert.equal(Model.settingsSectionBadge("transcription", fine, null), "");
  assert.equal(Model.settingsSectionBadge("transcription", null, null), "");
  // Only idle shows the gear -- offline/recording never do (same rule as needsSetup).
  assert.equal(Model.settingsSectionBadge("transcription", { state: "recording", setup_needed: "x" }, null), "");
});

test("settingsSectionBadge: placeholders say Soon, everything else is blank", () => {
  assert.equal(Model.settingsSectionBadge("speakers", null, null), "Soon");
  assert.equal(Model.settingsSectionBadge("audio", null, null), "");
  assert.equal(Model.settingsSectionBadge("calendar", null, null), "");
  assert.equal(Model.settingsSectionBadge("general", null, null), "");
  assert.equal(Model.settingsSectionBadge("nope", null, null), "");
  assert.equal(Model.settingsSectionBadge(undefined, null, null), "");
});

// ---------------------------------------------------------------- settings overlay: call_apps

test("callAppsList: sorted {key, label} rows; garbage reads as empty", () => {
  const rows = Model.callAppsList({ zoom: "Zoom", brave: "Brave", chrome: "Chrome" });
  assert.equal(JSON.stringify(rows), JSON.stringify([
    { key: "brave", label: "Brave" }, { key: "chrome", label: "Chrome" }, { key: "zoom", label: "Zoom" }]));
  assert.equal(Model.callAppsList(null).length, 0);
  assert.equal(Model.callAppsList("zoom").length, 0);
  assert.equal(Model.callAppsList(["zoom"]).length, 0);
  assert.equal(Model.callAppsList({ x: "" })[0].label, "x");
});

test("callAppLabel: first letter upper-cased, rest kept", () => {
  assert.equal(Model.callAppLabel("zoom"), "Zoom");
  assert.equal(Model.callAppLabel("  jitsi "), "Jitsi");
  assert.equal(Model.callAppLabel("WhatsApp"), "WhatsApp");
  assert.equal(Model.callAppLabel(""), "");
  assert.equal(Model.callAppLabel(undefined), "");
});

test("callAppsAdd: adds under the lower-cased key, keeps the rest, never mutates the input", () => {
  const src = { zoom: "Zoom" };
  const next = Model.callAppsAdd(src, "  Jitsi ");
  assert.equal(JSON.stringify(next), JSON.stringify({ jitsi: "Jitsi", zoom: "Zoom" }));
  assert.equal(JSON.stringify(src), JSON.stringify({ zoom: "Zoom" }));
});

test("callAppsAdd: blank or duplicate names leave the map as it was", () => {
  assert.equal(JSON.stringify(Model.callAppsAdd({ zoom: "Zoom" }, "")), JSON.stringify({ zoom: "Zoom" }));
  assert.equal(JSON.stringify(Model.callAppsAdd({ zoom: "Zoom" }, "ZOOM")), JSON.stringify({ zoom: "Zoom" }));
  assert.equal(JSON.stringify(Model.callAppsAdd(null, "zoom")), JSON.stringify({ zoom: "Zoom" }));
});

test("callAppsRemove: drops one key, tolerates a missing one", () => {
  assert.equal(JSON.stringify(Model.callAppsRemove({ zoom: "Zoom", slack: "Slack" }, "zoom")),
    JSON.stringify({ slack: "Slack" }));
  assert.equal(JSON.stringify(Model.callAppsRemove({ zoom: "Zoom" }, "nope")), JSON.stringify({ zoom: "Zoom" }));
  assert.equal(JSON.stringify(Model.callAppsRemove(null, "zoom")), "{}");
});

test("callAppsSerialize: compact JSON the CLI parses back as an object", () => {
  const text = Model.callAppsSerialize({ zoom: "Zoom", brave: "Brave" });
  assert.equal(text, '{"brave":"Brave","zoom":"Zoom"}');
  assert.equal(JSON.stringify(JSON.parse(text)), text);
  assert.equal(Model.callAppsSerialize(null), "{}");
});

// ---------------------------------------------------------------- settings overlay: misc helpers

test("opusBitrateOptions: the standard ladder, default marked, current kept even when nonstandard", () => {
  const std = Model.opusBitrateOptions("32k");
  assert.equal(JSON.stringify(std.map(o => o.value)), JSON.stringify(["16k", "24k", "32k", "48k", "64k", "96k", "128k"]));
  assert.equal(std[2].label, "32k (default)");
  const odd = Model.opusBitrateOptions("40k");
  assert.equal(odd.length, 8);
  assert.equal(odd[7].value, "40k");
  assert.equal(Model.opusBitrateOptions("").length, 7);
  assert.equal(Model.opusBitrateOptions(undefined).length, 7);
});

test("liveEngineStatusLine: mirrors the CLI's three wordings; null reads blank", () => {
  assert.equal(Model.liveEngineStatusLine({ installed: true, model: "parakeet-unified-en-0.6b", fast: true }),
    "Fast engine: on (parakeet-unified-en-0.6b)");
  assert.equal(Model.liveEngineStatusLine({ installed: true, model: "", fast: true }), "Fast engine: on");
  assert.equal(Model.liveEngineStatusLine({ installed: false, model: "", fast: false }), "Fast engine: not installed");
  assert.equal(Model.liveEngineStatusLine({ installed: true, model: "", fast: false }),
    "Fast engine: installed, but voxtype isn't on a Parakeet model");
  assert.equal(Model.liveEngineStatusLine(null), "");
  assert.equal(Model.liveEngineStatusLine("x"), "");
});

test("daemonStateLabel: one line per state, with the app/message when present", () => {
  assert.equal(Model.daemonStateLabel({ state: "offline" }), "Daemon: not running");
  assert.equal(Model.daemonStateLabel(null), "Daemon: not running");
  assert.equal(Model.daemonStateLabel({ state: "idle" }), "Daemon: idle");
  assert.equal(Model.daemonStateLabel({ state: "detected", app: "Zoom" }), "Daemon: call detected in Zoom");
  assert.equal(Model.daemonStateLabel({ state: "recording", app: "Slack" }), "Daemon: recording Slack");
  assert.equal(Model.daemonStateLabel({ state: "recording", app: "" }), "Daemon: recording");
  assert.equal(Model.daemonStateLabel({ state: "processing" }), "Daemon: processing");
  assert.equal(Model.daemonStateLabel({ state: "error", message: "boom" }), "Daemon: error — boom");
});

test("presentAppsLine: lists the apps on the mic, or says none", () => {
  assert.equal(Model.presentAppsLine(["Zoom", "Chrome"]), "Using the microphone now: Zoom, Chrome");
  assert.equal(Model.presentAppsLine([]), "No call app is using the microphone right now.");
  assert.equal(Model.presentAppsLine(null), "No call app is using the microphone right now.");
  assert.equal(Model.presentAppsLine([""]), "No call app is using the microphone right now.");
});

// ---------------------------------------------------------------- settings overlay: calendar

test("calendarSourceOptions: ics then command, fresh objects each call", () => {
  const a = Model.calendarSourceOptions();
  assert.equal(JSON.stringify(a.map(o => o.value)), JSON.stringify(["ics", "command"]));
  for (const o of a) assert.equal(typeof o.label, "string");
  a[0].label = "Mutated";
  assert.notEqual(Model.calendarSourceOptions()[0].label, "Mutated");
});

test("calendarSourceChoice: only 'command' is command; everything else is ics", () => {
  assert.equal(Model.calendarSourceChoice("command"), "command");
  assert.equal(Model.calendarSourceChoice("ics"), "ics");
  assert.equal(Model.calendarSourceChoice(""), "ics");
  assert.equal(Model.calendarSourceChoice(undefined), "ics");
  assert.equal(Model.calendarSourceChoice("caldav"), "ics");
});

test("calendarTestText: CLI failure, source failure, match, and no-match lines", () => {
  assert.equal(Model.calendarTestText(false, null), "✗ Couldn't reach spitball");
  assert.equal(Model.calendarTestText(true, null), "✗ Couldn't reach spitball");
  assert.equal(Model.calendarTestText(true, { ok: false, message: "calendar feed: HTTP 404" }),
    "✗ calendar feed: HTTP 404");
  assert.equal(Model.calendarTestText(true, { ok: false, error: "no feed address set" }),
    "✗ no feed address set");
  assert.equal(Model.calendarTestText(true, { ok: true, source: "ics", match: { title: "Weekly sync" }, confidence: 75 }),
    "✓ Matched \u201cWeekly sync\u201d (score 75)");
  assert.equal(Model.calendarTestText(true, { ok: true, source: "ics", match: null, events_nearby: 2,
    summary: "no confident match (2 candidates, best score 30)" }),
    "✓ Feed OK; no confident match (2 candidates, best score 30)");
  assert.equal(Model.calendarTestText(true, { ok: true, source: "command", cached: false, match: null, events_nearby: 0 }),
    "✓ Command OK; no events at that time");
  assert.equal(Model.calendarTestText(true, { ok: true, source: "ics", cached: true, match: null, events_nearby: 1 }),
    "✓ Feed OK (cached); 1 event(s) nearby, no confident match");
});

// ---------------------------------------------------------------- audio page: mic_denoise

test("micDenoiseOptions: off / auto / on, in that order, with labels", () => {
  const o = Model.micDenoiseOptions();
  assert.equal(JSON.stringify(o.map(x => x.value)), JSON.stringify(["off", "auto", "on"]));
  assert.equal(JSON.stringify(o.map(x => x.label)), JSON.stringify(["Off", "Auto", "On"]));
});

test("micDenoiseChoice: unknown, missing, or odd-cased values read as auto", () => {
  assert.equal(Model.micDenoiseChoice("off"), "off");
  assert.equal(Model.micDenoiseChoice(" ON "), "on");
  assert.equal(Model.micDenoiseChoice("auto"), "auto");
  assert.equal(Model.micDenoiseChoice("loud"), "auto");
  assert.equal(Model.micDenoiseChoice(""), "auto");
  assert.equal(Model.micDenoiseChoice(undefined), "auto");
  assert.equal(Model.micDenoiseChoice(null), "auto");
});

test("micDenoiseNote: one line per mode, never empty", () => {
  for (const v of ["off", "auto", "on"]) {
    const note = Model.micDenoiseNote(v);
    assert.ok(note.length > 20, v);
    assert.equal(note.indexOf("\n"), -1, v);
  }
  assert.equal(Model.micDenoiseNote("garbage"), Model.micDenoiseNote("auto"));
});

test("micDenoiseNotes: all three rows, exactly one selected", () => {
  const rows = Model.micDenoiseNotes("on");
  assert.equal(rows.length, 3);
  assert.equal(JSON.stringify(rows.map(r => r.selected)), JSON.stringify([false, false, true]));
  assert.equal(rows[2].label, "On");
  assert.equal(rows[2].note, Model.micDenoiseNote("on"));
  assert.equal(JSON.stringify(Model.micDenoiseNotes(undefined).map(r => r.selected)),
    JSON.stringify([false, true, false]));
});

test("micNoiseFloorDb: integer dBFS clamped to -80..-20, default -45", () => {
  assert.equal(Model.micNoiseFloorDb(-45), -45);
  assert.equal(Model.micNoiseFloorDb("-52"), -52);
  assert.equal(Model.micNoiseFloorDb(-38.6), -39);
  assert.equal(Model.micNoiseFloorDb(-200), -80);
  assert.equal(Model.micNoiseFloorDb(5), -20);
  assert.equal(Model.micNoiseFloorDb("abc"), -45);
  assert.equal(Model.micNoiseFloorDb(undefined), -45);
});

test("settingsSections: Audio is no longer a placeholder", () => {
  const audio = Model.settingsSections().find(x => x.id === "audio");
  assert.equal(audio.placeholder, false);
  assert.equal(audio.page, "AudioPage.qml");
});
