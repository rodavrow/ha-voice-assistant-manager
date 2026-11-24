// SPDX-FileCopyrightText: 2026 rodavrow
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The settings page of Voice Assistant Manager. One custom element, no build step and no
// libraries. Everything it shows comes from the voice_assistant_manager/panel/* websocket
// commands, and everything is put on the page as text, never as HTML: remembered facts
// and calendar titles come from people and services outside this page.

const WS = "voice_assistant_manager/panel";
const ELEMENT = "voice-assistant-manager-panel";
const PRIVATE_PREFIX = "(private)";

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

const clone = (value) => JSON.parse(JSON.stringify(value));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** A readable reason from a websocket error, a REST error of hass.callApi or an Error. */
function reason(err) {
  if (!err) return "";
  if (typeof err === "string") return err;
  return err.message || (err.body && err.body.message) || err.error || String(err);
}

const SECTIONS = [
  { id: "overview", label: "Overview", icon: "mdi:home-account" },
  { id: "assistant", label: "Assistant", icon: "mdi:robot-happy-outline" },
  { id: "people", label: "People", icon: "mdi:account-multiple-outline" },
  { id: "strangers", label: "Unknown voices", icon: "mdi:account-question-outline" },
  { id: "memory", label: "Memory", icon: "mdi:head-lightbulb-outline" },
  { id: "reminders", label: "Reminders", icon: "mdi:bell-outline" },
  { id: "calendars", label: "Calendars", icon: "mdi:calendar-clock-outline" },
  // The lists and speakers are those of every assistant; the switch is the selected one's.
  { id: "favorites", label: "Favorites", icon: "mdi:star-outline" },
  // About two assistants at once, so it ignores which assistant is selected.
  { id: "chat", label: "Chat show", icon: "mdi:account-voice", text: "chat.title" },
];
// The chat limits the card edits (chat_show.ChatLimits); "listen" is a checkbox, only for a speaker that can listen.
const CHAT_LIMITS = [
  { key: "max_lines", min: 2, max: 50, step: 1 },
  { key: "words", min: 5, max: 80, step: 1 },
  { key: "pause", min: 0, max: 10, step: 0.5 },
  { key: "minutes", min: 1, max: 30, step: 1 },
];
const CHAT_POLL_MS = 2000;
// Reasons a chat ends that are no failure (chat_show.py).
const CHAT_ENDED_FINE = ["chat_done", "chat_time_up", "chat_stopped"];
// Favorites (favorites.py): what each kind is called, and the longest name and address.
const FAVORITE_KINDS = { radio: "Radio", podcast: "Podcast", playlist: "Playlist", album: "Album", track: "Track", stream: "Stream" };
const MAX_FAVORITE_NAME = 80;
const MAX_FAVORITE_URL = 2000;
// The lists of favorites/get, in the order they are shown; the household's last.
const FAVORITE_LISTS = ["person:owner", "person:wife", "household"];

// The setup guide: its steps in order, the ready-made styles (setup_guide.STYLES), and the
// step that holds each section, for the "Go to step" buttons of the Check step.
const GUIDE_STEPS = ["welcome", "people", "satellites", "pipelines", "strangers", "extras", "check"];
const GUIDE_STYLES = ["butler", "friendly", "plain"];
const GUIDE_STEP_OF = { people: 2, satellites: 3, assistant: 4, strangers: 5, memory: 6, reminders: 6, calendars: 6 };
// The step that shows the fields a refused save names (save_settings answers their section).
// The reminders' agent is not in the guide: its own section opens instead.
const GUIDE_FIELD_STEP = { people: 2, strangers: 5, memory: 6 };
const DOCS = "https://github.com/rodavrow/ha-voice-assistant-manager";
const DOCS_TRACKING = `${DOCS}#2-keep-the-helper-up-to-date`;
const DOCS_REMINDERS = `${DOCS}/blob/main/docs/reminders.md`;
const DOCS_CALENDARS = `${DOCS}/blob/main/docs/calendars.md`;
const VOICE_ASSISTANTS = "/config/voice-assistants/assistants";

const STYLE = `
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
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.dot.warn { background: var(--vpr-warn); }
.dot.error { background: var(--vpr-error); }

main { min-width: 0; overflow-wrap: anywhere; }
main > header { margin-bottom: 20px; }
h2 { margin: 0; font-size: 26px; font-weight: 500; letter-spacing: -0.01em; }
h3 { margin: 0 0 4px; font-size: 16px; font-weight: 500; }
.lede { margin: 6px 0 0; max-width: 62ch; color: var(--vpr-muted); }

.card { background: var(--vpr-card); border-radius: var(--vpr-radius); border: 1px solid var(--vpr-line); padding: 20px; margin-bottom: 16px; }
.card > .intro { margin: 0 0 16px; color: var(--vpr-muted); max-width: 62ch; }

/* The cast: who can speak, and how each is answered. */
.cast { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0; padding: 0; overflow: hidden; }
.voice { padding: 22px 20px 20px; border-right: 1px solid var(--vpr-line); cursor: pointer; background: none; border-top: 0; border-bottom: 0; border-left: 0; text-align: left; display: block; width: 100%; }
.voice:last-child { border-right: 0; }
.voice:hover { background: color-mix(in srgb, var(--vpr-text) 4%, transparent); }
.avatar {
  width: 52px; height: 52px; border-radius: 50%; display: grid; place-items: center;
  font-size: 22px; font-weight: 500; margin-bottom: 14px;
  background: color-mix(in srgb, var(--tone) 18%, transparent); color: var(--tone);
}
.voice.one { --tone: var(--vpr-primary); }
.voice.two { --tone: var(--vpr-accent); }
.voice.unknown { --tone: var(--vpr-muted); }
.voice.unknown .avatar { background: none; border: 2px dashed color-mix(in srgb, var(--vpr-muted) 60%, transparent); }
.voice .name { font-size: 17px; font-weight: 500; }
.voice .how { color: var(--vpr-muted); font-size: 14px; margin-top: 2px; }

.status { list-style: none; margin: 0; padding: 0; }
.status li { display: flex; gap: 12px; align-items: baseline; padding: 9px 0; border-top: 1px solid var(--vpr-line); }
.status li:first-child { border-top: 0; padding-top: 0; }
.status li:last-child { padding-bottom: 0; }
.status .mark { width: 9px; height: 9px; border-radius: 50%; flex: none; transform: translateY(-1px); background: var(--vpr-line); }
.status .ok .mark { background: var(--vpr-ok); }
.status .warn .mark { background: var(--vpr-warn); }
.status .error .mark { background: var(--vpr-error); }
.status .info { color: var(--vpr-muted); }
.status .text { flex: 1; min-width: 0; }
.link { border: 0; background: none; padding: 0; color: var(--vpr-primary); cursor: pointer; white-space: nowrap; }
.link:hover { text-decoration: underline; }

.field { display: grid; gap: 6px; padding: 14px 0; border-top: 1px solid var(--vpr-line); }
.field:first-of-type { border-top: 0; padding-top: 0; }
.field:last-child { padding-bottom: 0; }
.field > label, .field > .label { font-weight: 500; }
.help { color: var(--vpr-muted); font-size: 13.5px; max-width: 66ch; }
.field.switch { grid-template-columns: minmax(0, 1fr) auto; align-items: center; column-gap: 16px; }
.field.switch .help { grid-column: 1; }
.field.switch .toggle { grid-column: 2; grid-row: 1 / span 2; }
.field.bad input, .field.bad select { border-color: var(--vpr-error); }
.field .problem { color: var(--vpr-error); font-size: 13.5px; }

input[type="text"], input[type="number"], input[type="time"], select.input, textarea {
  width: 100%; padding: 10px 12px; border-radius: 10px;
  border: 1px solid var(--vpr-line); background: var(--vpr-bg); color: var(--vpr-text);
}
input[type="number"], input[type="time"] { max-width: 160px; }
textarea { min-height: 96px; resize: vertical; line-height: 1.45; }
input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible {
  outline: 2px solid var(--vpr-primary); outline-offset: 2px;
}
.unit { display: flex; align-items: center; gap: 8px; color: var(--vpr-muted); }

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
.btn:disabled { opacity: 0.55; cursor: default; }
.row-actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }
.row-actions .spacer { flex: 1; }

.people { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.people .card { margin: 0; }
.person-head { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.person-head .avatar { width: 40px; height: 40px; font-size: 17px; margin: 0; }
.person-head.one { --tone: var(--vpr-primary); }
.person-head.two { --tone: var(--vpr-accent); }

.facts { list-style: none; margin: 0; padding: 0; }
.facts li { display: flex; align-items: center; gap: 10px; padding: 9px 0; border-top: 1px solid var(--vpr-line); }
.facts li:first-child { border-top: 0; }
.facts .fact { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.chip { border: 1px solid var(--vpr-line); background: none; border-radius: 999px; padding: 3px 11px; font-size: 13px; cursor: pointer; color: var(--vpr-muted); white-space: nowrap; }
.chip[aria-pressed="true"] { background: color-mix(in srgb, var(--vpr-accent) 18%, transparent); border-color: transparent; color: var(--vpr-text); }
.icon-btn { border: 0; background: none; cursor: pointer; color: var(--vpr-muted); width: 32px; height: 32px; border-radius: 50%; font-size: 18px; line-height: 1; flex: none; }
.icon-btn:hover { background: color-mix(in srgb, var(--vpr-error) 12%, transparent); color: var(--vpr-error); }
.chip:disabled, .icon-btn:disabled { opacity: 0.55; cursor: default; }
.icon-btn:disabled:hover { background: none; color: var(--vpr-muted); }
.add-fact { display: flex; gap: 10px; margin-top: 12px; }
.add-fact input { flex: 1; }
.empty { color: var(--vpr-muted); padding: 4px 0; }
details.list { border-top: 1px solid var(--vpr-line); padding: 12px 0; }
details.list:first-of-type { border-top: 0; padding-top: 0; }
details.list summary { cursor: pointer; font-weight: 500; list-style: none; display: flex; align-items: center; gap: 8px; }
details.list summary::-webkit-details-marker { display: none; }
details.list summary::before { content: "›"; display: inline-block; width: 12px; color: var(--vpr-muted); transition: transform 0.15s; }
details.list[open] summary::before { transform: rotate(90deg); }
details.list .count { color: var(--vpr-muted); font-weight: 400; }
details.list > div { padding: 10px 0 0 20px; }

.cal { display: flex; align-items: center; gap: 14px; padding: 12px 0; border-top: 1px solid var(--vpr-line); }
.cal:first-child { border-top: 0; padding-top: 0; }
.cal .what { flex: 1; min-width: 0; }
.cal .title { font-weight: 500; overflow-wrap: anywhere; }
.cal .sub { color: var(--vpr-muted); font-size: 13.5px; }
.editor h3 { margin-bottom: 16px; }
.editor { border: 1px solid var(--vpr-primary); border-radius: var(--vpr-radius); padding: 20px; margin: 4px 0 16px; background: color-mix(in srgb, var(--vpr-primary) 4%, var(--vpr-card)); }
.spoken { margin: 8px 0 0; padding-left: 14px; border-left: 3px solid var(--vpr-accent); max-width: 62ch; }
.spoken .when { color: var(--vpr-muted); font-size: 13.5px; }

.notice { border-radius: 10px; padding: 12px 14px; margin-bottom: 16px; background: color-mix(in srgb, var(--vpr-warn) 14%, transparent); }
.notice.error { background: color-mix(in srgb, var(--vpr-error) 12%, transparent); }

.savebar {
  position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); z-index: 6;
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  width: min(680px, calc(100vw - 32px)); padding: 12px 12px 12px 20px;
  background: var(--vpr-card); border: 1px solid var(--vpr-line); border-radius: 18px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.22);
}
.savebar .msg { flex: 1; min-width: 180px; }
.savebar .msg.error { color: var(--vpr-error); }
.toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 7; background: var(--vpr-text); color: var(--vpr-card); padding: 10px 18px; border-radius: 999px; }
.loading { padding: 80px 24px; text-align: center; color: var(--vpr-muted); }

/* The setup guide. */
.shell.guide { grid-template-columns: minmax(0, 1fr); max-width: 860px; }
.guide-top { margin-bottom: 26px; }
.step-of { color: var(--vpr-muted); font-size: 13.5px; margin-bottom: 12px; }
.steps { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); }
.steps li { position: relative; }
.steps li + li::before { content: ""; position: absolute; top: 13px; left: calc(-50% + 18px); right: calc(50% + 18px); height: 2px; background: var(--vpr-line); }
.steps li.past::before, .steps li.now::before { background: var(--vpr-primary); }
.steps .step { display: flex; flex-direction: column; align-items: center; gap: 6px; width: 100%; padding: 0; border: 0; background: none; color: var(--vpr-muted); text-align: center; cursor: pointer; }
.steps .step:disabled { cursor: default; opacity: 1; }
.steps .num { width: 28px; height: 28px; border-radius: 50%; display: grid; place-items: center; font-size: 13px; font-weight: 500; border: 2px solid var(--vpr-line); background: var(--vpr-card); }
.steps .past .num { background: var(--vpr-primary); border-color: var(--vpr-primary); color: var(--vpr-on-primary); }
.steps .now .num { border-color: var(--vpr-primary); color: var(--vpr-primary); box-shadow: 0 0 0 3px color-mix(in srgb, var(--vpr-primary) 22%, transparent); }
.steps .now .step { color: var(--vpr-text); font-weight: 500; }
.steps .name { font-size: 12.5px; line-height: 1.3; }
.steps .past .step:hover .name { text-decoration: underline; }
.guide-foot { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 8px; padding-top: 16px; border-top: 1px solid var(--vpr-line); }
.guide-foot .spacer { flex: 1; }
.plan { margin: 8px 0 0; padding-left: 22px; }
.plan li { padding: 3px 0; }
.intro.after, .help.after { margin: 14px 0 0; }
.styles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.style { display: grid; gap: 2px; align-content: start; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--vpr-line); background: var(--vpr-bg); text-align: left; cursor: pointer; }
.style:hover { border-color: color-mix(in srgb, var(--vpr-primary) 50%, var(--vpr-line)); }
.style[aria-pressed="true"] { border-color: var(--vpr-primary); box-shadow: 0 0 0 1px var(--vpr-primary); background: color-mix(in srgb, var(--vpr-primary) 8%, var(--vpr-card)); }
.style-name { font-weight: 500; }
.style-about { color: var(--vpr-muted); font-size: 13.5px; }
.sat-head { display: flex; align-items: center; gap: 12px; }
.sat-head input[type="checkbox"] { width: 18px; height: 18px; margin: 0; flex: none; accent-color: var(--vpr-primary); cursor: pointer; }
.sat-head label, .sat-head .what { display: grid; min-width: 0; }
.sat-head label { cursor: pointer; }
.sat-name { font-weight: 500; font-size: 16px; }
.sat-id { color: var(--vpr-muted); font-size: 13px; overflow-wrap: anywhere; }
.card.sat.off { padding-top: 14px; padding-bottom: 14px; }
.tasks { margin-top: 14px; }
.task { display: grid; grid-template-columns: 9px minmax(0, 1fr); column-gap: 12px; padding: 12px 0; border-top: 1px solid var(--vpr-line); }
.task:last-child { padding-bottom: 0; }
.task .mark { width: 9px; height: 9px; border-radius: 50%; margin-top: 7px; background: var(--vpr-line); }
.task.ok .mark { background: var(--vpr-ok); }
.task.warn .mark { background: var(--vpr-warn); }
.task.error .mark { background: var(--vpr-error); }
.task .body { display: grid; gap: 10px; min-width: 0; justify-items: start; }
.will { color: var(--vpr-muted); font-size: 13.5px; max-width: 66ch; }
.action { display: grid; gap: 8px; justify-items: start; }
.action-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.result { font-size: 13.5px; }
.result.ok { color: var(--vpr-ok); }
.result.error { color: var(--vpr-error); }
.tasks > .result { padding: 4px 0 0 21px; }
.names { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; width: 100%; }
.names .field, .field.pick { border-top: 0; padding: 0; width: 100%; }
.btn.small { padding: 6px 14px; font-size: 14px; white-space: nowrap; }
h3.group { margin: 24px 0 12px; }
.banner { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.banner > span { flex: 1; min-width: 200px; }
a.link { text-decoration: none; }
a.link:hover { text-decoration: underline; }
.status .text .will, .status .text .result { margin-top: 2px; }
.extra { margin: 0; padding: 12px 0; border-top: 1px solid var(--vpr-line); }
.extra:first-of-type { border-top: 0; padding-top: 0; }
.extra:last-child { padding-bottom: 0; }

/* The chat show. */
.pair, .limits { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; padding-bottom: 14px; }
.limits { grid-template-columns: repeat(4, minmax(0, 1fr)); padding-bottom: 0; }
.pair .field, .limits .field { border-top: 0; padding: 0; align-content: start; }
.limits input[type="number"] { max-width: none; }
.help.below, .notice.below { margin: -4px 0 14px; }
.limits-head { display: grid; gap: 2px; padding: 14px 0; border-top: 1px solid var(--vpr-line); }
.limits-head h3 { margin: 0; }
.notice.after { margin: 14px 0 0; }
.notice.plain { background: color-mix(in srgb, var(--vpr-text) 6%, transparent); }
.chat-head { display: flex; align-items: center; gap: 12px; }
.chat-head h3 { flex: 1; margin: 0; }
.live { display: inline-flex; align-items: center; gap: 6px; color: var(--vpr-ok); font-size: 13.5px; font-weight: 500; }
.dot.ok { background: var(--vpr-ok); }
.chat-head + .intro { margin-top: 4px; margin-bottom: 4px; }
.intro + .intro { margin-top: 0; }
.transcript { list-style: none; margin: 12px 0 0; padding: 0; }
.transcript li { padding: 9px 0 9px 14px; border-left: 3px solid var(--tone); margin-top: 6px; }
.transcript li.one { --tone: var(--vpr-primary); }
.transcript li.two { --tone: var(--vpr-accent); }
.transcript li.person { --tone: var(--vpr-muted); }
.transcript .by { font-weight: 500; color: var(--tone); }

/* Favorites. */
.fav-list + .fav-list { border-top: 1px solid var(--vpr-line); margin-top: 18px; padding-top: 18px; }
.fav-list > h3 { display: flex; gap: 8px; align-items: baseline; margin-bottom: 8px; }
.fav-list .count { color: var(--vpr-muted); font-weight: 400; }
.favs { list-style: none; margin: 0; padding: 0; }
.fav { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-top: 1px solid var(--vpr-line); }
.fav:first-child { border-top: 0; }
.fav .what { flex: 1; min-width: 0; }
.fav .title { font-weight: 500; overflow-wrap: anywhere; }
.fav .sub { color: var(--vpr-muted); font-size: 13.5px; display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px; }
.fav .side { flex: none; display: flex; gap: 8px; }
.fav.editing { display: block; }
.tag { display: inline-block; border-radius: 999px; padding: 0 9px; font-size: 12.5px; line-height: 20px; background: color-mix(in srgb, var(--vpr-muted) 16%, transparent); color: var(--vpr-text); white-space: nowrap; }
.fav-form { border: 1px solid var(--vpr-primary); border-radius: 12px; padding: 14px 16px; margin-top: 12px; background: color-mix(in srgb, var(--vpr-primary) 4%, var(--vpr-card)); }
.fav.editing .fav-form { margin-top: 0; }
.fav-form > h3 { margin-bottom: 10px; }
.fav-form .row-actions { margin-top: 14px; }
.fav-form .favs { margin-top: 10px; }
.fav-form .problem { margin-top: 10px; color: var(--vpr-error); font-size: 13.5px; }
.fav-form .empty { margin-top: 10px; }
.find { display: flex; gap: 10px; }
.find input { flex: 1; min-width: 0; }
.fav-list > .row-actions { margin-top: 12px; }
.speaker { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 300px); gap: 6px 16px; align-items: center; padding: 12px 0; border-top: 1px solid var(--vpr-line); }
.speaker:first-of-type { border-top: 0; padding-top: 0; }
.speaker .title { font-weight: 500; overflow-wrap: anywhere; }
.speaker .sub { color: var(--vpr-muted); font-size: 13.5px; }

@media (max-width: 860px) {
  .shell { grid-template-columns: minmax(0, 1fr); gap: 12px; padding: 12px 12px 120px; }
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
  .cast { grid-template-columns: minmax(0, 1fr); padding: 0; }
  .voice { border-right: 0; border-bottom: 1px solid var(--vpr-line); display: grid; grid-template-columns: auto 1fr; column-gap: 14px; align-items: center; padding: 12px 16px; }
  .voice:last-child { border-bottom: 0; }
  .voice .avatar { margin: 0; grid-row: span 2; width: 40px; height: 40px; font-size: 17px; }
  .voice .name { font-size: 16px; }
  .status li { flex-wrap: wrap; }
  .status .text { flex-basis: calc(100% - 21px); }
  .status .link { margin-left: 21px; }
  .people { grid-template-columns: minmax(0, 1fr); }
  .cal { flex-wrap: wrap; }
  .field.switch { column-gap: 12px; }
  input[type="number"], input[type="time"] { max-width: 130px; }
  .editor { padding: 14px; }
  .savebar { bottom: 12px; padding: 8px 8px 8px 14px; gap: 8px; flex-wrap: nowrap; }
  .savebar .msg { min-width: 0; font-size: 13.5px; line-height: 1.3; }
  .savebar .btn { padding: 8px 14px; flex: none; }
  h2 { font-size: 22px; }
  .lede { font-size: 14px; }
  .status .btn { margin-left: 21px; }
  .guide-top { margin-bottom: 18px; }
  .steps .name { display: none; }
  .steps .num { width: 24px; height: 24px; font-size: 12px; }
  .steps li + li::before { top: 11px; left: calc(-50% + 15px); right: calc(50% + 15px); }
  .styles, .names { grid-template-columns: minmax(0, 1fr); }
  .guide-foot { gap: 8px; }
  .pair { grid-template-columns: minmax(0, 1fr); }
  .limits { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 12px; }
  .fav { flex-wrap: wrap; gap: 8px; }
  .fav .what { flex-basis: 100%; }
  /* A search result keeps its Add button beside it. */
  .fav-form .fav { flex-wrap: nowrap; }
  .fav-form .fav .what { flex-basis: auto; }
  .fav-form { padding: 12px; }
  .speaker { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
`;

class VoicePersonaRouterPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._data = null;
    this._routerId = null;
    this._section = "overview";
    this._draft = null;
    this._saveError = "";
    this._saveSection = ""; // the section a refused save is about, when it names one
    this._busy = false;
    this._failed = "";
    this._calendar = null; // { original, row, errors } while a calendar is edited
    this._avatar = null; // { original, row, errors, busy } while an avatar is edited
    this._openLists = new Set();
    // One memory save at a time: each edit is built from the facts on screen, which only
    // change when the server answers, so a second edit could undo the first.
    this._memoryBusy = false;
    this._factDrafts = {}; // list key -> text typed in its add box, kept until it is saved
    this._toastTimer = null;
    this._guide = null; // the open setup guide, see _newGuide
    this._guideChecked = false; // whether the first load decided to open the guide
    this._sats = null; // { entryId, list, payloads, error } from guide/satellites
    this._satsGen = 0; // bumped whenever the satellites may have changed
    this._satsLoading = null; // the generation whose satellites are being fetched
    this._actions = {}; // per button that changes Home Assistant: { busy } or { ok, text }
    // The chat show: the last chat/state answer, the choices on the card, and the poll.
    // busy names the command under way ("start", "stop", "defaults"); loadError is chat/state's.
    this._chat = { state: null, form: null, stale: true, loading: false, busy: "", error: "", loadError: "", timer: null };
    // Favorites: favorites/get's answer, fetched whenever the section opens. One change at a
    // time, as for memory: each answer replaces a whole list.
    this._fav = { data: null, stale: true, loading: false, error: "" };
    this._favBusy = false;
    this._favForm = null; // the one open form: { list, mode: "edit" | "search" | "stream", ... }
    this._favError = null; // { list, text } after a refused remove
    this._favSpeakers = null; // the speaker of each satellite as chosen here, until saved
    this._favSpeakersError = "";
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
    if (first) this._load();
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.narrow = narrow;
  }

  connectedCallback() {
    if (!this.shadowRoot.firstChild) this._render();
    else this._wantChat();
  }

  disconnectedCallback() {
    this._stopChatPoll();
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
      this._resetDraft();
      this._failed = "";
      if (!this._guideChecked) {
        this._guideChecked = true;
        const router = this._router;
        if (router && router.guide === "new") {
          this._guide = this._newGuide(1);
          this._rememberStep(1);
        } else if (router && router.guide === "started") {
          this._guide = this._newGuide(this._resumeStep(router));
        }
      }
    } catch (err) {
      this._failed = (err && err.message) || "The settings could not be loaded.";
    }
    this._render();
  }

  get _router() {
    return this._data ? this._data.routers.find((r) => r.entry_id === this._routerId) : null;
  }

  _resetDraft() {
    const router = this._router;
    this._draft = router ? clone(router.settings) : null;
    this._saveError = "";
    this._calendar = null;
    this._avatar = null;
  }

  get _dirty() {
    const router = this._router;
    return !!router && !!this._draft && !same(this._draft, router.settings);
  }

  _replaceRouter(router) {
    const index = this._data.routers.findIndex((r) => r.entry_id === router.entry_id);
    if (index >= 0) this._data.routers[index] = router;
  }

  _text(key, fallback) {
    return (this._data && this._data.strings[key]) || fallback || "";
  }

  _value(section, key) {
    return (this._draft[section] || {})[key];
  }

  _set(section, key, value) {
    if (!this._draft[section]) this._draft[section] = {};
    if (value === "" || value === null || value === undefined) delete this._draft[section][key];
    else this._draft[section][key] = value;
    this._saveError = "";
    this._renderSaveBar();
  }

  async _save() {
    if (this._busy) return;
    this._busy = true;
    this._renderSaveBar();
    if (await this._saveDraft()) {
      this._toast("Saved");
      // The switch and the people decide whose lists are used, and the warnings.
      this._fav.stale = true;
    }
    this._busy = false;
    this._render();
  }

  /** Save the draft through save_settings. False, with _saveError set, when refused. */
  async _saveDraft() {
    this._saveSection = "";
    try {
      const result = await this._call("save_settings", {
        entry_id: this._routerId,
        settings: this._draft,
      });
      if (result.ok) {
        this._replaceRouter(result.router);
        this._resetDraft();
        return true;
      }
      this._saveError = Object.values(result.errors).join(" ");
      this._saveSection = result.section || "";
    } catch (err) {
      this._saveError = (err && err.message) || "Saving failed.";
    }
    return false;
  }

  async _saveCalendar(remove) {
    const editing = this._calendar;
    try {
      const result = await this._call("save_calendar", {
        entry_id: this._routerId,
        original: editing.original,
        row: editing.row,
        remove: !!remove,
      });
      if (result.ok) {
        this._replaceRouter(result.router);
        this._calendar = null;
        this._toast(remove ? "Calendar removed" : "Calendar saved");
      } else {
        editing.errors = result.errors;
      }
    } catch (err) {
      editing.errors = { base: (err && err.message) || "Saving failed." };
    }
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
        this._replaceRouter(result.router);
        if (added) delete this._factDrafts[list.key];
      } else {
        this._toast(Object.values(result.errors).join(" "));
      }
    } catch (err) {
      this._toast((err && err.message) || "Saving failed.");
    }
    this._memoryBusy = false;
    this._render();
  }

  async _setSidebar(show) {
    try {
      this._data.sidebar = (await this._call("set_sidebar", { show })).sidebar;
      this._toast(show ? "Shown in the sidebar" : "Hidden from the sidebar");
    } catch (err) {
      this._toast((err && err.message) || "That did not work.");
    }
    this._render();
  }

  async _setAnnouncementLook(on) {
    try {
      this._data.announcement_look = (
        await this._call("set_announcement_look", { on })
      ).announcement_look;
      this._toast(on ? "Speaking assistant is shown" : "Speaking assistant is not shown");
    } catch (err) {
      this._toast((err && err.message) || "That did not work.");
    }
    this._render();
  }

  _go(section) {
    if (section === "chat" && this._section !== "chat") this._chat.stale = true;
    if (section === "favorites" && this._section !== "favorites") this._fav.stale = true;
    this._section = section;
    this._calendar = null;
    this._avatar = null;
    this._render();
    window.scrollTo({ top: 0 });
  }

  _pickRouter(id) {
    if (this._dirty && !window.confirm("Leave without saving your changes?")) {
      this._render();
      return;
    }
    this._routerId = id;
    this._resetDraft();
    this._actions = {};
    this._dropSatellites();
    this._guide = null;
    if (this._router.guide === "new") this._openGuide(1);
    else this._render();
  }

  _toast(text) {
    this._toastText = text;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toastText = "";
      this._renderSaveBar();
    }, 2600);
  }

  // Entities to pick from.

  _entities(domain) {
    const states = (this._hass && this._hass.states) || {};
    return Object.keys(states)
      .filter((id) => id.startsWith(`${domain}.`))
      .sort()
      .map((id) => {
        const name = (states[id].attributes || {}).friendly_name;
        return [id, name ? `${name} (${id})` : id];
      });
  }

  // Drawing.

  _render() {
    const root = this.shadowRoot;
    const focus = root.activeElement && root.activeElement.dataset.key;
    root.replaceChildren(h("style", {}, STYLE), this._bar());
    this._wantSatellites();
    this._wantChat();
    this._wantFavorites();
    if (this._failed) {
      root.append(h("div", { class: "loading" }, this._failed));
    } else if (!this._data) {
      root.append(h("div", { class: "loading" }, "Loading…"));
    } else if (!this._router) {
      root.append(
        h("div", { class: "loading" }, "No assistant is set up yet. Add one under Settings > Devices & services.")
      );
    } else if (this._guide) {
      root.append(h("div", { class: "shell guide" }, this._switcher(), h("main", {}, this._guideView())));
    } else {
      // The chat show is about two assistants at once: the selected one does not matter there.
      const switcher = this._section === "chat" ? null : this._switcher();
      root.append(h("div", { class: "shell" }, switcher, this._nav(), this._main()));
    }
    root.append(h("div", { id: "floating" }));
    this._renderSaveBar();
    if (focus) {
      const again = root.querySelector(`[data-key="${focus}"]`);
      if (again) again.focus();
    }
    // On a phone the sections are one scrolling row: keep the open one in sight.
    const nav = root.querySelector("nav");
    const current = nav && nav.querySelector('[aria-current="true"]');
    if (current && nav.scrollWidth > nav.clientWidth) {
      nav.scrollLeft = current.offsetLeft - (nav.clientWidth - current.offsetWidth) / 2;
    }
  }

  _bar() {
    const menu = h("ha-menu-button", {});
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    return h("div", { class: "bar" }, menu, h("h1", {}, "Voice Manager"));
  }

  /** One button per assistant. The page shows the settings of one assistant at a time. */
  _switcher() {
    const routers = this._data.routers;
    if (routers.length < 2) return null;
    return h(
      "div",
      { class: "switcher", role: "group", "aria-label": "Assistant" },
      h("span", { class: "hint" }, "Settings of"),
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
    const worst = {};
    for (const line of this._router.status) {
      // Lines of a section without a tab, such as the satellites, mark the Overview.
      const section = SECTIONS.some((s) => s.id === line.section) ? line.section : "overview";
      if (line.level === "error" || (line.level === "warn" && worst[section] !== "error")) {
        worst[section] = line.level;
      }
    }
    return h(
      "nav",
      { "aria-label": "Sections" },
      SECTIONS.map((section) =>
        h(
          "button",
          { "aria-current": String(section.id === this._section), onclick: () => this._go(section.id) },
          h("ha-icon", { icon: section.icon }),
          h("span", { class: "label" }, section.text ? this._text(section.text, section.label) : section.label),
          worst[section.id] && h("span", { class: `dot ${worst[section.id]}`, title: "Needs attention" })
        )
      )
    );
  }

  _main() {
    const draw = {
      overview: () => this._overview(),
      assistant: () => this._assistant(),
      people: () => this._people(),
      strangers: () => this._strangers(),
      memory: () => this._memory(),
      reminders: () => this._reminders(),
      calendars: () => this._calendars(),
      favorites: () => this._favorites(),
      chat: () => this._chatShow(),
    }[this._section];
    return h("main", {}, draw());
  }

  _head(title, lede) {
    return h("header", {}, h("h2", {}, title), lede && h("p", { class: "lede" }, lede));
  }

  _notSaved() {
    return (
      !this._router.saved &&
      h(
        "div",
        { class: "notice" },
        "This assistant has not been set up yet. What you see is a proposal: check it and choose Save changes."
      )
    );
  }

  // Fields. Labels and help texts are those of the Configure dialog.

  _label(section, key) {
    return this._text(`step.settings.sections.${section}.data.${key}`, key);
  }

  _help(section, key) {
    return this._text(`step.settings.sections.${section}.data_description.${key}`);
  }

  _field(section, key, control, extra) {
    const id = `${section}.${key}`;
    control.id = id;
    control.dataset.key = id;
    const help = (extra && extra.help) || this._help(section, key);
    return h(
      "div",
      { class: "field" },
      h("label", { for: id }, (extra && extra.label) || this._label(section, key)),
      control,
      help && h("div", { class: "help" }, help)
    );
  }

  _textField(section, key, extra) {
    const multiline = extra && extra.multiline;
    const control = h(multiline ? "textarea" : "input", {
      type: multiline ? undefined : "text",
      value: this._value(section, key) || "",
      placeholder: (extra && extra.placeholder) || "",
      oninput: (ev) => this._set(section, key, ev.target.value),
    });
    return this._field(section, key, control, extra);
  }

  _numberField(section, key, min, max, step, unit, fallback) {
    const current = this._value(section, key);
    const control = h("input", {
      type: "number",
      min,
      max,
      step,
      value: current === undefined ? fallback : current,
      oninput: (ev) => {
        const value = ev.target.value === "" ? undefined : Number(ev.target.value);
        this._set(section, key, value);
      },
    });
    const field = this._field(section, key, control);
    if (unit) {
      const wrap = h("div", { class: "unit" });
      control.replaceWith(wrap);
      wrap.append(control, unit);
    }
    return field;
  }

  _select(options, current, onchange, emptyLabel) {
    const known = options.some(([value]) => value === current);
    return h(
      "select",
      { class: "input", onchange: (ev) => onchange(ev.target.value) },
      emptyLabel !== undefined && h("option", { value: "", selected: !current }, emptyLabel),
      current && !known && h("option", { value: current, selected: true }, `${current} (not found)`),
      options.map(([value, label]) => h("option", { value, selected: value === current }, label))
    );
  }

  _choiceField(section, key, options, extra) {
    const fallback = extra && extra.fallback;
    const control = this._select(
      options,
      this._value(section, key) || fallback,
      (value) => {
        this._set(section, key, value);
        if (extra && extra.redraw) this._render();
      },
      extra && extra.emptyLabel
    );
    return this._field(section, key, control, extra);
  }

  // The chat voice: a list when the engine names its voices, free text when it does not.
  _voiceField() {
    const engine = this._value("chat", "tts_engine");
    this._voiceLists = this._voiceLists || {};
    const voices = engine ? this._voiceLists[engine] : null;
    if (engine && voices === undefined) this._loadVoices(engine);
    if (!voices || !voices.length) return this._textField("chat", "voice");
    return this._choiceField(
      "chat",
      "voice",
      voices.map((voice) => [voice.id, voice.name]),
      { emptyLabel: "The engine's default voice" }
    );
  }

  async _loadVoices(engine) {
    // null while asked, so it is asked once.
    this._voiceLists[engine] = null;
    try {
      this._voiceLists[engine] = (await this._call("voices", { engine })).voices || [];
    } catch (err) {
      // Only a nicer field: without a list the voice is typed.
      this._voiceLists[engine] = [];
    }
    if (this._voiceLists[engine].length) this._render();
  }

  _entityField(section, key, domain, emptyLabel, extra) {
    return this._choiceField(section, key, this._entities(domain), { ...(extra || {}), emptyLabel });
  }

  // Several entities of one domain: every one that is chosen is highlighted.
  _entitiesField(section, key, domain, extra) {
    const chosen = this._value(section, key) || [];
    const known = this._entities(domain);
    const missing = chosen.filter((id) => !known.some(([value]) => value === id));
    const control = h(
      "select",
      {
        class: "input",
        multiple: true,
        size: Math.min(6, Math.max(3, known.length + missing.length)),
        onchange: (ev) => this._set(section, key, [...ev.target.selectedOptions].map((o) => o.value)),
      },
      missing.map((id) => h("option", { value: id, selected: true }, `${id} (not found)`)),
      known.map(([value, label]) => h("option", { value, selected: chosen.includes(value) }, label))
    );
    return this._field(section, key, control, extra);
  }

  _switchField(section, key, fallback, extra) {
    const id = `${section}.${key}`;
    const current = this._value(section, key);
    const help = (extra && extra.help) || this._help(section, key);
    const input = h("input", {
      type: "checkbox",
      id,
      checked: current === undefined ? fallback : !!current,
      onchange: (ev) => {
        this._set(section, key, ev.target.checked);
        if (extra && extra.redraw) this._render();
      },
    });
    input.dataset.key = id;
    return h(
      "div",
      { class: "field switch" },
      h("label", { for: id }, (extra && extra.label) || this._label(section, key)),
      h("div", { class: "toggle" }, input, h("span", {})),
      help && h("div", { class: "help" }, help)
    );
  }

  _on(section, key, fallback) {
    const value = this._value(section, key);
    return value === undefined ? fallback : !!value;
  }

  // Sections.

  _overview() {
    const router = this._router;
    const people = router.people;
    const strangers = router.settings.strangers || {};
    const refusal =
      strangers.mode === "fixed" || !strangers.reply_agent ? "Refused with a fixed line" : "Refused, in character";
    const voice = (cls, initial, name, how, section) =>
      h(
        "button",
        { class: `voice ${cls}`, onclick: () => this._go(section) },
        h("div", { class: "avatar" }, initial),
        h("div", { class: "name" }, name),
        h("div", { class: "how" }, how)
      );
    const problems = router.status.filter((line) => line.level === "warn" || line.level === "error");
    const last = router.last_announced;
    return [
      this._head(
        router.title,
        `Goes by “${router.assistant}”. It answers the people it recognises in their own persona and turns everyone else away.`
      ),
      (router.guide === "dismissed" || router.guide === "started") &&
        h(
          "div",
          { class: "notice banner" },
          h("span", {}, this._t("overview.banner", { title: router.title })),
          h(
            "button",
            {
              class: "btn primary",
              onclick: () =>
                this._openGuide(router.guide === "started" ? this._resumeStep(router) : this._firstUnfinished()),
            },
            this._t("overview.finish")
          )
        ),
      this._notSaved(),
      h(
        "div",
        { class: "card cast" },
        voice("one", people.owner.slice(0, 1).toUpperCase(), people.owner, "Answered with their own persona", "people"),
        voice("two", people.partner.slice(0, 1).toUpperCase(), people.partner, "Answered with their own persona", "people"),
        voice("unknown", "?", "Anyone else", refusal, "strangers")
      ),
      h(
        "div",
        { class: "card" },
        h("h3", {}, problems.length ? `${problems.length} ${problems.length === 1 ? "thing needs" : "things need"} attention` : "Everything is set up"),
        this._statusList(false),
        h(
          "div",
          { class: "row-actions" },
          h("button", { class: "link", onclick: () => this._openGuide(1) }, this._t("overview.again"))
        )
      ),
      last &&
        h(
          "div",
          { class: "card" },
          h("h3", {}, "Last calendar announcement"),
          h(
            "div",
            { class: "spoken" },
            h("div", {}, last.line || last.title),
            h("div", { class: "when" }, `${this._when(last.at)}, from ${last.calendar}${last.line ? "" : " (no line was written, so the title was used)"}`)
          )
        ),
      h(
        "div",
        { class: "card" },
        this._plainSwitch(
          "Show this page in the sidebar",
          "When off, the page stays reachable at /voice-assistant-manager. This applies to all assistants.",
          this._data.sidebar,
          (show) => this._setSidebar(show)
        ),
        this._plainSwitch(
          "Show which assistant is speaking",
          "On satellites whose firmware can show a character (a select ending in _next_announcement_assistant), chat lines, reminders and prepare_announcement show the assistant that speaks. Speakers without this are never touched. When off, they show their default. This applies to all assistants.",
          this._data.announcement_look !== false,
          (on) => this._setAnnouncementLook(on)
        )
      ),
    ];
  }

  _plainSwitch(label, help, checked, onchange) {
    const id = `switch-${label.replace(/\W+/g, "-")}`;
    return h(
      "div",
      { class: "field switch" },
      h("label", { for: id }, label),
      h("div", { class: "toggle" }, h("input", { type: "checkbox", id, checked, onchange: (ev) => onchange(ev.target.checked) }), h("span", {})),
      help && h("div", { class: "help" }, help)
    );
  }

  _when(iso) {
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return iso;
    return at.toLocaleString(undefined, { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  }

  _assistant() {
    return [
      this._head("Assistant", "The agent that does the real work, and how requests reach it."),
      this._notSaved(),
      h(
        "div",
        { class: "card" },
        this._entityField("agents", "agent", "conversation", "Choose an agent"),
        this._textField("reminders", "assistant", { placeholder: this._router.assistant })
      ),
      h(
        "div",
        { class: "card" },
        h("h3", {}, this._text("step.settings.sections.local.name", "Built-in commands")),
        h("p", { class: "intro" }, this._text("step.settings.sections.local.description")),
        this._switchField("local", "enabled", true),
        this._switchField("local", "leave_questions", true)
      ),
      h(
        "div",
        { class: "card" },
        h("h3", {}, this._text("step.settings.sections.chat.name", "Chat show")),
        h("p", { class: "intro" }, this._text("step.settings.sections.chat.description")),
        this._textField("chat", "character", { multiline: true }),
        this._entityField("chat", "tts_engine", "tts", "The speaker's own voice", { redraw: true }),
        this._voiceField()
      ),
      this._avatarCard(),
      h(
        "div",
        { class: "card" },
        h("h3", {}, this._text("step.settings.sections.advanced.name", "Advanced")),
        this._textField("advanced", "rule", { multiline: true }),
        this._textField("advanced", "agent_down_line", {
          placeholder: "Sorry, I can't think right now. Please try again in a moment.",
        }),
        this._numberField("advanced", "match_within", 0, 15, 0.5, "seconds", 6),
        this._choiceField(
          "advanced",
          "no_helper_role",
          [
            ["owner", this._router.people.owner],
            ["wife", this._router.people.partner],
            ["stranger", "An unknown voice"],
          ],
          { fallback: "owner" }
        ),
        this._choiceField(
          "advanced",
          "avatar_connection",
          [
            ["on_wake", "Connect when woken"],
            ["always", "Always connected"],
          ],
          { fallback: "on_wake" }
        )
      ),
    ];
  }

  _people() {
    const users = this._data.users.map((user) => [user.id, user.name]);
    const person = (section, cls, fallback) => {
      const name = this._value(section, "name") || fallback;
      return h(
        "div",
        { class: "card" },
        h("div", { class: `person-head ${cls}` }, h("div", { class: "avatar" }, name.slice(0, 1).toUpperCase()), h("h3", {}, fallback)),
        this._textField(section, "name"),
        this._choiceField(section, "user", users, { emptyLabel: "No user" }),
        this._textField(section, "persona", { multiline: true })
      );
    };
    return [
      this._head("People", "The two people this assistant knows. Each gets their own persona text, added to the main agent's instructions when they speak."),
      this._notSaved(),
      h("div", { class: "people" }, person("owner", "one", "Person 1"), person("partner", "two", "Person 2")),
    ];
  }

  _strangers() {
    return [
      this._head("Unknown voices", this._text("step.settings.sections.strangers.description")),
      this._notSaved(),
      this._strangerCards(),
    ];
  }

  /** The refusal and near-miss settings, shared by the section and the guide. */
  _strangerCards() {
    const ai = (this._value("strangers", "mode") || "ai") === "ai";
    const near = this._on("near_miss", "enabled", false);
    return [
      h(
        "div",
        { class: "card" },
        this._choiceField(
          "strangers",
          "mode",
          [
            ["ai", "Refusal written by an agent without tools"],
            ["fixed", "Fixed lines only"],
          ],
          { fallback: "ai", redraw: true }
        ),
        ai && this._entityField("strangers", "reply_agent", "conversation", "No agent"),
        ai && this._textField("strangers", "persona", { multiline: true }),
        this._textField("strangers", "fixed_replies", { multiline: true })
      ),
      h(
        "div",
        { class: "card" },
        h("h3", {}, this._text("step.settings.sections.near_miss.name", "Almost recognised")),
        h("p", { class: "intro" }, this._text("step.settings.sections.near_miss.description")),
        this._switchField("near_miss", "enabled", false, { redraw: true }),
        near && this._numberField("near_miss", "min_score", 0, 1, 0.01, "", 0.5),
        near && this._textField("near_miss", "persona", { multiline: true }),
        near && this._textField("near_miss", "fixed_replies", { multiline: true })
      ),
    ];
  }

  _memory() {
    const on = this._on("memory", "enabled", false);
    return [
      this._head("Memory", "Lets this assistant remember what the two people tell it. It is never used for an unknown voice."),
      this._notSaved(),
      h(
        "div",
        { class: "card" },
        this._switchField("memory", "enabled", false, { redraw: true }),
        on && this._entityField("memory", "agent", "conversation", "Choose an agent"),
        on && this._switchField("memory", "auto_save", true),
        on && this._switchField("memory", "shared", true),
        on && this._switchField("memory", "use_global", true),
        on && this._numberField("memory", "max_items", 10, 200, 1, "facts per list", 50)
      ),
      h(
        "div",
        { class: "card" },
        h("h3", {}, "What is remembered"),
        h("p", { class: "intro" }, "Changes here are saved at once. A private fact is kept from the other person."),
        this._router.memory.map((list) => this._factList(list))
      ),
    ];
  }

  _factList(list) {
    const name = list.label.replace(/\s*\(\d+\)$/, "");
    const input = h("input", {
      type: "text",
      placeholder: "Add a fact, for example: prefers tea in the evening",
      "aria-label": `Add a fact to ${name}`,
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
      "details",
      {
        class: "list",
        open: this._openLists.has(list.key),
        ontoggle: (ev) => {
          if (ev.target.open) this._openLists.add(list.key);
          else this._openLists.delete(list.key);
        },
      },
      h("summary", {}, name, h("span", { class: "count" }, `${list.items.length}`)),
      h(
        "div",
        {},
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
                      title: item.private ? "Only for this person. Select to share it." : "Shared. Select to keep it private.",
                      onclick: () =>
                        this._saveFacts(
                          list,
                          list.items.map((other) => (other.id === item.id ? { ...other, private: !item.private } : other))
                        ),
                    },
                    item.private ? "Private" : "Shared"
                  ),
                  h(
                    "button",
                    {
                      class: "icon-btn",
                      disabled: busy,
                      "aria-label": `Forget: ${item.text}`,
                      title: "Forget this",
                      onclick: () => this._saveFacts(list, list.items.filter((other) => other.id !== item.id)),
                    },
                    "×"
                  )
                )
              )
            )
          : h("div", { class: "empty" }, "Nothing is remembered here yet."),
        h("div", { class: "add-fact" }, input, h("button", { class: "btn", disabled: busy, onclick: add }, "Add"))
      )
    );
  }

  _reminders() {
    return [
      this._head("Reminders", this._text("step.settings.sections.reminders.description")),
      this._notSaved(),
      h(
        "div",
        { class: "card" },
        this._switchField("reminders", "enabled", false),
        this._textField("reminders", "failure_line", { placeholder: "I could not set that reminder. Please ask me again." })
      ),
      h(
        "div",
        { class: "card" },
        h("h3", {}, "Spoken announcements"),
        h("p", { class: "intro" }, "Who writes the line when a reminder or a calendar event is spoken. The agent must have no tools."),
        this._entityField("reminders", "agent", "conversation", "Use the agent that writes refusals"),
        this._textField("reminders", "persona", { multiline: true })
      ),
      h(
        "div",
        { class: "card" },
        h("h3", {}, "Reminder calendars"),
        h("p", { class: "intro" }, "The calendars My assistant lists reminders from. Hold Ctrl or Cmd to choose several."),
        this._entitiesField("reminders", "owner_calendars", "calendar"),
        this._entitiesField("reminders", "partner_calendars", "calendar"),
        this._entitiesField("reminders", "household_calendars", "calendar")
      ),
    ];
  }

  _calendars() {
    const router = this._router;
    const adding = this._calendar && this._calendar.original === null;
    return [
      this._head("Calendars", "Events from these calendars are spoken when they are due. This assistant writes the line, and your script speaks it."),
      this._notSaved(),
      h(
        "div",
        { class: "card" },
        router.calendars.length
          ? router.calendars.map((row) => this._calendarRow(row))
          : h("div", { class: "empty" }, "No calendars yet. Add one to have its events announced."),
        adding
          ? this._calendarEditor()
          : h(
              "div",
              { class: "row-actions" },
              h(
                "button",
                {
                  class: "btn",
                  disabled: !router.saved,
                  title: router.saved ? "" : "Save this assistant's settings once first.",
                  onclick: () => {
                    this._calendar = { original: null, row: { for: "household", all_day_day: "same", lead_minutes: 0 }, errors: {} };
                    this._render();
                  },
                },
                "Add a calendar"
              )
            )
      ),
      h(
        "div",
        { class: "card" },
        h("h3", {}, "For all calendars"),
        this._entityField("calendars", "script", "script", "No script: only fire the event"),
        this._field(
          "calendars",
          "all_day_time",
          h("input", {
            type: "time",
            value: String(this._value("calendars", "all_day_time") || "09:00:00").slice(0, 5),
            oninput: (ev) => this._set("calendars", "all_day_time", ev.target.value ? `${ev.target.value}:00` : undefined),
          })
        ),
        this._numberField("calendars", "catch_up_hours", 0, 48, 1, "hours", 3)
      ),
    ];
  }

  _calendarRow(row) {
    if (this._calendar && this._calendar.original === row.calendar) return this._calendarEditor();
    const state = this._hass && this._hass.states && this._hass.states[row.calendar];
    const name = (state && state.attributes && state.attributes.friendly_name) || row.calendar;
    const who = this._router.people[row.for] || "everyone";
    const time = row.all_day_time || String((this._router.settings.calendars || {}).all_day_time || "09:00").slice(0, 5);
    const parts = [
      `For ${who}`,
      `all-day events at ${time}${row.all_day_day === "before" ? " the day before" : ""}`,
      row.lead_minutes ? `timed events ${row.lead_minutes} min before` : "timed events at their start",
      row.contains ? `only “${row.contains}”` : "",
    ].filter(Boolean);
    return h(
      "div",
      { class: "cal" },
      h("div", { class: "what" }, h("div", { class: "title" }, name), h("div", { class: "sub" }, parts.join(", "))),
      h(
        "button",
        {
          class: "btn",
          onclick: () => {
            this._calendar = { original: row.calendar, row: clone(row), errors: {} };
            this._render();
          },
        },
        "Change"
      )
    );
  }

  _calendarEditor() {
    const editing = this._calendar;
    const row = editing.row;
    const label = (key) => this._text(`step.calendar_edit.data.${key}`, key);
    const help = (key) => this._text(`step.calendar_edit.data_description.${key}`);
    const set = (key, value) => {
      if (value === "" || value === undefined) delete row[key];
      else row[key] = value;
    };
    const field = (key, control) => {
      const input = control.matches("input, select, textarea") ? control : control.querySelector("input");
      input.id = `cal.${key}`;
      input.dataset.key = `cal.${key}`;
      return h(
        "div",
        { class: `field${editing.errors[key] ? " bad" : ""}` },
        h("label", { for: input.id }, label(key)),
        control,
        editing.errors[key] && h("div", { class: "problem" }, editing.errors[key]),
        help(key) && h("div", { class: "help" }, help(key))
      );
    };
    const people = this._router.people;
    return h(
      "div",
      { class: "editor" },
      h("h3", {}, editing.original === null ? "Add a calendar" : "Change this calendar"),
      editing.errors.base && h("div", { class: "notice error" }, editing.errors.base),
      field("calendar", this._select(this._entities("calendar"), row.calendar, (value) => set("calendar", value), "Choose a calendar")),
      field(
        "for",
        this._select(
          [
            ["household", "Everyone"],
            ["owner", people.owner],
            ["partner", people.partner],
          ],
          row.for || "household",
          (value) => set("for", value)
        )
      ),
      field("all_day_time", h("input", { type: "time", value: row.all_day_time || "", oninput: (ev) => set("all_day_time", ev.target.value) })),
      field(
        "all_day_day",
        this._select(
          [
            ["same", "On the day itself"],
            ["before", "The day before"],
          ],
          row.all_day_day || "same",
          (value) => set("all_day_day", value)
        )
      ),
      field(
        "lead_minutes",
        h("div", { class: "unit" }, h("input", {
          type: "number",
          min: 0,
          max: 1440,
          step: 1,
          value: row.lead_minutes || 0,
          oninput: (ev) => set("lead_minutes", Number(ev.target.value || 0)),
        }), "minutes")
      ),
      field("contains", h("input", { type: "text", value: row.contains || "", oninput: (ev) => set("contains", ev.target.value) })),
      field("hint", h("textarea", { value: row.hint || "", oninput: (ev) => set("hint", ev.target.value) })),
      h(
        "div",
        { class: "row-actions" },
        h("button", { class: "btn primary", onclick: () => this._saveCalendar(false) }, "Save calendar"),
        h(
          "button",
          {
            class: "btn",
            onclick: () => {
              this._calendar = null;
              this._render();
            },
          },
          "Cancel"
        ),
        h("span", { class: "spacer" }),
        editing.original !== null &&
          h(
            "button",
            {
              class: "btn danger",
              onclick: () => {
                if (window.confirm("Stop announcing this calendar?")) this._saveCalendar(true);
              },
            },
            "Remove"
          )
      )
    );
  }

  // Avatars: stored as config subentries of the assistant (avatar_flow.py, panel_api.ws_save_avatar).

  _avatarCard() {
    const router = this._router;
    const avatars = router.avatars || [];
    const adding = this._avatar && this._avatar.original === null;
    const name = router.assistant ? router.assistant.charAt(0).toUpperCase() + router.assistant.slice(1) : router.title;
    return h(
      "div",
      { class: "card" },
      h("h3", {}, "Avatar"),
      h(
        "p",
        { class: "intro" },
        "A face on a screen that moves its lips to what this assistant says, through a LiveTalking server. The satellite still speaks as before. After adding one, choose its text-to-speech entity in this assistant's voice pipeline (Settings > Voice assistants) and choose the voice again."
      ),
      this._chatVoiceReminder(),
      avatars.length ? avatars.map((row) => this._avatarRow(row)) : !adding && h("div", { class: "empty" }, "No avatar."),
      adding
        ? this._avatarEditor()
        : h(
            "div",
            { class: "row-actions" },
            h(
              "button",
              {
                class: "btn",
                disabled: !!this._avatar,
                onclick: () => {
                  this._avatar = { original: null, row: { name: `${name} avatar`, engine: "", url: "", avatar_id: "", enabled: true }, errors: {} };
                  this._render();
                },
              },
              "Add an avatar"
            )
          )
    );
  }

  /**
   * Voice for chats should follow the avatar: the avatar entity while one is on, the
   * normal engine again once it is off. A reminder only; the page never changes it itself.
   */
  _chatVoiceReminder() {
    const engine = this._value("chat", "tts_engine") || "";
    // The first avatar that is on, as avatar_devices.wish_for picks it for the satellites.
    const mine = (this._router.avatars || []).filter((a) => a.enabled && a.entity_id);
    const anyAvatar = new Set(this._data.routers.flatMap((r) => (r.avatars || []).map((a) => a.entity_id)).filter(Boolean));
    if (mine.length && !mine.some((a) => a.entity_id === engine)) {
      const wanted = mine[0].entity_id;
      return h(
        "div",
        { class: "notice after", role: "status" },
        `The avatar is on: choose ${wanted} as the text-to-speech engine of Voice for chats (under Chat show), so chat lines reach the avatar on the LiveTalking page too. Satellites in avatar mode already speak them through the avatar.`,
        " ",
        h(
          "button",
          {
            class: "btn small",
            onclick: () => {
              this._set("chat", "tts_engine", wanted);
              this._render();
            },
          },
          `Use ${wanted}`
        )
      );
    }
    if (!mine.length && anyAvatar.has(engine)) {
      return h(
        "div",
        { class: "notice after", role: "status" },
        `Voice for chats (under Chat show) uses the avatar ${engine}, but no avatar of this assistant is on. Choose this assistant's normal text-to-speech engine there again.`
      );
    }
    return null;
  }

  _avatarRow(row) {
    if (this._avatar && this._avatar.original === row.id) return this._avatarEditor();
    const state = this._hass && this._hass.states && this._hass.states[row.engine];
    const engine = (state && state.attributes && state.attributes.friendly_name) || row.engine;
    const parts = [
      `Speaks through ${engine}`,
      row.enabled ? `sends to ${row.url}` : "off",
      row.avatar_id && `avatar ${row.avatar_id}`,
      row.entity_id ? `entity ${row.entity_id}` : "its entity is being created",
    ].filter(Boolean);
    return h(
      "div",
      { class: "cal" },
      h("div", { class: "what" }, h("div", { class: "title" }, row.name), h("div", { class: "sub" }, parts.join(", "))),
      h(
        "button",
        {
          class: "btn",
          disabled: !!this._avatar,
          onclick: () => {
            this._avatar = { original: row.id, row: { name: row.name, engine: row.engine, url: row.url, avatar_id: row.avatar_id || "", enabled: row.enabled }, errors: {} };
            this._render();
          },
        },
        "Change"
      )
    );
  }

  _avatarEditor() {
    const editing = this._avatar;
    const row = editing.row;
    // Every avatar of every assistant: an avatar never speaks through another one.
    const avatarIds = new Set(this._data.routers.flatMap((r) => (r.avatars || []).map((a) => a.entity_id)).filter(Boolean));
    const engines = this._entities("tts").filter(([id]) => !avatarIds.has(id));
    const field = (key, label, control, help) => {
      const input = control.matches("input, select, textarea") ? control : control.querySelector("input");
      input.id = `avatar.${key}`;
      input.dataset.key = `avatar.${key}`;
      return h(
        "div",
        { class: `field${editing.errors[key] ? " bad" : ""}` },
        h("label", { for: input.id }, label),
        control,
        editing.errors[key] && h("div", { class: "problem" }, editing.errors[key]),
        help && h("div", { class: "help" }, help)
      );
    };
    return h(
      "div",
      { class: "editor" },
      h("h3", {}, editing.original === null ? "Add an avatar" : "Change this avatar"),
      editing.errors.base && h("div", { class: "notice error" }, editing.errors.base),
      field(
        "name",
        "Name",
        h("input", { class: "input", type: "text", value: row.name, oninput: (ev) => (row.name = ev.target.value) }),
        "The name of its text-to-speech entity: Ada avatar becomes tts.ada_avatar."
      ),
      field(
        "engine",
        "Text-to-speech engine",
        this._select(engines, row.engine, (value) => (row.engine = value), "Choose an engine"),
        "The engine that makes the speech. Change it here to switch engines, then choose the voice again in the pipeline."
      ),
      field(
        "url",
        "LiveTalking address",
        h("input", {
          class: "input",
          type: "url",
          placeholder: "http://livetalking.local:8010",
          value: row.url,
          oninput: (ev) => (row.url = ev.target.value),
        }),
        "Everyone watching LiveTalking hears this assistant."
      ),
      field(
        "avatar_id",
        "LiveTalking avatar",
        h("input", { class: "input", type: "text", placeholder: "ada", value: row.avatar_id || "", oninput: (ev) => (row.avatar_id = ev.target.value) }),
        "The avatar a satellite in avatar mode asks LiveTalking for. Leave empty for the server's default."
      ),
      this._plainSwitch(
        "Avatar on",
        "When on, satellites whose wake word uses this assistant show the avatar and speak through it, if their firmware has avatar mode, and the LiveTalking page gets a copy. When off, everything goes back to normal.",
        !!row.enabled,
        (on) => (row.enabled = on)
      ),
      h(
        "div",
        { class: "row-actions" },
        h("button", { class: "btn primary", disabled: !!editing.busy, onclick: () => this._saveAvatar(false) }, editing.busy ? "Saving…" : "Save"),
        h(
          "button",
          {
            class: "btn",
            disabled: !!editing.busy,
            onclick: () => {
              this._avatar = null;
              this._render();
            },
          },
          "Cancel"
        ),
        h("span", { class: "spacer" }),
        editing.original !== null &&
          h(
            "button",
            {
              class: "btn danger",
              disabled: !!editing.busy,
              onclick: () => {
                if (window.confirm("Remove this avatar? Its text-to-speech entity goes too, so a pipeline that uses it needs another engine.")) {
                  this._saveAvatar(true);
                }
              },
            },
            "Remove"
          )
      )
    );
  }

  async _saveAvatar(remove) {
    const editing = this._avatar;
    if (!editing || editing.busy) return;
    editing.busy = true;
    this._render();
    try {
      const result = await this._call("save_avatar", {
        entry_id: this._routerId,
        subentry_id: editing.original,
        ...(remove ? { remove: true } : { avatar: editing.row }),
      });
      if (result.ok) {
        this._replaceRouter(result.router);
        this._avatar = null;
        this._toast(remove ? "Avatar removed" : "Avatar saved");
        // Adding or renaming reloads the assistant; fetch its avatars again once the entity exists.
        if (!remove) setTimeout(() => this._refreshAvatars(result.router.entry_id), 2000);
      } else {
        editing.errors = result.errors;
      }
    } catch (err) {
      editing.errors = { base: reason(err) || "Saving failed." };
    }
    editing.busy = false;
    this._render();
  }

  /** Fetch one assistant's avatars again, without touching unsaved changes on the page. */
  async _refreshAvatars(entryId) {
    try {
      const data = await this._call("get");
      const fresh = data.routers.find((r) => r.entry_id === entryId);
      const current = this._data && this._data.routers.find((r) => r.entry_id === entryId);
      if (fresh && current) {
        current.avatars = fresh.avatars;
        this._render();
      }
    } catch (err) {
      // Only a nicer list: the next visit shows the entity anyway.
    }
  }

  // Favorites.

  /** Fetch the lists when the section opens, and again after the settings are saved. */
  _wantFavorites() {
    const fav = this._fav;
    if (!this._data || this._guide || this._section !== "favorites" || fav.loading || !fav.stale) return;
    this._loadFavorites();
  }

  async _loadFavorites() {
    const fav = this._fav;
    fav.loading = true;
    fav.stale = false;
    try {
      const data = await this._call("favorites/get");
      // Speakers chosen here and not saved yet are kept.
      if (!fav.data || !this._favSpeakers || same(this._favSpeakers, fav.data.overrides)) {
        this._favSpeakers = clone(data.overrides);
      }
      fav.data = data;
      fav.error = "";
    } catch (err) {
      fav.error = reason(err) || "?";
    }
    fav.loading = false;
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
        this._fav.data.lists[list] = result.items;
        if (form && this._favForm === form) this._favForm = null;
        this._toast(done);
      } else {
        text = Object.values(result.errors).join(" ");
      }
    } catch (err) {
      text = reason(err) || "Saving failed.";
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
      form.results = result.candidates || [];
      form.message = result.message || "";
    } catch (err) {
      form.error = reason(err) || "Searching failed.";
    }
    form.searching = false;
    this._render();
  }

  async _saveSpeakers() {
    if (this._favBusy) return;
    this._favBusy = true;
    this._favSpeakersError = "";
    this._render();
    try {
      const result = await this._call("favorites/overrides/save", { overrides: this._favSpeakers });
      if (result.ok) {
        this._fav.data.overrides = result.overrides;
        this._favSpeakers = clone(result.overrides);
        this._toast("Speakers saved");
      } else {
        this._favSpeakersError = Object.values(result.errors).join(" ");
      }
    } catch (err) {
      this._favSpeakersError = reason(err) || "Saving failed.";
    }
    this._favBusy = false;
    this._render();
  }

  _openFavoriteForm(form) {
    this._favForm = { error: "", ...form };
    this._favError = null;
    this._render();
  }

  _favorites() {
    const fav = this._fav;
    const head = this._head("Favorites", this._text("step.settings.sections.favorites.description"));
    const failed = fav.error && h("div", { class: "notice error", role: "alert" }, `The favorites could not be loaded: ${fav.error}`);
    const data = fav.data;
    const lists =
      data &&
      h(
        "div",
        { class: "card" },
        h("h3", {}, "Lists"),
        h("p", { class: "intro" }, "Changes here are saved at once. Each person's list is used by every assistant with favorites on; the household's by everyone."),
        FAVORITE_LISTS.map((list) => this._favoriteList(list, data.names[list] || list))
      );
    return [
      head,
      this._notSaved(),
      h(
        "div",
        { class: "card" },
        this._switchField("favorites", "enabled", false, { redraw: true }),
        this._favoriteToolsWarning()
      ),
      failed,
      !data && !failed && h("div", { class: "loading" }, "Loading favorites…"),
      data && this._favoriteConflicts(),
      lists,
      data && this._favoriteSpeakers(),
    ];
  }

  /** The selected assistant's main agent cannot use the tools while favorites are on. */
  _favoriteToolsWarning() {
    const data = this._fav.data;
    const assistant = data && data.assistants.find((item) => item.entry_id === this._routerId);
    if (!assistant || assistant.tools_on !== false || !this._on("favorites", "enabled", false)) return null;
    const agent = (this._router.settings.agents || {}).agent;
    const state = agent && this._hass && this._hass.states && this._hass.states[agent];
    const name = (state && state.attributes && state.attributes.friendly_name) || agent || "the main agent";
    return h(
      "div",
      { class: "notice after" },
      `Switch on the Voice Assistant Manager tools for ${name} next to Assist, under that agent's settings, so it can use favorites for requests the quick path does not catch.`
    );
  }

  _favoriteConflicts() {
    const data = this._fav.data;
    return Object.entries(data.conflicts)
      .filter(([, titles]) => titles.length)
      .map(([person, titles]) => {
        const name = data.names[`person:${person}`] || person;
        return h(
          "div",
          { class: "notice error" },
          `${name} is not the same person on every assistant with favorites on (${titles.join(", ")}), so their own favorites are not used. The household's still are.`
        );
      });
  }

  _favoriteList(list, name) {
    const items = this._fav.data.lists[list] || [];
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
        : h("div", { class: "empty" }, "No favorites here yet."),
      form && form.mode === "search" && this._favoriteFinder(form),
      form && form.mode === "stream" && this._favoriteStream(form),
      !(form && form.mode !== "edit") &&
        h(
          "div",
          { class: "row-actions" },
          h(
            "button",
            { class: "btn small", disabled: busy, onclick: () => this._openFavoriteForm({ list, mode: "search", query: "", results: null, message: "", searching: false }) },
            "Find with Music Assistant"
          ),
          h(
            "button",
            { class: "btn small", disabled: busy, onclick: () => this._openFavoriteForm({ list, mode: "stream", name: "", url: "" }) },
            "Add a stream address"
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
          h("span", { class: "tag" }, FAVORITE_KINDS[item.kind] || item.kind),
          item.aliases.length > 0 && h("span", {}, `Also: ${item.aliases.join(", ")}`)
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
            "aria-label": `Edit “${item.name}”`,
            onclick: () => this._openFavoriteForm({ list, mode: "edit", id: item.id, name: item.name, aliases: item.aliases.join(", ") }),
          },
          "Edit"
        ),
        h(
          "button",
          {
            class: "btn small danger",
            disabled: busy,
            "aria-label": `Remove “${item.name}”`,
            onclick: () => {
              if (window.confirm(`Remove “${item.name}” from the favorites?`)) {
                this._changeFavorite(list, "remove", { favorite_id: item.id }, null, "Favorite removed");
              }
            },
          },
          "Remove"
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
        save && h("button", { class: "btn primary", disabled: this._favBusy, onclick: save }, this._favBusy ? "Saving…" : saveLabel),
        h(
          "button",
          {
            class: "btn",
            onclick: () => {
              this._favForm = null;
              this._render();
            },
          },
          save ? "Cancel" : "Close"
        )
      ),
    ];
  }

  _favoriteEditor(form) {
    const save = () => {
      const aliases = form.aliases.split(",").map((alias) => alias.trim()).filter(Boolean);
      const data = { name: form.name.trim(), aliases };
      this._changeFavorite(form.list, "update", { favorite_id: form.id, data }, form, "Favorite saved");
    };
    return h(
      "div",
      { class: "fav-form" },
      h("h3", {}, "Change this favorite"),
      h("div", { class: "field" }, h("label", { for: "fav-name" }, "Name"), this._favoriteInput(form, "name", { maxLength: MAX_FAVORITE_NAME })),
      h(
        "div",
        { class: "field" },
        h("label", { for: "fav-aliases" }, "Other names"),
        this._favoriteInput(form, "aliases", { placeholder: "For example: the news, morning radio" }),
        h("div", { class: "help" }, "Separated by commas, 5 at most. Asking for any of them plays this favorite.")
      ),
      this._favoriteFormEnd(form, "Save", save)
    );
  }

  _favoriteFinder(form) {
    const query = this._favoriteInput(form, "query", {
      maxLength: MAX_FAVORITE_NAME,
      placeholder: "A station, podcast or playlist",
      "aria-label": "Name to find",
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
        "Favorite added"
      );
    const results = form.results || [];
    return h(
      "div",
      { class: "fav-form" },
      h("h3", {}, "Find with Music Assistant"),
      h(
        "div",
        { class: "find" },
        query,
        h("button", { class: "btn", disabled: form.searching, onclick: () => this._searchFavorites(form) }, form.searching ? "Searching…" : "Search")
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
                h("div", { class: "sub" }, h("span", { class: "tag" }, FAVORITE_KINDS[candidate.kind] || candidate.kind))
              ),
              h(
                "div",
                { class: "side" },
                h("button", { class: "btn small primary", disabled: this._favBusy, "aria-label": `Add “${candidate.name}”`, onclick: () => add(candidate) }, "Add")
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
      this._changeFavorite(form.list, "add", { data }, form, "Favorite added");
    };
    return h(
      "div",
      { class: "fav-form" },
      h("h3", {}, "Add a stream address"),
      h("div", { class: "field" }, h("label", { for: "fav-name" }, "Name"), this._favoriteInput(form, "name", { maxLength: MAX_FAVORITE_NAME, placeholder: "For example: Radio 2" })),
      h(
        "div",
        { class: "field" },
        h("label", { for: "fav-url" }, "Stream address"),
        this._favoriteInput(form, "url", { maxLength: MAX_FAVORITE_URL, placeholder: "https://", inputMode: "url" }),
        h("div", { class: "help" }, "An http:// or https:// address that plays the stream itself.")
      ),
      this._favoriteFormEnd(form, "Add", save)
    );
  }

  _favoriteSpeakers() {
    const data = this._fav.data;
    const chosen = this._favSpeakers || {};
    const players = data.players.map((player) => [player.entity_id, `${player.name} (${player.entity_id})`]);
    const playerName = (id) => (data.players.find((player) => player.entity_id === id) || {}).name || id;
    const rows = data.satellites.map((satellite) => {
      const select = this._select(
        players,
        chosen[satellite.device_id] || "",
        (value) => {
          if (value) chosen[satellite.device_id] = value;
          else delete chosen[satellite.device_id];
          this._favSpeakersError = "";
          this._render();
        },
        "Its own speaker"
      );
      select.setAttribute("aria-label", `Speaker for ${satellite.name}`);
      return h(
        "div",
        { class: "speaker" },
        h(
          "div",
          {},
          h("div", { class: "title" }, satellite.name),
          h("div", { class: "sub" }, satellite.player ? `Its own speaker: ${playerName(satellite.player)}` : "No speaker of its own: choose one to play favorites there.")
        ),
        select
      );
    });
    const changed = !same(chosen, data.overrides);
    return h(
      "div",
      { class: "card" },
      h("h3", {}, "Speakers"),
      h("p", { class: "intro" }, "Where a favorite plays when it is asked for on a satellite without naming a speaker. A satellite plays on its own speaker unless you choose another one here."),
      rows.length ? rows : h("div", { class: "empty" }, "No satellites found."),
      this._favSpeakersError && h("div", { class: "notice error after", role: "alert" }, this._favSpeakersError),
      rows.length > 0 &&
        h(
          "div",
          { class: "row-actions" },
          h("button", { class: "btn primary", disabled: this._favBusy || !changed, onclick: () => this._saveSpeakers() }, "Save speakers")
        )
    );
  }

  // The chat show.

  /** A text of the chat show from en.json (options.chat.*), with {placeholders} filled in. */
  _c(key, vars) {
    return this._fill(this._text(`chat.${key}`, key), vars);
  }

  get _chatShowing() {
    return !!this._data && !!this._router && !this._guide && this._section === "chat" && this.isConnected;
  }

  /** Fetch the chat when its section opens, and again every 2 s while a chat runs there. */
  _wantChat() {
    const chat = this._chat;
    if (!this._chatShowing) {
      this._stopChatPoll();
      return;
    }
    if (chat.loading || chat.timer) return;
    if (chat.stale) {
      this._loadChat();
    } else if (chat.state && chat.state.chat.running) {
      chat.timer = setTimeout(() => {
        chat.timer = null;
        chat.stale = true;
        this._wantChat();
      }, CHAT_POLL_MS);
    }
  }

  _stopChatPoll() {
    clearTimeout(this._chat.timer);
    this._chat.timer = null;
  }

  async _loadChat() {
    const chat = this._chat;
    chat.loading = true;
    chat.stale = false;
    let changed;
    try {
      const state = await this._call("chat/state");
      changed = !same(state, chat.state) || !!chat.loadError;
      chat.state = state;
      chat.loadError = "";
      if (!chat.form) chat.form = this._chatForm(state);
    } catch (err) {
      const text = reason(err) || "?";
      changed = chat.loadError !== text;
      chat.loadError = text;
    }
    chat.loading = false;
    // Drawn again only for news, so a list someone is choosing from stays open.
    if (changed && this._chatShowing) this._render();
    else this._wantChat();
  }

  /** The first choices: those of the last chat, else the first two assistants; saved limits. */
  _chatForm(state) {
    const last = state.chat;
    const ids = state.routers.map((router) => router.entry_id);
    // An assistant of the last chat that is no longer offered is not kept.
    const id = (index) => (ids.includes(last.assistant_ids[index]) ? last.assistant_ids[index] : "");
    return {
      a: id(0) || ids[0] || "",
      b: id(1) || ids[1] || "",
      speaker: last.speaker,
      topic: last.topic,
      limits: this._chatLimits(state.defaults),
    };
  }

  _chatLimits(defaults) {
    const limits = Object.fromEntries(CHAT_LIMITS.map(({ key }) => [key, defaults[key]]));
    limits.listen = !!defaults.listen;
    return limits;
  }

  /** The chosen assistants and speaker, and whether Start may be used with them. */
  _chatChoice() {
    const { state, form } = this._chat;
    const router = (id) => state.routers.find((item) => item.entry_id === id);
    const a = router(form.a);
    const b = router(form.b);
    const speaker = state.speakers.find((item) => item.entity_id === form.speaker);
    const ready = !!a && !!b && a !== b && !!speaker && a.has_writer && b.has_writer;
    return { a, b, speaker, ready };
  }

  _chatShow() {
    const chat = this._chat;
    const head = this._head(this._c("title"), this._c("lede"));
    const failed = chat.loadError && h("div", { class: "notice error" }, this._c("load_failed", { reason: chat.loadError }));
    if (!chat.state) return [head, failed || h("div", { class: "loading" }, this._c("loading"))];
    return [head, failed, this._chatSetup(), this._chatTranscript()];
  }

  _chatSetup() {
    const chat = this._chat;
    const { state, form } = chat;
    const running = state.chat.running;
    const locked = running || !!chat.busy;
    const choice = this._chatChoice();
    const changed = (key) => (value) => {
      form[key] = value;
      chat.error = "";
      this._render();
    };
    const routers = state.routers.map((router) => [router.entry_id, router.title]);
    const assistant = (key) => {
      const control = this._select(routers, form[key], changed(key), this._c("choose_assistant"));
      control.disabled = locked;
      const help = key === "a" ? this._c("assistant_a_help") : "";
      return this._field("chatshow", key, control, { label: this._c(`assistant_${key}`), help });
    };
    const speaker = this._speakerSelect(state.speakers, form.speaker, changed("speaker"));
    speaker.disabled = locked;
    const topic = h("input", {
      type: "text",
      maxLength: 200,
      value: form.topic,
      disabled: locked,
      oninput: (ev) => {
        form.topic = ev.target.value;
      },
    });
    const label = (key, text) => (chat.busy === key ? this._c("working") : text);
    return h(
      "div",
      { class: "card" },
      h("h3", {}, this._c("setup")),
      h("p", { class: "intro" }, this._c("setup_intro")),
      routers.length < 2 && h("div", { class: "notice" }, this._c("one_router")),
      h("div", { class: "pair" }, assistant("a"), assistant("b")),
      form.a && form.a === form.b && h("div", { class: "notice" }, this._text("error.chat_same_assistant")),
      this._chatWarnings([choice.a, choice.b]),
      this._field("chatshow", "speaker", speaker, {
        label: this._c("speaker"),
        help: state.speakers.length ? this._c("speaker_help") : this._c("no_speakers"),
      }),
      choice.speaker &&
        (choice.speaker.voice
          ? h("div", { class: "help below" }, this._c("voice"))
          : h("div", { class: "notice below" }, this._c("no_voice"))),
      this._field("chatshow", "topic", topic, { label: this._c("topic"), help: this._c("topic_help") }),
      h("div", { class: "limits-head" }, h("h3", {}, this._c("limits")), h("div", { class: "help" }, this._c("limits_help"))),
      h("div", { class: "limits" }, CHAT_LIMITS.map((limit) => this._chatLimitField(limit, locked))),
      choice.speaker && choice.speaker.listen && this._chatListenField(locked),
      chat.error && h("div", { class: "notice error after" }, chat.error),
      h(
        "div",
        { class: "row-actions" },
        h("button", { class: "btn primary", disabled: locked || !choice.ready, onclick: () => this._startChat() }, label("start", this._c("start"))),
        // Stopped already: the last line is finishing, so there is nothing left to stop.
        h("button", { class: "btn danger", disabled: !running || !!state.chat.ended || !!chat.busy, onclick: () => this._stopChat() }, label("stop", this._c("stop"))),
        h("span", { class: "spacer" }),
        h("button", { class: "btn", disabled: locked, onclick: () => this._saveChatDefaults() }, label("defaults", this._c("save_defaults")))
      ),
      h("div", { class: "help after" }, this._c("grace_note"))
    );
  }

  /** Satellites and media players in two groups. A speaker no longer offered is not kept. */
  _speakerSelect(speakers, current, onchange) {
    const known = speakers.some((item) => item.entity_id === current);
    const group = (domain, label) => {
      const list = speakers.filter((item) => item.domain === domain);
      return (
        list.length > 0 &&
        h(
          "optgroup",
          { label },
          list.map((item) => h("option", { value: item.entity_id, selected: item.entity_id === current }, item.name))
        )
      );
    };
    return h(
      "select",
      { class: "input", onchange: (ev) => onchange(ev.target.value) },
      h("option", { value: "", selected: !known }, this._c("choose_speaker")),
      group("assist_satellite", this._c("satellites")),
      group("media_player", this._c("media_players"))
    );
  }

  /** What keeps a chosen assistant from chatting well, with the way to its settings. */
  _chatWarnings(routers) {
    const warning = (text, linkText, router, section, field) => {
      // The settings can only be opened for an assistant this page lists.
      const listed = this._data.routers.some((item) => item.entry_id === router.entry_id);
      return h(
        "div",
        { class: "notice banner" },
        h("span", {}, text),
        listed && h("button", { class: "link", onclick: () => this._openSettings(router.entry_id, section, field) }, linkText)
      );
    };
    return [...new Set(routers.filter(Boolean))].map((router) => {
      const name = router.title;
      if (!router.has_writer) return warning(this._c("no_writer", { name }), this._c("set_writer"), router, "reminders", "reminders.agent");
      if (!router.has_character) {
        return warning(this._c("no_character", { name }), this._c("set_character"), router, "assistant", "chat.character");
      }
      return null;
    });
  }

  /** Open an assistant's settings at one field, selecting that assistant first. */
  _openSettings(entryId, section, fieldId) {
    if (entryId !== this._routerId) {
      this._pickRouter(entryId);
      // Kept on the old assistant (changes not left), or its setup guide opened instead.
      if (entryId !== this._routerId || this._guide) return;
    }
    this._go(section);
    // Focusing the field scrolls it into sight.
    const field = this.shadowRoot.getElementById(fieldId);
    if (field) field.focus();
  }

  /** Only for a satellite that can start a conversation; the chat still works if it cannot. */
  _chatListenField(locked) {
    const form = this._chat.form;
    const id = "chatshow.listen";
    const control = h("input", {
      type: "checkbox",
      id,
      checked: !!form.limits.listen,
      disabled: locked,
      onchange: (ev) => {
        form.limits.listen = ev.target.checked;
      },
    });
    return h(
      "div",
      { class: "field switch listen" },
      h("label", { for: id }, this._c("listen")),
      h("div", { class: "toggle" }, control, h("span", {})),
      h("div", { class: "help" }, this._c("listen_help"))
    );
  }

  _chatLimitField({ key, min, max, step }, locked) {
    const form = this._chat.form;
    const control = h("input", {
      type: "number",
      min,
      max,
      step,
      value: form.limits[key] === undefined ? "" : form.limits[key],
      disabled: locked,
      // Left empty, the saved default is used.
      oninput: (ev) => {
        if (ev.target.value === "") delete form.limits[key];
        else form.limits[key] = Number(ev.target.value);
      },
    });
    return this._field("chatshow", key, control, { label: this._c(key) });
  }

  _chatTranscript() {
    const { state } = this._chat;
    const chat = state.chat;
    if (!chat.assistants.length) return null;
    const [a, b] = chat.assistants;
    const found = state.speakers.find((item) => item.entity_id === chat.speaker);
    const speaker = found ? found.name : chat.speaker;
    const tone = (who) => ["one", "two"][chat.assistants.indexOf(who)] || "person";
    const fine = CHAT_ENDED_FINE.includes(chat.ended);
    // Ended but still running: a stopped chat lets the line being spoken finish.
    const finishing = chat.running && !!chat.ended;
    return h(
      "div",
      { class: "card" },
      h(
        "div",
        { class: "chat-head" },
        h("h3", {}, this._c("transcript")),
        chat.running && !finishing && h("span", { class: "live" }, h("span", { class: "dot ok" }), this._c("live"))
      ),
      h("p", { class: "intro" }, this._c(chat.running ? "running" : "ended", { a, b, speaker })),
      chat.topic && h("p", { class: "intro" }, this._c("topic_line", { topic: chat.topic })),
      chat.lines.length > 0 &&
        h(
          "ol",
          { class: "transcript" },
          chat.lines.map((line) => h("li", { class: tone(line.who) }, h("span", { class: "by" }, `${line.who}:`), " ", line.text))
        ),
      chat.running && !finishing && !chat.lines.length && h("div", { class: "empty" }, this._c("waiting")),
      finishing && h("div", { class: "notice after plain" }, this._c("finishing")),
      chat.ended && !finishing && h("div", { class: `notice after${fine ? " plain" : " error"}` }, this._text(`error.${chat.ended}`, chat.ended))
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
      if (result && result.ok === false) chat.error = Object.values(result.errors).join(" ");
    } catch (err) {
      chat.error = this._c("failed", { reason: reason(err) });
    }
    chat.busy = "";
    // Ask for the chat again now rather than at the next poll.
    this._stopChatPoll();
    chat.stale = true;
    this._render();
  }

  _startChat() {
    const form = this._chat.form;
    return this._chatAct("start", () =>
      this._call("chat/start", {
        a: form.a,
        b: form.b,
        speaker: form.speaker,
        topic: form.topic.trim(),
        limits: { ...form.limits },
      })
    );
  }

  _stopChat() {
    return this._chatAct("stop", () => this._call("chat/stop"));
  }

  _saveChatDefaults() {
    const chat = this._chat;
    return this._chatAct("defaults", async () => {
      const result = await this._call("chat/save_defaults", { limits: { ...chat.form.limits } });
      if (result.ok) {
        chat.form.limits = this._chatLimits(result.defaults);
        this._toast(this._c("defaults_saved"));
      }
      return result;
    });
  }

  // Status lines, with what the router can fix itself. Used by the Overview and the guide.

  _statusList(inGuide) {
    const sats = this._satellites;
    return [
      sats && sats.error && h("div", { class: "notice error" }, this._t("satellites.failed", { reason: sats.error })),
      this._statusLines(inGuide),
    ];
  }

  _statusLines(inGuide) {
    return h(
      "ul",
      { class: "status" },
      this._router.status.map((line) => {
        const attention = line.level === "warn" || line.level === "error";
        return h(
          "li",
          { class: line.level },
          h("span", { class: "mark" }),
          h("div", { class: "text" }, line.text, attention && this._fixPreview(line)),
          attention && this._statusButton(line, inGuide)
        );
      })
    );
  }

  /** What a fix will change, and how it went, under its status line. */
  _fixPreview(line) {
    const sats = this._satellites;
    const list = sats ? sats.list : [];
    if (line.fix === "create_helper") {
      const satellite = list.find((item) => item.device_id === line.target);
      // The helper id follows from the device id, so the change is named before the list is in.
      const helper = satellite ? satellite.helper : `input_text.voice_speaker_${line.target}`;
      return [h("div", { class: "will" }, this._t("satellites.helper_will", { helper })), this._result(`helper:${line.target}`)];
    }
    if (line.fix === "local_off") {
      const names = this._pipelineUsers(line.target);
      const name = this._pipelineName(line.target);
      const will = names.length
        ? this._t(names.length > 1 ? "pipelines.local_many" : "pipelines.local_one", {
            satellites: this._names(names),
            pipeline: name,
          })
        : this._t("fix.local_will");
      return [h("div", { class: "will" }, will), this._result(`local:${line.target}`)];
    }
    return null;
  }

  _statusButton(line, inGuide) {
    if (line.fix === "create_helper" || line.fix === "local_off") {
      const helper = line.fix === "create_helper";
      const state = this._actions[`${helper ? "helper" : "local"}:${line.target}`] || {};
      return h(
        "button",
        {
          class: "btn small",
          disabled: !!state.busy,
          onclick: () => (helper ? this._createHelper(line.target) : this._localOff(line.target)),
        },
        state.busy ? this._t("working") : this._t(`fix.${line.fix}`)
      );
    }
    // The tracking automation needs the names the voice match reports: the guide asks them.
    if (line.fix === "tracking") {
      const open = () => (inGuide ? this._guideGo(3) : this._openGuide(3));
      return h("button", { class: "link", onclick: open }, this._t("fix.tracking"));
    }
    const step = GUIDE_STEP_OF[line.section];
    if (inGuide) {
      return step && h("button", { class: "link", onclick: () => this._guideGo(step) }, this._t("fix.go_step", { n: step }));
    }
    const section = SECTIONS.find((s) => s.id === line.section);
    if (section) {
      return h("button", { class: "link", onclick: () => this._go(section.id) }, `Open ${section.label}`);
    }
    return h("button", { class: "link", onclick: () => this._openGuide(step || 1) }, this._t("fix.open_guide"));
  }

  // The setup guide.

  /** A text of the guide from en.json (options.guide.*), with {placeholders} filled in. */
  _t(key, vars) {
    return this._fill(this._text(`guide.${key}`, key), vars);
  }

  /** A text with its {placeholders} filled in. */
  _fill(text, vars) {
    for (const [name, value] of Object.entries(vars || {})) text = text.split(`{${name}}`).join(String(value));
    return text;
  }

  /** "A", "A and B", "A, B and C". */
  _names(names) {
    if (names.length < 2) return names.join("");
    return `${names.slice(0, -1).join(", ")} ${this._t("and")} ${names[names.length - 1]}`;
  }

  _newGuide(step) {
    const router = this._router;
    return {
      step,
      // A router that never finished the guide can put it off; one that did just closes it.
      skippable: ["new", "started", "dismissed"].includes(router.guide),
      style: "", // the style picked in step 2
      texts: null, // what guide/style answered, kept for step 5
      textsUsed: false,
      styleError: "",
      chosen: null, // Set of the device ids of the chosen satellites
      names: {}, // device id -> { owner, partner } as the voice match reports them
      wake: {}, // device id -> the select of the wake word picked in step 4
      plans: {}, // select -> what guide/pipeline_copy answered, for the preview
    };
  }

  _openGuide(step) {
    this._guide = this._newGuide(step);
    this._section = "overview";
    this._saveError = "";
    if (this._guide.skippable) this._rememberStep(step);
    this._guideGo(step);
  }

  /** The step a started guide reopens at; the first step for anything else. */
  _resumeStep(router) {
    const step = Number(router.guide_step);
    return router.guide === "started" && step >= 1 && step <= GUIDE_STEPS.length ? step : 1;
  }

  /**
   * Store the step a guide is at, so closing the page does not lose the place. Only for a
   * guide that is not finished: running a finished guide again leaves it finished.
   */
  async _rememberStep(step) {
    const id = this._routerId;
    try {
      await this._guideCall("state", { state: "started", step });
    } catch (err) {
      this._toast(this._t("failed", { reason: reason(err) }));
      return;
    }
    const router = this._data.routers.find((r) => r.entry_id === id);
    if (router) Object.assign(router, { guide: "started", guide_step: step });
  }

  _closeGuide() {
    this._guide = null;
    this._render();
    window.scrollTo({ top: 0 });
  }

  /** Where to pick up a guide that was put off: the first step that still has work. */
  _firstUnfinished() {
    const router = this._router;
    if (!router.saved) return 1;
    const has = (test) => router.status.some((line) => (line.level === "warn" || line.level === "error") && test(line));
    if (has((line) => line.section === "people")) return 2;
    if (has((line) => line.fix === "create_helper" || line.fix === "tracking")) return 3;
    if (has((line) => line.fix === "local_off" || line.section === "assistant")) return 4;
    if (router.status.some((line) => line.section === "satellites" && line.level === "info")) return 4;
    if (has((line) => line.section === "strangers")) return 5;
    if (has((line) => line.section === "memory")) return 6;
    return GUIDE_STEPS.length;
  }

  /** Show a step. keepError keeps a refused save's text, for the step that can fix it. */
  _guideGo(step, keepError) {
    const error = this._saveError;
    if (this._guide.skippable && step !== this._guide.step) this._rememberStep(step);
    this._guide.step = step;
    if (GUIDE_STEPS[step - 1] === "strangers") this._useStyleTexts();
    this._saveError = keepError ? error : "";
    this._render();
    window.scrollTo({ top: 0 });
  }

  /** Whether the guide and router on screen are still the ones an awaited call began with. */
  _still(guide, id) {
    return this._guide === guide && this._routerId === id;
  }

  async _guideNext() {
    if (this._busy) return;
    const guide = this._guide;
    const id = this._routerId;
    // Each step saves through the same rules as the sections; a refusal keeps the step open.
    if (guide.step > 1 && (this._dirty || !this._router.saved)) {
      this._busy = true;
      this._render();
      const saved = await this._saveDraft();
      this._busy = false;
      if (!saved || !this._still(guide, id)) {
        this._render();
        return;
      }
    }
    this._guideGo(guide.step + 1);
  }

  async _setGuideState(state) {
    const guide = this._guide;
    const id = this._routerId;
    try {
      await this._guideCall("state", { state });
    } catch (err) {
      if (this._still(guide, id)) this._saveError = this._t("failed", { reason: reason(err) });
      this._render();
      return;
    }
    if (this._still(guide, id)) this._guide = null;
    await this._refresh();
    this._render();
    window.scrollTo({ top: 0 });
  }

  /**
   * A guide command for the router on screen, or for data.entry_id when given. Throws the
   * refusal text when it says no.
   */
  async _guideCall(command, data) {
    const result = await this._call(`guide/${command}`, { entry_id: this._routerId, ...(data || {}) });
    if (!result.ok) throw new Error(Object.values(result.errors || {}).join(" "));
    return result;
  }

  /** Fetch the status and the satellites again, after something in Home Assistant changed. */
  async _refresh() {
    try {
      const data = await this._call("get");
      for (const router of data.routers) this._replaceRouter(router);
    } catch (err) {
      this._toast(this._t("failed", { reason: reason(err) }));
    }
    this._dropSatellites();
    if (this._guide) this._guide.plans = {};
  }

  /** Forget the satellites; a fetch still under way belongs to an older state and is ignored. */
  _dropSatellites() {
    this._sats = null;
    this._satsGen += 1;
  }

  get _satellites() {
    return this._sats && this._sats.entryId === this._routerId ? this._sats : null;
  }

  /** Fetch the satellites when what is on screen needs them and they are not there yet. */
  _wantSatellites() {
    const router = this._router;
    if (!router || this._satellites || this._satsLoading === this._satsGen) return;
    const inGuide = this._guide && ["satellites", "pipelines", "check"].includes(GUIDE_STEPS[this._guide.step - 1]);
    const onOverview = !this._guide && this._section === "overview" && router.status.some((line) => line.fix);
    if (inGuide || onOverview) this._loadSatellites();
  }

  async _loadSatellites() {
    const id = this._routerId;
    const gen = this._satsGen;
    this._satsLoading = gen;
    let sats;
    try {
      const result = await this._guideCall("satellites");
      sats = { entryId: id, list: result.satellites, payloads: result.helper_payloads, error: "" };
    } catch (err) {
      sats = { entryId: id, list: [], payloads: {}, error: reason(err) || "?" };
    }
    // Something changed while this was under way: its answer may be out of date.
    if (gen !== this._satsGen || id !== this._routerId) return;
    this._satsLoading = null;
    this._sats = sats;
    this._render();
  }

  /** The chosen satellites: at first those that already have a helper or use this router. */
  _chosen() {
    const guide = this._guide;
    const sats = this._satellites;
    if (!guide.chosen && sats && !sats.error) {
      const set = sats.list.filter((item) => item.helper_exists || item.wake_words.some((word) => word.uses_router));
      guide.chosen = new Set(set.map((item) => item.device_id));
    }
    return guide.chosen || new Set();
  }

  _pipelineUsers(pipelineId) {
    const sats = this._satellites;
    return (sats ? sats.list : [])
      .filter((item) => item.wake_words.some((word) => word.pipeline_id === pipelineId))
      .map((item) => item.name);
  }

  _pipelineName(pipelineId) {
    const sats = this._satellites;
    for (const item of sats ? sats.list : []) {
      const word = item.wake_words.find((w) => w.pipeline_id === pipelineId);
      if (word) return word.pipeline_name;
    }
    return pipelineId;
  }

  _draftPeople() {
    const people = this._router.people;
    return {
      owner: this._value("owner", "name") || people.owner,
      partner: this._value("partner", "name") || people.partner,
    };
  }

  // Buttons that change Home Assistant. Each runs only on its click, which names the change.

  /** Run one change; its result stays next to its button. The status is fetched again after. */
  async _act(key, work) {
    // Results are kept per router: a switch replaces this object, so a late result is dropped.
    const actions = this._actions;
    if ((actions[key] || {}).busy) return;
    actions[key] = { busy: true };
    this._render();
    let outcome;
    try {
      outcome = { ok: true, text: await work() };
    } catch (err) {
      outcome = { ok: false, text: reason(err) };
    }
    actions[key] = outcome;
    await this._refresh();
    // A fixed status line disappears from the Overview, so say what happened there too.
    if (outcome.ok) this._toast(outcome.text);
    this._render();
  }

  _createHelper(deviceId) {
    return this._act(`helper:${deviceId}`, async () => {
      const found = await this._guideCall("satellites");
      const satellite = found.satellites.find((item) => item.device_id === deviceId);
      const payload = found.helper_payloads[deviceId];
      if (!satellite || !payload) throw new Error(this._t("fix.no_satellite"));
      // Home Assistant would give a second helper of that name another id: never create one.
      if (satellite.helper_exists) return this._t("satellites.helper_exists", { helper: satellite.helper });
      let created;
      try {
        created = await this._hass.callWS({ type: "input_text/create", ...payload });
      } catch (err) {
        throw new Error(this._t("fix.helper_failed", { reason: reason(err) }));
      }
      const expected = satellite.helper.split(".")[1];
      if (created && created.id && created.id !== expected) {
        throw new Error(this._t("satellites.helper_other_id", { id: created.id, helper: satellite.helper }));
      }
      return this._t("satellites.helper_done", { helper: satellite.helper });
    });
  }

  _createTracking(satellite) {
    const names = { ...this._trackingNames(satellite) };
    return this._act(`tracking:${satellite.device_id}`, async () => {
      const built = await this._guideCall("tracking_automation", { device_id: satellite.device_id, names });
      const path = `config/automation/config/${encodeURIComponent(built.id)}`;
      // Never overwrite: an automation with this id may exist and no longer use the sensors.
      let exists = true;
      try {
        await this._hass.callApi("GET", path);
      } catch (err) {
        if (!err || err.status_code !== 404) {
          throw new Error(this._t("fix.automation_failed", { reason: reason(err) }));
        }
        exists = false;
      }
      if (exists) throw new Error(this._t("satellites.tracking_exists", { id: built.id }));
      try {
        await this._hass.callApi("POST", path, built.config);
      } catch (err) {
        throw new Error(this._t("fix.automation_failed", { reason: reason(err) }));
      }
      return this._t("satellites.tracking_done");
    });
  }

  _localOff(pipelineId) {
    return this._act(`local:${pipelineId}`, async () => {
      const result = await this._guideCall("local_off", { pipeline_id: pipelineId });
      if (!result.satellites.length) return this._t("pipelines.local_done_none");
      return this._t("pipelines.local_done", { satellites: this._names(result.satellites) });
    });
  }

  _useWakeWord(satellite, word, number) {
    // The router of the click, even if another one is picked while the pipeline is made.
    const entryId = this._routerId;
    return this._act(`wake:${word.select}`, async () => {
      const plan = await this._guideCall("pipeline_copy", { entry_id: entryId, select: word.select });
      let pipelineId = plan.reuse;
      if (!pipelineId) {
        try {
          const created = await this._hass.callWS({ type: "assist_pipeline/pipeline/create", ...plan.payload });
          pipelineId = created.id;
        } catch (err) {
          throw new Error(this._t("pipelines.pipeline_failed", { reason: reason(err) }));
        }
      }
      await this._guideCall("set_wake_word", { entry_id: entryId, select: word.select, pipeline_id: pipelineId });
      return this._t("pipelines.wake_done", { n: number, satellite: satellite.name });
    });
  }

  /** What guide/pipeline_copy would do for a wake word; fetched once, only to show it. */
  _plan(select) {
    const plans = this._guide.plans;
    if (!plans[select]) {
      plans[select] = { loading: true };
      this._guideCall("pipeline_copy", { select })
        .then(
          (result) => {
            plans[select] = result;
          },
          (err) => {
            plans[select] = { error: reason(err) };
          }
        )
        .then(() => this._render());
    }
    return plans[select];
  }

  async _pickStyle(style) {
    const guide = this._guide;
    const id = this._routerId;
    const people = this._draftPeople();
    try {
      const result = await this._guideCall("style", { style, owner: people.owner, partner: people.partner });
      // The texts are for this router's people: never put them into another router's draft.
      if (!this._still(guide, id)) return;
      guide.style = style;
      guide.texts = result.texts;
      guide.textsUsed = false;
      guide.styleError = "";
      this._set("owner", "persona", result.texts.owner.persona);
      this._set("partner", "persona", result.texts.partner.persona);
    } catch (err) {
      if (!this._still(guide, id)) return;
      guide.styleError = this._t("people.style_failed", { reason: reason(err) });
    }
    this._render();
  }

  /** Put the refusal texts of the picked style in, once, when step 5 opens. */
  _useStyleTexts() {
    const guide = this._guide;
    if (!guide.texts || guide.textsUsed) return;
    guide.textsUsed = true;
    for (const section of ["strangers", "near_miss"]) {
      for (const [key, value] of Object.entries(guide.texts[section] || {})) this._set(section, key, value);
    }
  }

  _trackingNames(satellite) {
    const names = this._guide.names;
    if (!names[satellite.device_id]) names[satellite.device_id] = this._draftPeople();
    return names[satellite.device_id];
  }

  // Drawing the guide.

  _guideView() {
    const draw = {
      welcome: () => this._guideWelcome(),
      people: () => this._guidePeople(),
      satellites: () => this._guideSatellites(),
      pipelines: () => this._guidePipelines(),
      strangers: () => this._guideStrangers(),
      extras: () => this._guideExtras(),
      check: () => this._guideCheck(),
    }[GUIDE_STEPS[this._guide.step - 1]];
    return [this._guideSteps(), draw(), this._guideFoot()];
  }

  _guideSteps() {
    const step = this._guide.step;
    const total = GUIDE_STEPS.length;
    return h(
      "div",
      { class: "guide-top" },
      h("div", { class: "step-of" }, `${this._t("title")} · ${this._t("step_of", { n: step, total })}`),
      h(
        "ol",
        { class: "steps" },
        GUIDE_STEPS.map((id, index) => {
          const n = index + 1;
          const name = this._t(`steps.${id}`);
          return h(
            "li",
            { class: n < step ? "past" : n === step ? "now" : "later" },
            h(
              "button",
              {
                class: "step",
                disabled: n >= step,
                "aria-current": n === step ? "step" : undefined,
                "aria-label": `${this._t("step_of", { n, total })}: ${name}`,
                onclick: () => this._guideGo(n),
              },
              h("span", { class: "num" }, n < step ? "✓" : String(n)),
              h("span", { class: "name" }, name)
            )
          );
        })
      )
    );
  }

  /**
   * The way to the field a refused save is about. Each Next saves every section, so the field
   * can be on a later step, which the step bar does not open yet.
   */
  _guideFixButton() {
    const section = this._saveSection;
    const step = GUIDE_FIELD_STEP[section];
    if (step) {
      if (step === this._guide.step) return null;
      return h("button", { class: "link", onclick: () => this._guideGo(step, true) }, this._t("fix.go_step", { n: step }));
    }
    const known = SECTIONS.find((s) => s.id === section);
    if (!known) return null;
    const open = () => {
      this._closeGuide();
      this._go(known.id);
    };
    return h("button", { class: "link", onclick: open }, this._t("fix.open_section", { section: known.label }));
  }

  _guideFoot() {
    const guide = this._guide;
    const last = guide.step === GUIDE_STEPS.length;
    const label = this._busy ? this._t("saving") : last ? this._t("done") : guide.step === 1 ? this._t("welcome.start") : this._t("next");
    return [
      this._saveError && h("div", { class: "notice error", role: "alert" }, this._saveError, " ", this._guideFixButton()),
      h(
        "div",
        { class: "guide-foot" },
        guide.step > 1 && h("button", { class: "btn", disabled: this._busy, onclick: () => this._guideGo(guide.step - 1) }, this._t("back")),
        h("span", { class: "spacer" }),
        guide.skippable
          ? h("button", { class: "link", disabled: this._busy, onclick: () => this._setGuideState("dismissed") }, this._t("skip"))
          : h("button", { class: "link", disabled: this._busy, onclick: () => this._closeGuide() }, this._t("close")),
        h(
          "button",
          {
            class: "btn primary",
            disabled: this._busy,
            onclick: () => (last ? this._setGuideState("done") : this._guideNext()),
          },
          label
        )
      ),
    ];
  }

  _guideWelcome() {
    return [
      this._head(this._t("welcome.title", { title: this._router.title }), this._t("welcome.lede")),
      h(
        "div",
        { class: "card" },
        h("h3", {}, this._t("welcome.what")),
        h("ol", { class: "plan" }, [1, 2, 3, 4].map((n) => h("li", {}, this._t(`welcome.item_${n}`)))),
        h("p", { class: "intro after" }, this._t("welcome.promise"))
      ),
    ];
  }

  _guidePeople() {
    const guide = this._guide;
    const users = this._data.users.map((user) => [user.id, user.name]);
    const people = this._draftPeople();
    const person = (section, cls, fallback) =>
      h(
        "div",
        { class: "card" },
        h(
          "div",
          { class: `person-head ${cls}` },
          h("div", { class: "avatar" }, (this._value(section, "name") || fallback).slice(0, 1).toUpperCase()),
          h("h3", {}, fallback)
        ),
        this._textField(section, "name"),
        this._choiceField(section, "user", users, { emptyLabel: "No user", label: this._t("people.user_label") })
      );
    return [
      this._head(this._t("steps.people"), this._t("people.lede")),
      h("div", { class: "people" }, person("owner", "one", "Person 1"), person("partner", "two", "Person 2")),
      h(
        "div",
        { class: "card", style: "margin-top: 16px" },
        h("h3", {}, this._t("people.style_title")),
        h("p", { class: "intro" }, this._t("people.style_intro")),
        h(
          "div",
          { class: "styles", role: "group", "aria-label": this._t("people.style_title") },
          GUIDE_STYLES.map((style) =>
            h(
              "button",
              { class: "style", "aria-pressed": String(guide.style === style), onclick: () => this._pickStyle(style) },
              h("span", { class: "style-name" }, this._t(`styles.${style}.title`)),
              h("span", { class: "style-about" }, this._t(`styles.${style}.about`))
            )
          )
        ),
        guide.styleError && h("div", { class: "notice error after" }, guide.styleError),
        guide.style && h("p", { class: "help after" }, this._t("people.style_filled", { style: this._t(`styles.${guide.style}.title`) }))
      ),
      h(
        "div",
        { class: "card" },
        this._textField("owner", "persona", { multiline: true, label: this._t("people.persona_of", { name: people.owner }) }),
        this._textField("partner", "persona", { multiline: true, label: this._t("people.persona_of", { name: people.partner }) })
      ),
    ];
  }

  _guideSatellites() {
    const head = this._head(this._t("steps.satellites"), this._t("satellites.lede"));
    const sats = this._satellites;
    if (!sats) return [head, h("div", { class: "card" }, h("div", { class: "empty" }, this._t("satellites.loading")))];
    if (sats.error) return [head, h("div", { class: "notice error" }, this._t("satellites.failed", { reason: sats.error }))];
    if (!sats.list.length) return [head, h("div", { class: "card" }, h("div", { class: "empty" }, this._t("satellites.empty")))];
    const chosen = this._chosen();
    return [head, sats.list.map((item) => this._satelliteCard(item, chosen.has(item.device_id)))];
  }

  _satelliteCard(satellite, on) {
    const id = `sat-${satellite.device_id}`;
    const box = h("input", {
      type: "checkbox",
      id,
      checked: on,
      "aria-label": this._t("satellites.choose", { satellite: satellite.name }),
      onchange: (ev) => {
        const chosen = this._chosen();
        if (ev.target.checked) chosen.add(satellite.device_id);
        else chosen.delete(satellite.device_id);
        this._render();
      },
    });
    box.dataset.key = id;
    return h(
      "div",
      { class: `card sat${on ? "" : " off"}` },
      h(
        "div",
        { class: "sat-head" },
        box,
        h("label", { for: id }, h("span", { class: "sat-name" }, satellite.name), h("span", { class: "sat-id" }, satellite.entity_id))
      ),
      on && h("div", { class: "tasks" }, this._helperTask(satellite), this._trackingTask(satellite))
    );
  }

  _task(level, text, ...rest) {
    return h("div", { class: `task ${level}` }, h("span", { class: "mark" }), h("div", { class: "body" }, h("div", {}, text), ...rest));
  }

  /** A button that changes Home Assistant, with the line that says what it changes. */
  _action(key, will, label, run, blocked) {
    const state = this._actions[key] || {};
    return h(
      "div",
      { class: "action" },
      h("div", { class: "will" }, will),
      h(
        "div",
        { class: "action-row" },
        h("button", { class: "btn", disabled: !!state.busy || !!blocked, onclick: run }, state.busy ? this._t("working") : label),
        typeof blocked === "string" && blocked && h("span", { class: "help" }, blocked)
      ),
      this._result(key)
    );
  }

  _result(key) {
    const state = this._actions[key];
    if (!state || state.busy) return null;
    return h("div", { class: `result ${state.ok ? "ok" : "error"}`, role: "status" }, state.text);
  }

  _helperTask(satellite) {
    const key = `helper:${satellite.device_id}`;
    const helper = satellite.helper;
    if (satellite.helper_exists) return this._task("ok", this._t("satellites.helper_ok", { helper }), this._result(key));
    return this._task(
      "warn",
      this._t("satellites.helper_missing", { helper }),
      this._action(key, this._t("satellites.helper_will", { helper }), this._t("satellites.helper_button"), () =>
        this._createHelper(satellite.device_id)
      )
    );
  }

  _trackingTask(satellite) {
    const key = `tracking:${satellite.device_id}`;
    if (satellite.tracked) return this._task("ok", this._t("satellites.tracked"), this._result(key));
    // Without a score sensor there is nothing to trigger on: the user's own recognition writes.
    if (!satellite.speaker_sensor || !satellite.score_sensor) {
      return this._task(
        "info",
        this._t("satellites.no_sensors", { helper: satellite.helper }),
        h("a", { class: "link", href: DOCS_TRACKING, target: "_blank", rel: "noreferrer" }, this._t("satellites.docs"))
      );
    }
    const names = this._trackingNames(satellite);
    const people = this._draftPeople();
    const input = (role) => {
      const id = `track-${role}-${satellite.device_id}`;
      const control = h("input", { type: "text", id, value: names[role] || "", oninput: (ev) => (names[role] = ev.target.value) });
      control.dataset.key = id;
      return h("div", { class: "field" }, h("label", { for: id }, this._t("satellites.name_for", { person: people[role] })), control);
    };
    return this._task(
      "warn",
      this._t("satellites.tracking_missing", { sensor: satellite.speaker_sensor }),
      h("div", { class: "help" }, this._t("satellites.names_intro", { sensor: satellite.speaker_sensor })),
      h("div", { class: "names" }, input("owner"), input("partner")),
      this._action(
        key,
        this._t("satellites.tracking_will", { helper: satellite.helper, sensor: satellite.speaker_sensor }),
        this._t("satellites.tracking_button"),
        () => this._createTracking(satellite),
        satellite.helper_exists ? "" : this._t("satellites.helper_first")
      )
    );
  }

  _guidePipelines() {
    const sats = this._satellites;
    let words;
    if (!sats) words = h("div", { class: "card" }, h("div", { class: "empty" }, this._t("satellites.loading")));
    else if (sats.error) words = h("div", { class: "notice error" }, this._t("satellites.failed", { reason: sats.error }));
    else {
      const chosen = this._chosen();
      const list = sats.list.filter((item) => chosen.has(item.device_id));
      words = list.length
        ? list.map((item) => this._wakeWordCard(item))
        : h("div", { class: "card" }, h("div", { class: "empty" }, this._t("pipelines.none_chosen")));
    }
    return [
      this._head(this._t("steps.pipelines"), this._t("pipelines.lede")),
      h("div", { class: "card" }, this._entityField("agents", "agent", "conversation", "Choose an agent")),
      h("h3", { class: "group" }, this._t("pipelines.wake_title")),
      words,
    ];
  }

  _wakeWordCard(satellite) {
    const words = satellite.wake_words;
    const mine = words.map((word, index) => ({ word, n: index + 1 })).filter(({ word }) => word.uses_router);
    let body;
    if (!words.some((word) => word.pipeline_id)) {
      body = this._task(
        "warn",
        this._t("pipelines.unknown"),
        h("a", { class: "link", href: VOICE_ASSISTANTS }, this._t("pipelines.open_voice"))
      );
    } else if (mine.length) {
      body = mine.map(({ word, n }) => this._localTask(word, n));
    } else {
      body = this._copyTask(satellite);
    }
    return h(
      "div",
      { class: "card sat" },
      h(
        "div",
        { class: "sat-head" },
        h("div", { class: "what" }, h("span", { class: "sat-name" }, satellite.name), h("span", { class: "sat-id" }, satellite.entity_id))
      ),
      h("div", { class: "tasks" }, body, words.map((word) => this._result(`wake:${word.select}`)))
    );
  }

  _localTask(word, n) {
    const key = `local:${word.pipeline_id}`;
    if (!word.prefer_local) return this._task("ok", this._t("pipelines.ready", { n }), this._result(key));
    const users = this._pipelineUsers(word.pipeline_id);
    const will = this._t(users.length > 1 ? "pipelines.local_many" : "pipelines.local_one", {
      satellites: this._names(users),
      pipeline: word.pipeline_name,
    });
    return this._task(
      "error",
      this._t("pipelines.local_on", { n, pipeline: word.pipeline_name }),
      this._action(key, will, this._t("pipelines.local_button"), () => this._localOff(word.pipeline_id))
    );
  }

  /** No wake word uses this router: offer a copy of the pipeline of the one the user picks. */
  _copyTask(satellite) {
    const guide = this._guide;
    const words = satellite.wake_words;
    const picked = words.find((word) => word.select === guide.wake[satellite.device_id] && word.pipeline_id);
    const word = picked || words.find((item) => item.pipeline_id);
    const n = words.indexOf(word) + 1;
    const id = `wake-${satellite.device_id}`;
    const control = h(
      "select",
      {
        class: "input",
        id,
        onchange: (ev) => {
          guide.wake[satellite.device_id] = ev.target.value;
          this._render();
        },
      },
      words.map((item, index) =>
        h(
          "option",
          { value: item.select, selected: item === word, disabled: !item.pipeline_id },
          item.pipeline_id
            ? this._t("pipelines.word", { n: index + 1, pipeline: item.pipeline_name })
            : this._t("pipelines.word_unknown", { n: index + 1 })
        )
      )
    );
    control.dataset.key = id;
    const plan = this._plan(word.select);
    let will;
    if (plan.loading) will = this._t("pipelines.planning");
    else if (plan.error) will = plan.error;
    else if (plan.reuse) will = this._t("pipelines.will_reuse", { n, satellite: satellite.name, name: this._router.title });
    else {
      will = this._t("pipelines.will_copy", {
        n,
        satellite: satellite.name,
        name: plan.payload.name,
        source: word.pipeline_name,
      });
    }
    return this._task(
      "warn",
      this._t("pipelines.not_yet"),
      h("div", { class: "field pick" }, h("label", { for: id }, this._t("pipelines.pick")), control),
      this._action(
        `wake:${word.select}`,
        will,
        this._t("pipelines.wake_button"),
        () => this._useWakeWord(satellite, word, n),
        !!(plan.loading || plan.error)
      )
    );
  }

  _guideStrangers() {
    const guide = this._guide;
    return [
      this._head(this._t("steps.strangers"), this._text("step.settings.sections.strangers.description")),
      guide.textsUsed &&
        guide.style &&
        h("p", { class: "help", style: "margin: 0 0 16px" }, this._t("strangers.style_filled", { style: this._t(`styles.${guide.style}.title`) })),
      this._strangerCards(),
    ];
  }

  _guideExtras() {
    const on = this._on("memory", "enabled", false);
    const docs = (href, key) => h("a", { class: "link", href, target: "_blank", rel: "noreferrer" }, this._t(key));
    return [
      this._head(this._t("steps.extras"), this._t("extras.lede")),
      h(
        "div",
        { class: "card" },
        h("h3", {}, this._text("step.settings.sections.memory.name", "Memory")),
        this._switchField("memory", "enabled", false, { redraw: true }),
        on && this._entityField("memory", "agent", "conversation", "Choose an agent")
      ),
      h(
        "div",
        { class: "card" },
        h("p", { class: "extra" }, this._t("extras.reminders"), " ", docs(DOCS_REMINDERS, "extras.reminders_docs")),
        h("p", { class: "extra" }, this._t("extras.calendars"), " ", docs(DOCS_CALENDARS, "extras.calendars_docs"))
      ),
    ];
  }

  _guideCheck() {
    return [
      this._head(this._t("steps.check"), this._t("check.lede")),
      h("div", { class: "card" }, this._statusList(true)),
      h("p", { class: "help", style: "margin: 0 0 8px" }, this._t("check.done_hint")),
    ];
  }

  _renderSaveBar() {
    const holder = this.shadowRoot.getElementById("floating");
    if (!holder) return;
    // In the guide, Next saves and the guide shows the errors itself.
    if (!this._guide && (this._dirty || this._saveError)) {
      holder.replaceChildren(
        h(
          "div",
          { class: "savebar", role: "status" },
          h("div", { class: `msg${this._saveError ? " error" : ""}` }, this._saveError || "Changes are not saved yet."),
          h(
            "button",
            {
              class: "btn",
              disabled: this._busy,
              onclick: () => {
                this._resetDraft();
                this._render();
              },
            },
            "Discard"
          ),
          h("button", { class: "btn primary", disabled: this._busy, onclick: () => this._save() }, this._busy ? "Saving…" : "Save changes")
        )
      );
    } else if (this._toastText) {
      holder.replaceChildren(h("div", { class: "toast", role: "status" }, this._toastText));
    } else {
      holder.replaceChildren();
    }
  }
}

if (!customElements.get(ELEMENT)) customElements.define(ELEMENT, VoicePersonaRouterPanel);
