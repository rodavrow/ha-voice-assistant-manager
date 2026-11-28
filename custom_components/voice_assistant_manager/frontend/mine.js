// SPDX-FileCopyrightText: 2026 rodavrow
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The My assistant page of Voice Assistant Manager: what a signed-in person may see and change
// about their own assistant. One custom element, no build step and no libraries, in the look
// of the settings page (panel.js). Everything comes from the voice_assistant_manager/me/*
// websocket commands, which work out who is asking from the connection: the page never
// sends who the user is. Everything is put on the page as text, never as HTML: personas,
// facts and reminder titles come from people and services outside this page.

const WS = "voice_assistant_manager/me";
const ELEMENT = "voice-assistant-manager-mine";
const PRIVATE_PREFIX = "(private)";
const MAX_PERSONA = 2000; // mine_api.MAX_PERSONA_LENGTH
const MAX_TITLE = 200; // mine_api.MAX_TITLE_LENGTH
// Favorites (favorites.py): the longest name and address.
const MAX_FAVORITE_NAME = 80;
const MAX_FAVORITE_URL = 2000;
const DAYS = ["today", "tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

/** Create an element. Children are nodes or strings; strings become text nodes. */
function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node && key !== "list") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child);
  }
  return node;
}

/** A readable reason from a websocket error or an Error. */
function reason(err) {
  if (!err) return "";
  if (typeof err === "string") return err;
  return err.message || err.error || String(err);
}

/** The text of a refusal: {ok: false, errors: {base: text}}. */
const refusal = (result) => Object.values((result && result.errors) || {}).join(" ");

const SECTIONS = [
  { id: "persona", icon: "mdi:account-voice" },
  { id: "reminders", icon: "mdi:bell-outline" },
  { id: "memory", icon: "mdi:head-lightbulb-outline" },
  { id: "favorites", icon: "mdi:star-outline" },
  { id: "chat", icon: "mdi:forum-outline" },
  { id: "shared", icon: "mdi:home-account" },
];

// The chat show: asked for again this often while a chat runs, and the reasons a chat ends
// that are no failure (chat_show.py).
const CHAT_POLL_MS = 2000;
const CHAT_ENDED_FINE = ["chat_done", "chat_time_up", "chat_stopped"];
// The chat limits the card edits (chat_text.LIMIT_RANGES); "listen" is a checkbox, only for a speaker that can listen.
const CHAT_LIMITS = [
  { key: "max_lines", min: 2, max: 50, step: 1 },
  { key: "words", min: 5, max: 80, step: 1 },
  { key: "pause", min: 0, max: 10, step: 0.5 },
  { key: "minutes", min: 1, max: 30, step: 1 },
];
// The stage card (stage.js) as the page shows it for a chat on "This device": over everything.
const STAGE_CARD = "vam-chat-stage-card";
const STAGE_BOX_STYLE = "position:fixed;inset:0;z-index:1000;background:#000;";
const STAGE_STOP_STYLE =
  "position:absolute;z-index:5;top:calc(12px + env(safe-area-inset-top, 0px));right:12px;padding:10px 18px;" +
  "border:0;border-radius:999px;background:rgba(255,255,255,0.9);color:#000;font:inherit;font-size:16px;";

const STYLE = `
.pair, .limits { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; padding-bottom: 14px; }
.pair .field, .limits .field { border-top: 0; padding: 0; align-content: start; }
.limits { grid-template-columns: repeat(4, minmax(0, 1fr)); padding-top: 14px; border-top: 1px solid var(--vpr-line); }
.limits-head { padding: 14px 0 0; border-top: 1px solid var(--vpr-line); }
.limits-head h3 { margin: 0 0 4px; }
.limits-head + .limits { border-top: 0; }
.listen { display: flex; align-items: flex-start; gap: 10px; padding-bottom: 14px; }
@media (max-width: 600px) { .pair { grid-template-columns: minmax(0, 1fr); } .limits { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.chat-head { display: flex; align-items: center; gap: 12px; }
.chat-head h3 { flex: 1; margin: 0; }
.live { color: var(--vpr-primary); font-size: 13.5px; font-weight: 500; }
.transcript { list-style: none; margin: 12px 0 0; padding: 0; }
.transcript li { padding: 9px 0 9px 14px; border-left: 3px solid var(--tone); margin-top: 6px; }
.transcript li.one { --tone: var(--vpr-primary); }
.transcript li.two { --tone: var(--vpr-text); }
.transcript li.person { --tone: var(--vpr-muted); }
.transcript .by { font-weight: 500; }
:host {
  --vpr-bg: var(--primary-background-color, #f3f4f6);
  --vpr-card: var(--card-background-color, #ffffff);
  --vpr-text: var(--primary-text-color, #1c1e21);
  --vpr-muted: var(--secondary-text-color, #666b73);
  --vpr-line: var(--divider-color, #dfe2e7);
  --vpr-primary: var(--primary-color, #03a9f4);
  --vpr-accent: var(--accent-color, #ff9800);
  --vpr-error: var(--error-color, #d32f2f);
  --vpr-warn: var(--warning-color, #e08a00);
  --vpr-ok: var(--success-color, #2e9d57);
  --vpr-on-primary: var(--text-primary-color, #ffffff);
  --vpr-radius: 14px;
  display: block;
  min-height: 100vh;
  background: var(--vpr-bg);
  color: var(--vpr-text);
  font-family: var(--mdc-typography-font-family, var(--paper-font-body1_-_font-family, Roboto, "Segoe UI", system-ui, sans-serif));
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
* { box-sizing: border-box; }
button, input, select, textarea { font: inherit; color: inherit; }

.bar {
  position: sticky; top: 0; z-index: 5;
  display: flex; align-items: center; gap: 8px;
  height: var(--header-height, 56px); padding: 0 16px 0 4px;
  background: var(--app-header-background-color, var(--vpr-primary));
  color: var(--app-header-text-color, var(--vpr-on-primary));
}
.bar h1 { flex: 1; margin: 0 0 0 12px; font-size: 20px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.shell { display: grid; grid-template-columns: 232px minmax(0, 1fr); gap: 32px; max-width: 1080px; margin: 0 auto; padding: 28px 24px 120px; }
.shell.alone { grid-template-columns: minmax(0, 1fr); max-width: 720px; }
.switcher { grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.switcher .hint { color: var(--vpr-muted); margin-right: 4px; }
.who { flex: none; white-space: nowrap; display: flex; align-items: center; gap: 8px; padding: 5px 16px 5px 5px; border-radius: 999px; border: 1px solid var(--vpr-line); background: var(--vpr-card); cursor: pointer; color: var(--vpr-muted); }
.who:hover { color: var(--vpr-text); }
.who .initial { width: 28px; height: 28px; border-radius: 50%; display: grid; place-items: center; font-size: 14px; font-weight: 500; background: color-mix(in srgb, var(--vpr-muted) 18%, transparent); }
.who[aria-pressed="true"] { border-color: var(--vpr-primary); color: var(--vpr-text); font-weight: 500; box-shadow: 0 0 0 1px var(--vpr-primary); }
.who[aria-pressed="true"] .initial { background: var(--vpr-primary); color: var(--vpr-on-primary); }
nav { position: sticky; top: calc(var(--header-height, 56px) + 28px); align-self: start; display: flex; flex-direction: column; gap: 2px; }
nav button {
  display: flex; align-items: center; gap: 12px; width: 100%;
  padding: 10px 12px; border: 0; border-radius: 10px; background: none;
  text-align: left; cursor: pointer; color: var(--vpr-muted);
}
nav button:hover { background: color-mix(in srgb, var(--vpr-text) 6%, transparent); color: var(--vpr-text); }
nav button[aria-current="true"] { background: color-mix(in srgb, var(--vpr-primary) 14%, transparent); color: var(--vpr-text); font-weight: 500; }
nav button[aria-current="true"] ha-icon { color: var(--vpr-primary); }
nav ha-icon { --mdc-icon-size: 20px; width: 20px; height: 20px; flex: none; }
nav .label { flex: 1; }

main { min-width: 0; overflow-wrap: anywhere; }
main > header { margin-bottom: 20px; }
h2 { margin: 0; font-size: 26px; font-weight: 500; letter-spacing: -0.01em; }
h3 { margin: 0 0 4px; font-size: 16px; font-weight: 500; }
.lede { margin: 6px 0 0; max-width: 62ch; color: var(--vpr-muted); }

.card { background: var(--vpr-card); border-radius: var(--vpr-radius); border: 1px solid var(--vpr-line); padding: 20px; margin-bottom: 16px; }
.card > .intro { margin: 0 0 16px; color: var(--vpr-muted); max-width: 62ch; }
.card > h3 { margin-bottom: 14px; }

.field { display: grid; gap: 6px; padding: 14px 0; border-top: 1px solid var(--vpr-line); }
.field:first-of-type { border-top: 0; padding-top: 0; }
.field:last-child { padding-bottom: 0; }
.field > label, .field > .label { font-weight: 500; }
.help { color: var(--vpr-muted); font-size: 13.5px; max-width: 66ch; }
.help.bad { color: var(--vpr-error); }
.field.switch { grid-template-columns: minmax(0, 1fr) auto; align-items: center; column-gap: 16px; }
.field.switch .help { grid-column: 1; }
.field.switch .toggle { grid-column: 2; grid-row: 1 / span 2; }
.problem { color: var(--vpr-error); font-size: 13.5px; }

input[type="text"], input[type="number"], input[type="time"], select.input, textarea {
  width: 100%; padding: 10px 12px; border-radius: 10px;
  border: 1px solid var(--vpr-line); background: var(--vpr-bg); color: var(--vpr-text);
}
input[type="number"], input[type="time"] { max-width: 160px; }
select.input { max-width: 280px; }
textarea { min-height: 160px; resize: vertical; line-height: 1.45; }
textarea.over { border-color: var(--vpr-error); }
input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible {
  outline: 2px solid var(--vpr-primary); outline-offset: 2px;
}

.toggle { position: relative; width: 46px; height: 26px; flex: none; }
.toggle input { position: absolute; inset: 0; opacity: 0; margin: 0; cursor: pointer; width: 100%; height: 100%; }
.toggle span { position: absolute; inset: 0; border-radius: 999px; background: color-mix(in srgb, var(--vpr-muted) 40%, transparent); transition: background 0.15s; pointer-events: none; }
.toggle span::after { content: ""; position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 50%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,0.3); transition: transform 0.15s; }
.toggle input:checked + span { background: var(--vpr-primary); }
.toggle input:checked + span::after { transform: translateX(20px); }
.toggle input:focus-visible + span { outline: 2px solid var(--vpr-primary); outline-offset: 2px; }

.btn { padding: 9px 18px; border-radius: 999px; border: 1px solid var(--vpr-line); background: var(--vpr-card); cursor: pointer; font-weight: 500; }
.btn:hover { background: color-mix(in srgb, var(--vpr-text) 6%, var(--vpr-card)); }
.btn.primary { background: var(--vpr-primary); border-color: var(--vpr-primary); color: var(--vpr-on-primary); }
.btn.primary:hover { filter: brightness(1.06); }
.btn.danger { color: var(--vpr-error); }
.btn.small { padding: 6px 14px; font-size: 14px; }
.btn:disabled { opacity: 0.55; cursor: default; }
.row-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 18px; }
.row-actions .spacer { flex: 1; }

.segments { display: flex; gap: 8px; flex-wrap: wrap; }
.chip { border: 1px solid var(--vpr-line); background: none; border-radius: 999px; padding: 3px 11px; font-size: 13px; cursor: pointer; color: var(--vpr-muted); white-space: nowrap; }
.chip[aria-pressed="true"] { background: color-mix(in srgb, var(--vpr-accent) 18%, transparent); border-color: transparent; color: var(--vpr-text); }
.segments .chip { padding: 7px 14px; font-size: 14px; }
.segments .chip[aria-pressed="true"] { background: color-mix(in srgb, var(--vpr-primary) 16%, transparent); box-shadow: 0 0 0 1px var(--vpr-primary); }
.tag { display: inline-block; border-radius: 999px; padding: 0 9px; font-size: 12.5px; line-height: 20px; background: color-mix(in srgb, var(--vpr-muted) 16%, transparent); color: var(--vpr-text); white-space: nowrap; }
.tag.mine { background: color-mix(in srgb, var(--vpr-primary) 16%, transparent); }

.facts { list-style: none; margin: 0; padding: 0; }
.facts li { display: flex; align-items: center; gap: 10px; padding: 9px 0; border-top: 1px solid var(--vpr-line); }
.facts li:first-child { border-top: 0; }
.facts .fact { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.icon-btn { border: 0; background: none; cursor: pointer; color: var(--vpr-muted); width: 32px; height: 32px; border-radius: 50%; font-size: 18px; line-height: 1; flex: none; }
.icon-btn:hover { background: color-mix(in srgb, var(--vpr-error) 12%, transparent); color: var(--vpr-error); }
.chip:disabled, .icon-btn:disabled { opacity: 0.55; cursor: default; }
.icon-btn:disabled:hover { background: none; color: var(--vpr-muted); }
.add-fact { display: flex; gap: 10px; margin-top: 12px; }
.add-fact input { flex: 1; min-width: 0; }
.empty { color: var(--vpr-muted); padding: 4px 0; }
.list + .list { border-top: 1px solid var(--vpr-line); margin-top: 16px; padding-top: 16px; }
.list h3 { display: flex; gap: 8px; align-items: baseline; }
.list .count { color: var(--vpr-muted); font-weight: 400; }

.items { list-style: none; margin: 0; padding: 0; }
.item { display: flex; align-items: center; gap: 14px; padding: 12px 0; border-top: 1px solid var(--vpr-line); }
.item:first-child { border-top: 0; padding-top: 0; }
.item:last-child { padding-bottom: 0; }
.item .what { flex: 1; min-width: 0; }
.item .title { font-weight: 500; overflow-wrap: anywhere; }
.item .sub { color: var(--vpr-muted); font-size: 13.5px; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
.item .side { flex: none; max-width: 45%; display: flex; justify-content: flex-end; }
.item .side .btn { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item .side .help { text-align: right; }

.notice { border-radius: 10px; padding: 12px 14px; margin-bottom: 16px; background: color-mix(in srgb, var(--vpr-warn) 14%, transparent); }
.notice.error { background: color-mix(in srgb, var(--vpr-error) 12%, transparent); }
.notice.info { background: color-mix(in srgb, var(--vpr-primary) 10%, transparent); }
.card > .notice:last-child { margin-bottom: 0; }
.result { font-size: 14px; }
.result.ok { color: var(--vpr-ok); }
.result.error { color: var(--vpr-error); }
.when-row { display: flex; gap: 12px; flex-wrap: wrap; align-items: start; margin-top: 10px; }
.when-row .field { border-top: 0; padding: 0; }

.facts-of { display: grid; gap: 2px; margin: 0; }
.facts-of dt { font-weight: 500; }
.facts-of dd { margin: 0 0 14px; }
.facts-of dd:last-child { margin-bottom: 0; }
.toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 7; background: var(--vpr-text); color: var(--vpr-card); padding: 10px 18px; border-radius: 999px; max-width: calc(100vw - 32px); }
.loading { padding: 80px 24px; text-align: center; color: var(--vpr-muted); }
.unlinked { max-width: 560px; margin: 56px auto; text-align: center; }
.unlinked .avatar { width: 56px; height: 56px; border-radius: 50%; margin: 0 auto 14px; border: 2px dashed color-mix(in srgb, var(--vpr-muted) 60%, transparent); }
.unlinked p { color: var(--vpr-muted); margin: 6px 0 0; }

/* Favorites, as on the settings page. */
.fav-list + .fav-list { border-top: 1px solid var(--vpr-line); margin-top: 18px; padding-top: 18px; }
.fav-list > h3 { display: flex; gap: 8px; align-items: baseline; margin-bottom: 8px; }
.fav-list .count { color: var(--vpr-muted); font-weight: 400; }
.fav-list > .notice:last-child { margin-bottom: 0; }
.favs { list-style: none; margin: 0; padding: 0; }
.fav { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-top: 1px solid var(--vpr-line); }
.fav:first-child { border-top: 0; }
.fav .what { flex: 1; min-width: 0; }
.fav .title { font-weight: 500; overflow-wrap: anywhere; }
.fav .sub { color: var(--vpr-muted); font-size: 13.5px; display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px; }
.fav .side { flex: none; display: flex; gap: 8px; }
.fav.editing { display: block; }
.fav-form { border: 1px solid var(--vpr-primary); border-radius: 12px; padding: 14px 16px; margin-top: 12px; background: color-mix(in srgb, var(--vpr-primary) 4%, var(--vpr-card)); }
.fav.editing .fav-form { margin-top: 0; }
.fav-form > h3 { margin-bottom: 10px; }
.fav-form .row-actions { margin-top: 14px; }
.fav-form .favs { margin-top: 10px; }
.fav-form .problem { margin-top: 10px; }
.fav-form .empty { margin-top: 10px; }
.find { display: flex; gap: 10px; }
.find input { flex: 1; min-width: 0; }
.fav-list > .row-actions { margin-top: 12px; }

@media (max-width: 860px) {
  .shell, .shell.alone { grid-template-columns: minmax(0, 1fr); gap: 12px; padding: 12px 12px 120px; }
  /* One row each, scrolling sideways, so the settings start near the top of a phone. */
  .switcher, nav {
    flex-direction: row; flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none;
    margin: 0 -12px; padding: 0 12px;
    -webkit-mask-image: linear-gradient(to right, #000 calc(100% - 28px), transparent);
    mask-image: linear-gradient(to right, #000 calc(100% - 28px), transparent);
  }
  .switcher::-webkit-scrollbar, nav::-webkit-scrollbar { display: none; }
  .switcher::after, nav::after { content: ""; flex: none; width: 20px; }
  .switcher { gap: 6px; }
  .switcher .hint { display: none; }
  .who { padding: 4px 12px 4px 4px; font-size: 14px; }
  .who .initial { width: 24px; height: 24px; font-size: 12px; }
  nav { position: sticky; top: var(--header-height, 56px); z-index: 4; gap: 4px; padding-top: 6px; padding-bottom: 6px; background: var(--vpr-bg); }
  nav button { width: auto; flex: none; white-space: nowrap; padding: 7px 12px; gap: 6px; font-size: 14px; }
  nav ha-icon { display: none; }
  nav .label { flex: none; }
  .card { padding: 16px; }
  h2 { font-size: 22px; }
  .lede { font-size: 14px; }
  .field.switch { column-gap: 12px; }
  input[type="number"], input[type="time"] { max-width: 130px; }
  select.input { max-width: none; }
  .item { flex-wrap: wrap; gap: 8px; }
  .item .what { flex-basis: 100%; }
  .item .side { max-width: 100%; justify-content: flex-start; }
  .item .side .help { text-align: left; }
  .unlinked { margin: 32px auto; }
  .fav { flex-wrap: wrap; gap: 8px; }
  .fav .what { flex-basis: 100%; }
  /* A search result keeps its Add button beside it. */
  .fav-form .fav { flex-wrap: nowrap; }
  .fav-form .fav .what { flex-basis: auto; }
  .fav-form { padding: 12px; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
`;

class VoicePersonaRouterMine extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._data = null; // the answer of me/get
    this._failed = "";
    this._routerId = null;
    this._section = "persona";
    this._personas = {}; // the persona being written, by router, until it is saved
    this._personaBusy = false;
    this._personaError = "";
    this._reminders = null; // { entryId, loading, reminders, unreadable, error }
    this._remindersGen = 0; // bumped on every fetch, so an older answer is dropped
    this._deleting = ""; // the key of the reminder being deleted
    this._deleteError = "";
    this._form = this._newForm();
    this._adding = false;
    this._addResult = null; // { ok, text } after Add reminder
    this._toastText = "";
    this._toastTimer = null;
    // One memory edit at a time: each one is built from the lists as the server last sent
    // them, so a second edit sent before the first is answered would undo the first.
    this._memoryBusy = false;
    this._factDrafts = {}; // the fact being typed, by list, kept while a save is in flight
    // Favorites: the own list and the household's, the same on every assistant. One change at
    // a time, as for memory: each answer replaces a whole list.
    this._fav = null; // { loading, error, state, lists: { own, household } } from favorites/get
    this._favGen = 0; // bumped on every fetch, so an older answer is dropped
    this._favBusy = false;
    this._favForm = null; // the one open form: { list, mode: "edit" | "search" | "stream", ... }
    this._favError = null; // { list, text } after a refused remove
    // The chat show: the last chat/state answer, the choices on the card, and the poll.
    // busy names the command under way ("start" or "stop").
    this._chat = { state: null, form: null, loading: false, busy: "", error: "", loadError: "", timer: null };
    // A chat on "This device": the id this page watches the stage under, the speaker that
    // stands for it (stage.py), and the stage card over the page while such a chat runs.
    this._viewer = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
    this._here = `page:${this._viewer}`;
    this._stage = null; // { box, card, lock }
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
    if (this._stage) this._stage.card.hass = hass;
    if (first) this._load();
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.narrow = narrow;
  }

  connectedCallback() {
    if (!this.shadowRoot.firstChild) this._render();
    else if (this._section === "chat") this._loadChat();
  }

  disconnectedCallback() {
    this._stopChatPoll();
    this._closeStage();
  }

  // Data.

  _call(command, data) {
    return this._hass.callWS({ type: `${WS}/${command}`, ...(data || {}) });
  }

  async _load() {
    try {
      this._data = await this._call("get");
      const ids = this._data.routers.map((router) => router.entry_id);
      if (!ids.includes(this._routerId)) this._routerId = ids[0] || null;
      this._failed = "";
    } catch (err) {
      // Without an answer there are no texts from en.json yet, so this one is written here.
      this._failed = `This page could not be loaded: ${reason(err)}`;
    }
    this._render();
    if (this._router) this._loadReminders();
    if (this._router && this._section === "favorites") this._loadFavorites();
    if (this._router && this._section === "chat") this._loadChat();
  }

  get _router() {
    return this._data ? this._data.routers.find((r) => r.entry_id === this._routerId) : null;
  }

  async _loadReminders() {
    const entryId = this._routerId;
    const gen = ++this._remindersGen;
    this._reminders = { entryId, loading: true, reminders: [], unreadable: [], error: "" };
    this._render();
    let next;
    try {
      const result = await this._call("reminders", { entry_id: entryId });
      next = result.ok === false
        ? { entryId, reminders: [], unreadable: [], error: refusal(result) }
        : { entryId, reminders: result.reminders || [], unreadable: result.unreadable || [], error: "" };
    } catch (err) {
      next = { entryId, reminders: [], unreadable: [], error: this._t("failed", { reason: reason(err) }) };
    }
    if (gen !== this._remindersGen) return;
    this._reminders = next;
    this._render();
  }

  // The person may switch to another router while a save, add or delete is on its way.
  // Its answer is about the router it was sent for, so it is only shown on that router.
  _stillOn(entryId) {
    return entryId === this._routerId;
  }

  async _savePersona() {
    const router = this._router;
    const entryId = router.entry_id;
    const persona = this._personaText(router);
    this._personaBusy = true;
    this._personaError = "";
    this._render();
    let error = "";
    try {
      const result = await this._call("save_persona", { entry_id: entryId, persona });
      if (result.ok) {
        router.persona = persona.trim();
        delete this._personas[entryId];
        if (this._stillOn(entryId)) this._toast(this._t("persona.saved"));
      } else {
        error = refusal(result);
      }
    } catch (err) {
      error = this._t("failed", { reason: reason(err) });
    }
    this._personaBusy = false;
    if (this._stillOn(entryId)) this._personaError = error;
    this._render();
  }

  async _saveFacts(list, items, added) {
    if (this._memoryBusy) return;
    this._memoryBusy = true;
    this._render();
    const lines = items.map((item) => (item.private ? `${PRIVATE_PREFIX} ${item.text}` : item.text));
    try {
      const result = await this._call("save_memory", {
        entry_id: this._routerId,
        key: list.key,
        shown: list.items.map((item) => item.id),
        lines,
      });
      if (result.ok) {
        list.items = result.items;
        if (added) delete this._factDrafts[list.key];
      } else {
        this._toast(refusal(result));
      }
    } catch (err) {
      this._toast(this._t("failed", { reason: reason(err) }));
    }
    this._memoryBusy = false;
    this._render();
  }

  async _deleteReminder(reminder) {
    const entryId = this._routerId;
    this._deleting = this._reminderKey(reminder);
    this._deleteError = "";
    this._render();
    let ok = false;
    let error = "";
    try {
      const result = await this._call("delete_reminder", {
        entry_id: entryId,
        calendar: reminder.calendar,
        uid: reminder.uid,
        recurrence_id: reminder.recurrence_id || null,
      });
      ok = Boolean(result.ok);
      if (!ok) error = refusal(result);
    } catch (err) {
      error = this._t("failed", { reason: reason(err) });
    }
    this._deleting = "";
    if (!this._stillOn(entryId)) {
      this._render();
      return;
    }
    if (ok) this._toast(this._t("reminders.deleted"));
    this._deleteError = error;
    await this._loadReminders();
  }

  async _addReminder() {
    const form = this._form;
    const entryId = this._routerId;
    const data = { entry_id: entryId, scope: form.scope, title: form.title.trim(), announce: form.announce };
    if (form.mode === "minutes") {
      const minutes = Number(form.minutes);
      // A blank or broken number is left out, so the server answers why it cannot be used.
      if (form.minutes !== "" && Number.isInteger(minutes)) data.in_minutes = minutes;
    } else {
      if (form.time) data.at_time = form.time;
      if (form.day) data.on_day = form.day;
    }
    this._adding = true;
    this._addResult = null;
    this._render();
    let added;
    try {
      const result = await this._call("add_reminder", data);
      if (result.ok === false) {
        added = { ok: false, text: refusal(result) };
      } else if (result.status && result.status !== "ok") {
        added = { ok: false, text: result.message || this._t("failed", { reason: result.status }) };
      } else {
        added = { ok: true, text: result.message || this._t("reminders.added") };
      }
    } catch (err) {
      added = { ok: false, text: this._t("failed", { reason: reason(err) }) };
    }
    this._adding = false;
    if (!this._stillOn(entryId)) {
      this._render();
      return;
    }
    this._addResult = added;
    if (added.ok) {
      this._form = { ...this._newForm(), scope: form.scope, announce: form.announce };
      await this._loadReminders();
    } else {
      this._render();
    }
  }

  _newForm() {
    return { title: "", scope: "me", announce: true, mode: "minutes", minutes: "", time: "", day: "" };
  }

  _personaText(router) {
    const draft = this._personas[router.entry_id];
    return draft === undefined ? router.persona : draft;
  }

  _reminderKey(reminder) {
    return [reminder.calendar, reminder.uid, reminder.recurrence_id || "", reminder.start].join("|");
  }

  _pickRouter(id) {
    if (id === this._routerId) return;
    this._routerId = id;
    this._personaError = "";
    this._deleteError = "";
    this._addResult = null;
    this._form = this._newForm();
    this._loadReminders();
  }

  _go(section) {
    this._section = section;
    window.scrollTo({ top: 0 });
    // Reminders may have been added by voice since the page opened.
    if (section === "reminders") this._loadReminders();
    // Favorites may have been added by voice too.
    else if (section === "favorites") this._loadFavorites();
    else this._render();
    if (section === "chat") this._loadChat();
    else this._stopChatPoll();
  }

  _toast(text) {
    this._toastText = text;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toastText = "";
      this._renderToast();
    }, 2600);
  }

  // Texts, from options.mine.* in translations/en.json.

  _text(key, fallback) {
    return (this._data && this._data.strings && this._data.strings[key]) || fallback || "";
  }

  /** A text of this page with its {placeholders} filled in. */
  _t(key, vars, fallback) {
    let text = this._text(`mine.${key}`, fallback || key);
    for (const [name, value] of Object.entries(vars || {})) text = text.split(`{${name}}`).join(String(value));
    return text;
  }

  // Drawing.

  _render() {
    const root = this.shadowRoot;
    const focus = root.activeElement && root.activeElement.dataset.key;
    root.replaceChildren(h("style", {}, STYLE), this._bar());
    if (this._failed) {
      root.append(h("div", { class: "loading" }, this._failed));
    } else if (!this._data) {
      // Shown before the texts of en.json have arrived.
      root.append(h("div", { class: "loading" }, "Loading…"));
    } else if (!this._router) {
      root.append(h("div", { class: "shell alone" }, h("main", {}, this._unlinked())));
    } else {
      root.append(h("div", { class: "shell" }, this._switcher(), this._nav(), this._main()));
    }
    root.append(h("div", { id: "floating" }));
    this._renderToast();
    if (focus) {
      const again = root.querySelector(`[data-key="${focus}"]`);
      if (again) {
        again.focus();
        if (again.tagName === "TEXTAREA" || again.type === "text") {
          const end = again.value.length;
          again.setSelectionRange(end, end);
        }
      }
    }
    // On a phone the sections are one scrolling row: keep the open one in sight.
    const nav = root.querySelector("nav");
    const current = nav && nav.querySelector('[aria-current="true"]');
    if (current && nav.scrollWidth > nav.clientWidth) {
      nav.scrollLeft = current.offsetLeft - (nav.clientWidth - current.offsetWidth) / 2;
    }
  }

  _renderToast() {
    const holder = this.shadowRoot.getElementById("floating");
    if (!holder) return;
    if (this._toastText) holder.replaceChildren(h("div", { class: "toast", role: "status" }, this._toastText));
    else holder.replaceChildren();
  }

  _bar() {
    const menu = h("ha-menu-button", {});
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    return h("div", { class: "bar" }, menu, h("h1", {}, this._t("title", {}, "My assistant")));
  }

  _unlinked() {
    return h(
      "div",
      { class: "card unlinked" },
      h("div", { class: "avatar" }),
      h("h3", {}, this._t("unlinked_title")),
      h("p", {}, this._t("unlinked"))
    );
  }

  /** One button per assistant this person is linked to, when there is more than one. */
  _switcher() {
    const routers = this._data.routers;
    if (routers.length < 2) return null;
    return h(
      "div",
      { class: "switcher", role: "group", "aria-label": this._t("routers") },
      h("span", { class: "hint" }, this._t("routers")),
      routers.map((router) =>
        h(
          "button",
          {
            class: "who",
            "aria-pressed": String(router.entry_id === this._routerId),
            onclick: () => this._pickRouter(router.entry_id),
          },
          h("span", { class: "initial" }, router.title.slice(0, 1).toUpperCase()),
          router.title
        )
      )
    );
  }

  _nav() {
    return h(
      "nav",
      { "aria-label": this._t("title") },
      SECTIONS.map((section) =>
        h(
          "button",
          { "aria-current": String(section.id === this._section), onclick: () => this._go(section.id) },
          h("ha-icon", { icon: section.icon }),
          h("span", { class: "label" }, this._t(`sections.${section.id}`))
        )
      )
    );
  }

  _main() {
    const draw = {
      persona: () => this._persona(),
      reminders: () => this._remindersView(),
      memory: () => this._memory(),
      favorites: () => this._favorites(),
      chat: () => this._chatShow(),
      shared: () => this._shared(),
    }[this._section];
    return h("main", {}, draw());
  }

  _head(id, lede) {
    return h("header", {}, h("h2", {}, this._t(`sections.${id}`)), lede && h("p", { class: "lede" }, lede));
  }

  // Persona.

  _persona() {
    const router = this._router;
    const text = this._personaText(router);
    const over = text.trim().length > MAX_PERSONA;
    const changed = text.trim() !== router.persona.trim();
    const count = h(
      "div",
      { class: `help${over ? " bad" : ""}`, "aria-live": "polite" },
      over ? this._text("error.mine_persona_too_long") : this._t("persona.count", { count: text.length, max: MAX_PERSONA })
    );
    const save = h(
      "button",
      { class: "btn primary", disabled: this._personaBusy || over || !changed, onclick: () => this._savePersona() },
      this._personaBusy ? this._t("persona.saving") : this._t("persona.save")
    );
    const area = h("textarea", {
      id: "persona",
      class: over ? "over" : "",
      value: text,
      placeholder: this._t("persona.placeholder"),
      oninput: (ev) => {
        this._personas[router.entry_id] = ev.target.value;
        const now = ev.target.value;
        const tooLong = now.trim().length > MAX_PERSONA;
        ev.target.classList.toggle("over", tooLong);
        count.className = `help${tooLong ? " bad" : ""}`;
        count.textContent = tooLong
          ? this._text("error.mine_persona_too_long")
          : this._t("persona.count", { count: now.length, max: MAX_PERSONA });
        save.disabled = this._personaBusy || tooLong || now.trim() === router.persona.trim();
      },
    });
    area.dataset.key = "persona";
    return [
      this._head("persona", this._t("persona.lede", { title: router.title })),
      h(
        "div",
        { class: "card" },
        h("div", { class: "field" }, h("label", { for: "persona" }, this._t("persona.label")), area, count),
        this._personaError && h("div", { class: "problem", role: "alert" }, this._personaError),
        h("div", { class: "row-actions" }, save)
      ),
    ];
  }

  // Reminders.

  _remindersView() {
    const state = this._reminders && this._reminders.entryId === this._routerId ? this._reminders : null;
    return [
      this._head("reminders", this._t("reminders.lede")),
      h("div", { class: "notice info" }, this._t("reminders.calendars_note")),
      h("div", { class: "card" }, this._reminderList(state)),
      this._router.can_add === false
        ? h("div", { class: "notice" }, this._t("reminders.no_script"))
        : this._addForm(),
    ];
  }

  _reminderList(state) {
    if (!state || state.loading) return h("div", { class: "empty" }, this._t("reminders.loading"));
    const parts = [];
    if (state.error) parts.push(h("div", { class: "notice error", role: "alert" }, state.error));
    if (this._deleteError) parts.push(h("div", { class: "notice error", role: "alert" }, this._deleteError));
    for (const calendar of state.unreadable) {
      parts.push(h("div", { class: "notice" }, this._t("reminders.unreadable", { calendar: this._calendarName(calendar) })));
    }
    if (state.reminders.length) {
      parts.push(h("ul", { class: "items" }, state.reminders.map((reminder) => this._reminderRow(reminder))));
    } else if (!state.error) {
      parts.push(h("div", { class: "empty" }, this._t("reminders.empty")));
    }
    return parts;
  }

  _calendarName(entityId) {
    const state = this._hass && this._hass.states && this._hass.states[entityId];
    const name = state && state.attributes && state.attributes.friendly_name;
    return name ? `${name} (${entityId})` : entityId;
  }

  _reminderRow(reminder) {
    const key = this._reminderKey(reminder);
    const busy = this._deleting === key;
    const title = reminder.title || "";
    const side = reminder.can_delete
      ? h(
          "button",
          {
            class: "btn small danger",
            disabled: !!this._deleting,
            title: this._t("reminders.delete", { title }),
            onclick: () => this._deleteReminder(reminder),
          },
          busy ? this._t("reminders.deleting") : this._t("reminders.delete", { title })
        )
      : h("div", { class: "help" }, this._t("reminders.cannot_delete"));
    return h(
      "li",
      { class: "item" },
      h(
        "div",
        { class: "what" },
        h("div", { class: "title" }, title),
        h(
          "div",
          { class: "sub" },
          h("span", { class: `tag${reminder.whose === "mine" ? " mine" : ""}` }, this._t(`reminders.${reminder.whose === "mine" ? "mine" : "household"}`)),
          h("span", {}, this._when(reminder)),
          h("span", {}, reminder.calendar_name)
        )
      ),
      h("div", { class: "side" }, side)
    );
  }

  /** When a reminder is due, in the browser's language. An all-day one has a date only. */
  _when(reminder) {
    if (reminder.all_day) {
      const [year, month, day] = String(reminder.start).slice(0, 10).split("-").map(Number);
      const date = new Date(year, month - 1, day);
      if (Number.isNaN(date.getTime())) return reminder.start;
      const text = date.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
      return `${text}, ${this._t("reminders.all_day")}`;
    }
    const at = new Date(reminder.start);
    if (Number.isNaN(at.getTime())) return reminder.start;
    return at.toLocaleString(undefined, { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  }

  _addForm() {
    const form = this._form;
    const set = (field, redraw) => (ev) => {
      form[field] = ev.target.type === "checkbox" ? ev.target.checked : ev.target.value;
      if (redraw) this._render();
    };
    const segment = (field, value, label) =>
      h(
        "button",
        {
          class: "chip",
          type: "button",
          "aria-pressed": String(form[field] === value),
          onclick: () => {
            form[field] = value;
            this._render();
          },
        },
        label
      );
    const title = h("input", {
      type: "text",
      id: "rem-title",
      value: form.title,
      maxLength: MAX_TITLE,
      placeholder: this._t("reminders.what_placeholder"),
      oninput: set("title"),
      onkeydown: (ev) => {
        if (ev.key === "Enter" && !this._adding) this._addReminder();
      },
    });
    title.dataset.key = "rem-title";
    const when =
      form.mode === "minutes"
        ? h(
            "div",
            { class: "when-row" },
            h(
              "div",
              { class: "field" },
              h("label", { for: "rem-minutes" }, this._t("reminders.minutes")),
              h("input", { type: "number", id: "rem-minutes", min: 1, step: 1, placeholder: "20", value: form.minutes, oninput: set("minutes") })
            )
          )
        : h(
            "div",
            { class: "when-row" },
            h(
              "div",
              { class: "field" },
              h("label", { for: "rem-time" }, this._t("reminders.time")),
              h("input", { type: "time", id: "rem-time", value: form.time, oninput: set("time") })
            ),
            h(
              "div",
              { class: "field" },
              h("label", { for: "rem-day" }, this._t("reminders.day")),
              h(
                "select",
                { class: "input", id: "rem-day", onchange: set("day") },
                h("option", { value: "", selected: form.day === "" }, this._t("reminders.day_next")),
                DAYS.map((day) => h("option", { value: day, selected: form.day === day }, this._t(`reminders.days.${day}`)))
              )
            )
          );
    const announce = h("input", { type: "checkbox", id: "rem-announce", checked: form.announce, onchange: set("announce") });
    return h(
      "div",
      { class: "card" },
      h("h3", {}, this._t("reminders.add_title")),
      h(
        "div",
        { class: "field" },
        h("label", { for: "rem-title" }, this._t("reminders.what")),
        title
      ),
      h(
        "div",
        { class: "field" },
        h("div", { class: "label" }, this._t("reminders.for")),
        h(
          "div",
          { class: "segments", role: "group", "aria-label": this._t("reminders.for") },
          segment("scope", "me", this._t("reminders.for_me")),
          segment("scope", "household", this._t("reminders.for_household"))
        )
      ),
      h(
        "div",
        { class: "field" },
        h("div", { class: "label" }, this._t("reminders.when")),
        h(
          "div",
          { class: "segments", role: "group", "aria-label": this._t("reminders.when") },
          segment("mode", "minutes", this._t("reminders.in_minutes")),
          segment("mode", "time", this._t("reminders.at_time"))
        ),
        when
      ),
      h(
        "div",
        { class: "field switch" },
        h("label", { for: "rem-announce" }, this._t("reminders.announce")),
        h("div", { class: "toggle" }, announce, h("span", {})),
        h("div", { class: "help" }, this._t("reminders.announce_help"))
      ),
      h(
        "div",
        { class: "row-actions" },
        h(
          "button",
          { class: "btn primary", disabled: this._adding, onclick: () => this._addReminder() },
          this._adding ? this._t("reminders.adding") : this._t("reminders.add")
        ),
        this._addResult &&
          h(
            "span",
            { class: `result ${this._addResult.ok ? "ok" : "error"}`, role: this._addResult.ok ? "status" : "alert" },
            this._addResult.text
          )
      )
    );
  }

  // Memory: the person's own two lists, edited as on the settings page.

  _memory() {
    const router = this._router;
    return [
      this._head("memory", this._t("memory.lede", { title: router.title })),
      h(
        "div",
        { class: "card" },
        router.memory.length
          ? router.memory.map((list) => this._factList(list))
          : h("div", { class: "empty" }, this._t("memory.none"))
      ),
    ];
  }

  _listName(list) {
    return list.key.startsWith("global:")
      ? this._t("memory.global")
      : this._t("memory.entry", { title: this._router.title });
  }

  _factList(list) {
    const name = this._listName(list);
    const input = h("input", {
      type: "text",
      placeholder: this._t("memory.add_placeholder"),
      "aria-label": this._t("memory.add_named", { list: name }),
      maxLength: 200,
      value: this._factDrafts[list.key] || "",
      oninput: (ev) => {
        this._factDrafts[list.key] = ev.target.value;
      },
    });
    input.dataset.key = `fact:${list.key}`;
    const busy = this._memoryBusy;
    const add = () => {
      const text = input.value.trim();
      if (text && !this._memoryBusy) this._saveFacts(list, [...list.items, { text }], true);
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") add();
    });
    return h(
      "section",
      { class: "list" },
      h("h3", {}, name, h("span", { class: "count" }, `${list.items.length}`)),
      list.items.length
        ? h(
            "ul",
            { class: "facts" },
            list.items.map((item) =>
              h(
                "li",
                {},
                h("span", { class: "fact" }, item.text),
                h(
                  "button",
                  {
                    class: "chip",
                    disabled: busy,
                    "aria-pressed": String(!!item.private),
                    title: item.private ? this._t("memory.private_help") : this._t("memory.shared_help"),
                    onclick: () =>
                      this._saveFacts(
                        list,
                        list.items.map((other) => (other.id === item.id ? { ...other, private: !item.private } : other))
                      ),
                  },
                  item.private ? this._t("memory.private") : this._t("memory.shared")
                ),
                h(
                  "button",
                  {
                    class: "icon-btn",
                    disabled: busy,
                    "aria-label": this._t("memory.forget_named", { text: item.text }),
                    title: this._t("memory.forget"),
                    onclick: () => this._saveFacts(list, list.items.filter((other) => other.id !== item.id)),
                  },
                  "×"
                )
              )
            )
          )
        : h("div", { class: "empty" }, this._t("memory.empty")),
      h("div", { class: "add-fact" }, input, h("button", { class: "btn", disabled: busy, onclick: add }, this._t("memory.add")))
    );
  }

  // Favorites: the person's own list, unless the assistants disagree who they are, and the
  // household's. Both are edited as on the settings page.

  async _loadFavorites() {
    const gen = ++this._favGen;
    // Lists already on screen stay there while they are fetched again.
    if (!this._fav || !this._fav.lists) {
      this._fav = { loading: true, error: "" };
      this._render();
    }
    let next;
    try {
      const result = await this._call("favorites/get");
      next = result.ok === false
        ? { error: refusal(result) }
        : { error: "", state: result.own_state, lists: { own: result.own || [], household: result.household || [] } };
    } catch (err) {
      next = { error: this._t("failed", { reason: reason(err) }) };
    }
    if (gen !== this._favGen) return;
    this._fav = { loading: false, ...next };
    this._render();
  }

  /**
   * Send one favorites command for a list. Its answer holds the whole list. A refusal is shown
   * in the open form it came from, else above the list.
   */
  async _changeFavorite(list, command, payload, form, done) {
    if (this._favBusy) return;
    this._favBusy = true;
    this._favError = null;
    if (form) form.error = "";
    this._render();
    let text = "";
    try {
      const result = await this._call(`favorites/${command}`, { list, ...payload });
      if (result.ok) {
        this._fav.lists[list] = result.items;
        if (form && this._favForm === form) this._favForm = null;
        this._toast(this._t(done));
      } else {
        text = refusal(result);
      }
    } catch (err) {
      text = this._t("failed", { reason: reason(err) });
    }
    if (text && form) form.error = text;
    else if (text) this._favError = { list, text };
    this._favBusy = false;
    this._render();
  }

  async _searchFavorites(form) {
    const name = form.query.trim();
    if (!name || form.searching) return;
    form.searching = true;
    form.error = "";
    form.results = null;
    form.message = "";
    this._render();
    try {
      const result = await this._call("favorites/search", { name });
      if (result.ok === false) form.error = refusal(result);
      form.results = result.candidates || [];
      form.message = result.message || "";
    } catch (err) {
      form.error = this._t("failed", { reason: reason(err) });
    }
    form.searching = false;
    this._render();
  }

  _openFavoriteForm(form) {
    this._favForm = { error: "", ...form };
    this._favError = null;
    this._render();
  }

  _favorites() {
    const fav = this._fav;
    const head = this._head("favorites", this._t("favorites.lede"));
    if (!fav || fav.loading) return [head, h("div", { class: "card" }, h("div", { class: "empty" }, this._t("favorites.loading")))];
    if (!fav.lists) return [head, h("div", { class: "notice error", role: "alert" }, fav.error)];
    // While favorites are off, the household's list plays nothing either.
    if (fav.state === "off") return [head, h("div", { class: "notice" }, this._t("favorites.off"))];
    return [
      head,
      fav.error && h("div", { class: "notice error", role: "alert" }, fav.error),
      h(
        "div",
        { class: "card" },
        fav.state === "conflict"
          ? h("section", { class: "fav-list" }, h("h3", {}, this._t("favorites.own")), h("div", { class: "notice" }, this._t("favorites.conflict")))
          : this._favoriteList("own", this._t("favorites.own")),
        this._favoriteList("household", this._t("favorites.household"))
      ),
    ];
  }

  _kind(kind) {
    return this._t(`favorites.kinds.${kind}`, {}, kind);
  }

  _favoriteList(list, name) {
    const items = this._fav.lists[list] || [];
    const form = this._favForm && this._favForm.list === list ? this._favForm : null;
    const error = this._favError && this._favError.list === list ? this._favError.text : "";
    const busy = this._favBusy;
    return h(
      "section",
      { class: "fav-list" },
      h("h3", {}, name, h("span", { class: "count" }, `${items.length}`)),
      error && h("div", { class: "notice error", role: "alert" }, error),
      items.length
        ? h(
            "ul",
            { class: "favs" },
            items.map((item) =>
              form && form.mode === "edit" && form.id === item.id
                ? h("li", { class: "fav editing" }, this._favoriteEditor(form))
                : this._favoriteRow(list, item)
            )
          )
        : h("div", { class: "empty" }, this._t("favorites.empty")),
      form && form.mode === "search" && this._favoriteFinder(form),
      form && form.mode === "stream" && this._favoriteStream(form),
      !(form && form.mode !== "edit") &&
        h(
          "div",
          { class: "row-actions" },
          h(
            "button",
            { class: "btn small", disabled: busy, onclick: () => this._openFavoriteForm({ list, mode: "search", query: "", results: null, message: "", searching: false }) },
            this._t("favorites.find")
          ),
          h(
            "button",
            { class: "btn small", disabled: busy, onclick: () => this._openFavoriteForm({ list, mode: "stream", name: "", url: "" }) },
            this._t("favorites.stream")
          )
        )
    );
  }

  _favoriteRow(list, item) {
    const busy = this._favBusy;
    return h(
      "li",
      { class: "fav" },
      h(
        "div",
        { class: "what" },
        h("div", { class: "title" }, item.name),
        h(
          "div",
          { class: "sub" },
          h("span", { class: "tag" }, this._kind(item.kind)),
          item.aliases.length > 0 && h("span", {}, this._t("favorites.also", { names: item.aliases.join(", ") }))
        )
      ),
      h(
        "div",
        { class: "side" },
        h(
          "button",
          {
            class: "btn small",
            disabled: busy,
            "aria-label": this._t("favorites.edit_named", { name: item.name }),
            onclick: () => this._openFavoriteForm({ list, mode: "edit", id: item.id, name: item.name, aliases: item.aliases.join(", ") }),
          },
          this._t("favorites.edit")
        ),
        h(
          "button",
          {
            class: "btn small danger",
            disabled: busy,
            "aria-label": this._t("favorites.remove_named", { name: item.name }),
            onclick: () => {
              if (window.confirm(this._t("favorites.remove_confirm", { name: item.name }))) {
                this._changeFavorite(list, "remove", { favorite_id: item.id }, null, "favorites.removed");
              }
            },
          },
          this._t("favorites.remove")
        )
      )
    );
  }

  /** A text box of an open favorites form, kept in the form while typing. */
  _favoriteInput(form, field, extra) {
    const input = h("input", {
      type: "text",
      id: `fav-${field}`,
      value: form[field] || "",
      oninput: (ev) => {
        form[field] = ev.target.value;
      },
      ...extra,
    });
    input.dataset.key = `fav-${field}`;
    return input;
  }

  _favoriteFormEnd(form, saveLabel, save) {
    return [
      form.error && h("div", { class: "problem", role: "alert" }, form.error),
      h(
        "div",
        { class: "row-actions" },
        save && h("button", { class: "btn primary", disabled: this._favBusy, onclick: save }, this._favBusy ? this._t("favorites.saving") : saveLabel),
        h(
          "button",
          {
            class: "btn",
            onclick: () => {
              this._favForm = null;
              this._render();
            },
          },
          save ? this._t("favorites.cancel") : this._t("favorites.close")
        )
      ),
    ];
  }

  _favoriteEditor(form) {
    const save = () => {
      const aliases = form.aliases.split(",").map((alias) => alias.trim()).filter(Boolean);
      const data = { name: form.name.trim(), aliases };
      this._changeFavorite(form.list, "update", { favorite_id: form.id, data }, form, "favorites.saved");
    };
    return h(
      "div",
      { class: "fav-form" },
      h("h3", {}, this._t("favorites.edit_title")),
      h("div", { class: "field" }, h("label", { for: "fav-name" }, this._t("favorites.name")), this._favoriteInput(form, "name", { maxLength: MAX_FAVORITE_NAME })),
      h(
        "div",
        { class: "field" },
        h("label", { for: "fav-aliases" }, this._t("favorites.aliases")),
        this._favoriteInput(form, "aliases", { placeholder: this._t("favorites.aliases_placeholder") }),
        h("div", { class: "help" }, this._t("favorites.aliases_help"))
      ),
      this._favoriteFormEnd(form, this._t("favorites.save"), save)
    );
  }

  _favoriteFinder(form) {
    const query = this._favoriteInput(form, "query", {
      maxLength: MAX_FAVORITE_NAME,
      placeholder: this._t("favorites.find_placeholder"),
      "aria-label": this._t("favorites.find_label"),
      onkeydown: (ev) => {
        if (ev.key === "Enter") this._searchFavorites(form);
      },
    });
    const add = (candidate) =>
      this._changeFavorite(
        form.list,
        "add",
        { data: { name: candidate.name, kind: candidate.kind, uri: candidate.uri, image: candidate.image || "" } },
        form,
        "favorites.added"
      );
    const results = form.results || [];
    return h(
      "div",
      { class: "fav-form" },
      h("h3", {}, this._t("favorites.find")),
      h(
        "div",
        { class: "find" },
        query,
        h(
          "button",
          { class: "btn", disabled: form.searching, onclick: () => this._searchFavorites(form) },
          form.searching ? this._t("favorites.searching") : this._t("favorites.search")
        )
      ),
      form.message && h("div", { class: "empty" }, form.message),
      results.length > 0 &&
        h(
          "ul",
          { class: "favs" },
          results.map((candidate) =>
            h(
              "li",
              { class: "fav" },
              h(
                "div",
                { class: "what" },
                h("div", { class: "title" }, candidate.name),
                h("div", { class: "sub" }, h("span", { class: "tag" }, this._kind(candidate.kind)))
              ),
              h(
                "div",
                { class: "side" },
                h(
                  "button",
                  {
                    class: "btn small primary",
                    disabled: this._favBusy,
                    "aria-label": this._t("favorites.add_named", { name: candidate.name }),
                    onclick: () => add(candidate),
                  },
                  this._t("favorites.add")
                )
              )
            )
          )
        ),
      this._favoriteFormEnd(form, "", null)
    );
  }

  _favoriteStream(form) {
    const save = () => {
      const data = { name: form.name.trim(), url: form.url.trim(), kind: "stream" };
      this._changeFavorite(form.list, "add", { data }, form, "favorites.added");
    };
    return h(
      "div",
      { class: "fav-form" },
      h("h3", {}, this._t("favorites.stream")),
      h(
        "div",
        { class: "field" },
        h("label", { for: "fav-name" }, this._t("favorites.name")),
        this._favoriteInput(form, "name", { maxLength: MAX_FAVORITE_NAME, placeholder: this._t("favorites.name_placeholder") })
      ),
      h(
        "div",
        { class: "field" },
        h("label", { for: "fav-url" }, this._t("favorites.url")),
        this._favoriteInput(form, "url", { maxLength: MAX_FAVORITE_URL, placeholder: "https://", inputMode: "url" }),
        h("div", { class: "help" }, this._t("favorites.url_help"))
      ),
      this._favoriteFormEnd(form, this._t("favorites.add"), save)
    );
  }

  // The chat show: two of this person's assistants talk to each other.

  get _chatShowing() {
    return !!this._router && this._section === "chat" && this.isConnected;
  }

  _stopChatPoll() {
    clearTimeout(this._chat.timer);
    this._chat.timer = null;
  }

  /** Fetch the chat, and again every 2 s while one runs and its section is open. */
  async _loadChat() {
    const chat = this._chat;
    if (chat.loading) return;
    this._stopChatPoll();
    chat.loading = true;
    let changed;
    try {
      const state = await this._call("chat/state");
      if (state.ok === false) throw new Error(refusal(state));
      changed = JSON.stringify(state) !== JSON.stringify(chat.state) || !!chat.loadError;
      chat.state = state;
      chat.loadError = "";
      if (!chat.form) chat.form = this._chatForm(state);
    } catch (err) {
      const text = reason(err) || "?";
      changed = chat.loadError !== text;
      chat.loadError = text;
    }
    chat.loading = false;
    if (!this._chatShowing) return;
    // Drawn again only for news, so a list someone is choosing from stays open.
    if (changed) this._render();
    if (chat.state && chat.state.chat.running) chat.timer = setTimeout(() => this._loadChat(), CHAT_POLL_MS);
  }

  /** The first choices: those of the last chat where still offered, else the first two assistants. */
  _chatForm(state) {
    const last = state.chat;
    const ids = state.routers.map((router) => router.entry_id);
    const id = (index) => (ids.includes(last.assistant_ids[index]) ? last.assistant_ids[index] : "");
    const offered = state.speakers.some((item) => item.entity_id === last.speaker);
    return {
      a: id(0) || ids[0] || "",
      b: id(1) || ids[1] || "",
      // "This device" unless the last chat was on a speaker that is still offered.
      speaker: offered ? last.speaker : this._here,
      topic: "",
      // The saved limits, which the person may change for their chat.
      limits: { ...state.defaults },
    };
  }

  _chatShow() {
    const chat = this._chat;
    const head = this._head("chat", this._t("chat.lede"));
    const failed = chat.loadError && h("div", { class: "notice error", role: "alert" }, this._t("failed", { reason: chat.loadError }));
    if (!chat.state) return [head, failed || h("div", { class: "empty" }, this._t("chat.loading"))];
    return [head, failed, this._chatSetup(), this._chatTranscript()];
  }

  _chatSetup() {
    const chat = this._chat;
    const { state, form } = chat;
    const running = state.chat.running;
    const locked = running || !!chat.busy;
    const router = (id) => state.routers.find((item) => item.entry_id === id);
    const a = router(form.a);
    const b = router(form.b);
    const here = form.speaker === this._here;
    const speakerKnown = here || state.speakers.some((item) => item.entity_id === form.speaker);
    const ready = !!a && !!b && a !== b && a.has_writer && b.has_writer && speakerKnown;
    const changed = (key) => (ev) => {
      form[key] = ev.target.value;
      chat.error = "";
      this._render();
    };
    const assistant = (key) =>
      h(
        "div",
        { class: "field" },
        h("label", { for: `chat-${key}` }, this._t(`chat.assistant_${key}`)),
        h(
          "select",
          { id: `chat-${key}`, class: "input", disabled: locked, onchange: changed(key) },
          h("option", { value: "", selected: !form[key] }, this._t("chat.choose_assistant")),
          state.routers.map((item) => h("option", { value: item.entry_id, selected: item.entry_id === form[key] }, item.title))
        )
      );
    const group = (domain, label) => {
      const list = state.speakers.filter((item) => item.domain === domain);
      return (
        list.length > 0 &&
        h("optgroup", { label }, list.map((item) => h("option", { value: item.entity_id, selected: item.entity_id === form.speaker }, item.name)))
      );
    };
    const speaker = h(
      "select",
      { id: "chat-speaker", class: "input", disabled: locked, onchange: changed("speaker") },
      h("option", { value: this._here, selected: here }, this._t("chat.this_device")),
      group("assist_satellite", this._t("chat.satellites")),
      group("media_player", this._t("chat.media_players"))
    );
    const topic = h("input", {
      id: "chat-topic",
      type: "text",
      maxLength: 200,
      value: form.topic,
      disabled: locked,
      oninput: (ev) => {
        form.topic = ev.target.value;
      },
    });
    topic.dataset.key = "chat-topic";
    const limit = ({ key, min, max, step }) => {
      const control = h("input", {
        id: `chat-${key}`,
        type: "number",
        min,
        max,
        step,
        value: form.limits[key] === undefined ? "" : form.limits[key],
        disabled: locked,
        // Left empty, the saved limit is used.
        oninput: (ev) => {
          if (ev.target.value === "") delete form.limits[key];
          else form.limits[key] = Number(ev.target.value);
        },
      });
      control.dataset.key = `chat-${key}`;
      return h("div", { class: "field" }, h("label", { for: `chat-${key}` }, this._t(`chat.${key}`)), control);
    };
    const chosen = state.speakers.find((item) => item.entity_id === form.speaker);
    const listen =
      chosen &&
      chosen.listen &&
      h(
        "div",
        { class: "listen" },
        h("input", {
          id: "chat-listen",
          type: "checkbox",
          checked: !!form.limits.listen,
          disabled: locked,
          onchange: (ev) => {
            form.limits.listen = ev.target.checked;
          },
        }),
        h("div", {}, h("label", { for: "chat-listen" }, this._t("chat.listen")), h("div", { class: "help" }, this._t("chat.listen_help")))
      );
    const noWriter = [a, b].filter((item) => item && !item.has_writer);
    const label = (key) => (chat.busy === key ? this._t("chat.working") : this._t(`chat.${key}`));
    return h(
      "div",
      { class: "card" },
      h("h3", {}, this._t("chat.setup")),
      state.routers.length < 2 && h("div", { class: "notice" }, this._t("chat.one_router")),
      h("div", { class: "pair" }, assistant("a"), assistant("b")),
      form.a && form.a === form.b && h("div", { class: "notice" }, this._text("error.chat_same_assistant")),
      noWriter.map((item) => h("div", { class: "notice" }, this._t("chat.no_writer", { title: item.title }))),
      h(
        "div",
        { class: "field" },
        h("label", { for: "chat-speaker" }, this._t("chat.speaker")),
        speaker,
        h("div", { class: "help" }, this._t(here ? "chat.here_help" : chosen && chosen.cast ? "chat.cast_help" : "chat.speaker_help"))
      ),
      h(
        "div",
        { class: "field" },
        h("label", { for: "chat-topic" }, this._t("chat.topic")),
        topic,
        h("div", { class: "help" }, this._t("chat.topic_help"))
      ),
      h("div", { class: "limits-head" }, h("h3", {}, this._t("chat.limits")), h("div", { class: "help" }, this._t("chat.limits_help"))),
      h("div", { class: "limits" }, CHAT_LIMITS.map(limit)),
      listen,
      chat.error && h("div", { class: "notice error", role: "alert" }, chat.error),
      h(
        "div",
        { class: "row-actions" },
        h("button", { class: "btn primary", disabled: locked || !ready, onclick: () => this._startChat() }, label("start")),
        // Stopped already: the last line is finishing, so there is nothing left to stop.
        h("button", { class: "btn danger", disabled: !running || !!state.chat.ended || !!chat.busy, onclick: () => this._stopChat() }, label("stop"))
      )
    );
  }

  _chatTranscript() {
    const { state } = this._chat;
    const chat = state.chat;
    if (!chat.assistants.length) return null;
    const [a, b] = chat.assistants;
    const found = state.speakers.find((item) => item.entity_id === chat.speaker);
    const speaker = found ? found.name : chat.speaker.startsWith("page:") ? this._t("chat.this_device") : chat.speaker;
    const tone = (who) => ["one", "two"][chat.assistants.indexOf(who)] || "person";
    // Ended but still running: a stopped chat lets the line being spoken finish.
    const finishing = chat.running && !!chat.ended;
    return h(
      "div",
      { class: "card" },
      h(
        "div",
        { class: "chat-head" },
        h("h3", {}, this._t("chat.transcript")),
        chat.running && !finishing && h("span", { class: "live" }, this._t("chat.live"))
      ),
      h("p", { class: "intro" }, this._t(chat.running ? "chat.running" : "chat.ended", { a, b, speaker })),
      chat.lines.length > 0 &&
        h(
          "ol",
          { class: "transcript" },
          chat.lines.map((line) => h("li", { class: tone(line.who) }, h("span", { class: "by" }, `${line.who}:`), " ", line.text))
        ),
      chat.running && !finishing && !chat.lines.length && h("div", { class: "empty" }, this._t("chat.waiting")),
      finishing && h("div", { class: "notice info" }, this._t("chat.finishing")),
      chat.ended &&
        !finishing &&
        h("div", { class: `notice${CHAT_ENDED_FINE.includes(chat.ended) ? " info" : " error"}` }, this._text(`error.${chat.ended}`, chat.ended))
    );
  }

  /** One chat command at a time; a refusal or failure is shown on the card. */
  async _chatAct(busy, work) {
    const chat = this._chat;
    if (chat.busy) return;
    chat.busy = busy;
    chat.error = "";
    this._render();
    try {
      const result = await work();
      if (result && result.ok === false) chat.error = refusal(result);
    } catch (err) {
      chat.error = this._t("failed", { reason: reason(err) });
    }
    chat.busy = "";
    this._render();
    // Ask for the chat again now rather than at the next poll.
    this._loadChat();
  }

  _startChat() {
    const form = this._chat.form;
    return this._chatAct("start", async () => {
      // The page must be watching the stage before the chat is let start on it.
      if (form.speaker === this._here) await this._openStage();
      return this._call("chat/start", {
        a: form.a,
        b: form.b,
        speaker: form.speaker,
        topic: form.topic.trim(),
        limits: { ...form.limits },
      });
    });
  }

  _stopChat() {
    return this._chatAct("stop", () => this._call("chat/stop"));
  }

  /** Put the stage card over the page, hidden until a chat is on it, and wait until it watches. */
  async _openStage() {
    if (!this._stage) {
      if (!customElements.get(STAGE_CARD)) await import(this._chat.state.stage_script);
      const card = document.createElement(STAGE_CARD);
      card.setConfig({ viewer: this._viewer, sound_text: this._t("chat.tap_for_sound") });
      card.hass = this._hass;
      card.addEventListener("vam-stage", (ev) => this._stageActive(ev.detail.active));
      const stop = h("button", { type: "button", onclick: () => this._stopChat() }, this._t("chat.stop"));
      stop.style.cssText = STAGE_STOP_STYLE;
      const box = h("div", {}, card, stop);
      box.style.cssText = STAGE_BOX_STYLE;
      box.hidden = true;
      // On the document, not in this page: drawing the page again would take the card off
      // it for a moment, which closes its streams.
      document.body.append(box);
      this._stage = { box, card, lock: null };
    }
    if (!(await this._stage.card.watching())) throw new Error(this._t("chat.here_failed"));
  }

  /** Show the stage while a chat is on it, and keep the screen on meanwhile where that can be asked. */
  _stageActive(active) {
    const stage = this._stage;
    if (!stage) return;
    stage.box.hidden = !active;
    if (active && !stage.lock && navigator.wakeLock) {
      navigator.wakeLock
        .request("screen")
        .then((lock) => {
          if (this._stage === stage && !stage.box.hidden) stage.lock = lock;
          else lock.release();
        })
        .catch(() => {});
    } else if (!active && stage.lock) {
      stage.lock.release();
      stage.lock = null;
    }
    // The chat ended, or started: show its lines now rather than at the next poll.
    if (this._chatShowing) this._loadChat();
  }

  /** Leaving the page ends its stage: the chat on it ends a moment later (stage.py). */
  _closeStage() {
    const stage = this._stage;
    if (!stage) return;
    this._stage = null;
    if (stage.lock) stage.lock.release();
    stage.box.remove();
  }

  // Shared settings, read-only.

  _shared() {
    const router = this._router;
    return [
      this._head("shared", this._t("shared.lede")),
      h(
        "div",
        { class: "card" },
        h(
          "dl",
          { class: "facts-of" },
          h("dt", {}, this._t("shared.assistant")),
          h("dd", {}, router.assistant, h("div", { class: "help" }, this._t("shared.assistant_help"))),
          h("dt", {}, this._t("shared.strangers")),
          h("dd", {}, this._t(router.strangers_mode === "fixed" ? "shared.strangers_fixed" : "shared.strangers_ai"))
        )
      ),
      h(
        "div",
        { class: "card" },
        h("h3", {}, this._t("shared.calendars")),
        router.calendars.length
          ? h("ul", { class: "items" }, router.calendars.map((row) => this._calendarRow(row)))
          : h("div", { class: "empty" }, this._t("shared.calendars_none"))
      ),
    ];
  }

  _calendarRow(row) {
    const when = row.when || {};
    const before = when.all_day_day === "before";
    const time = when.all_day_time ? String(when.all_day_time).slice(0, 5) : "";
    const allDay = time
      ? this._t(before ? "shared.all_day_before" : "shared.all_day_at", { time })
      : this._t(before ? "shared.all_day_usual_before" : "shared.all_day_usual");
    const timed = when.lead_minutes
      ? this._t("shared.lead", { minutes: when.lead_minutes })
      : this._t("shared.at_start");
    return h(
      "li",
      { class: "item" },
      h(
        "div",
        { class: "what" },
        h("div", { class: "title" }, row.name),
        h(
          "div",
          { class: "sub" },
          h("span", { class: `tag${row.for === "household" ? "" : " mine"}` }, this._t(row.for === "household" ? "shared.for_everyone" : "shared.for_you")),
          h("span", {}, `${allDay}, ${timed}`)
        )
      )
    );
  }
}

if (!customElements.get(ELEMENT)) customElements.define(ELEMENT, VoicePersonaRouterMine);
