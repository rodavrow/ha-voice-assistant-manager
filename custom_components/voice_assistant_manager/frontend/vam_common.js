// SPDX-FileCopyrightText: 2026 rodavrow
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the settings page (panel.js) and the My assistant page (mine.js) share. Each page
// loads it with its own ?v= version, so a browser never mixes an old copy with a new page.
// The limits mirror what the Python side enforces; tests/test_frontend_common.py keeps
// them in step.

/** Create an element. Children are nodes or strings; strings become text nodes. */
export function h(tag, props, ...children) {
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

// How a private fact is written in the memory editor (memory_store.PRIVATE_PREFIX).
export const PRIVATE_PREFIX = "(private)";

// The chat limits the card edits (chat_text.LIMIT_RANGES); "listen" is a checkbox, only for a
// speaker that can listen.
export const CHAT_LIMITS = [
  { key: "max_lines", min: 2, max: 50, step: 1 },
  { key: "words", min: 5, max: 80, step: 1 },
  { key: "pause", min: 0, max: 10, step: 0.5 },
  { key: "minutes", min: 1, max: 30, step: 1 },
];
// The chat show is asked for again this often while a chat runs.
export const CHAT_POLL_MS = 2000;
// Reasons a chat ends that are no failure (chat_show.py).
export const CHAT_ENDED_FINE = ["chat_done", "chat_time_up", "chat_stopped"];

// The stage card (stage.js) as a page shows it for a chat on "This device": over everything.
export const STAGE_CARD = "vam-chat-stage-card";
export const STAGE_BOX_STYLE = "position:fixed;inset:0;z-index:1000;background:#000;";
export const STAGE_STOP_STYLE =
  "position:absolute;z-index:5;top:calc(12px + env(safe-area-inset-top, 0px));right:12px;padding:10px 18px;" +
  "border:0;border-radius:999px;background:rgba(255,255,255,0.9);color:#000;font:inherit;font-size:16px;";

// Favorites (favorites.MAX_NAME_LENGTH, favorites.MAX_URL_LENGTH): the longest name and address.
export const MAX_FAVORITE_NAME = 80;
export const MAX_FAVORITE_URL = 2000;

// My assistant (mine_api.MAX_PERSONA_LENGTH, mine_api.MAX_TITLE_LENGTH).
export const MAX_PERSONA = 2000;
export const MAX_TITLE = 200;
