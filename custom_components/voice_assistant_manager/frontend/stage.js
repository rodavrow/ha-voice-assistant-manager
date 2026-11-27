// SPDX-FileCopyrightText: 2026 rodavrow
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The chat stage card: cast to a Google Cast device during a chat show (stage.py). It opens
// one LiveTalking stream per assistant of the chat, through Home Assistant (the cast page is
// on https, LiveTalking on http), shows the speaking one across the screen and the other in
// an inset, and plays their voices from the same streams as their lips.
//
// The Voice Manager page uses the same card for a chat on "This device": with a `viewer` in
// its config the card is told only of the chat that page started.

const STAGE_WS = "voice_assistant_manager/stage";
// A stream that is "disconnected" this long is reported lost; it often comes back sooner.
const LOST_AFTER_MS = 5000;
// LiveTalking takes the whole offer at once, so the card waits for its candidates.
const GATHER_MS = 3000;

const STYLE = `
:host { display: block; height: 100%; min-height: 100vh; background: #000; overflow: hidden; }
.stage { position: relative; width: 100%; height: 100%; min-height: 100vh; background: #000; color: #fff;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.actor { position: absolute; overflow: hidden; background: #000;
  transition: top 0.35s ease, left 0.35s ease, width 0.35s ease, height 0.35s ease, border-radius 0.35s ease, opacity 0.35s ease; }
.actor video { width: 100%; height: 100%; display: block; object-fit: contain; background: #000; }
.actor.main { top: 0; left: 0; width: 100%; height: 100%; z-index: 1; border-radius: 0; }
.actor.inset { top: 66%; left: 72%; width: 25%; height: 30%; z-index: 2; border-radius: 14px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.6); outline: 2px solid rgba(255, 255, 255, 0.25); }
.actor.inset video { object-fit: cover; }
.actor.waiting { opacity: 0.35; }
.name { position: absolute; left: 12px; top: 10px; padding: 4px 10px; border-radius: 999px; font-size: 18px;
  background: rgba(0, 0, 0, 0.55); letter-spacing: 0.01em; }
.main .name { left: 3%; top: 4%; font-size: 26px; }
.caption { position: absolute; left: 8%; right: 30%; bottom: 6%; z-index: 3; text-align: left; font-size: 30px; line-height: 1.3;
  padding: 10px 18px; border-radius: 12px; background: rgba(0, 0, 0, 0.65); }
.caption:empty { display: none; }
.sound { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); z-index: 4; font: inherit; font-size: 20px;
  padding: 14px 24px; border: 0; border-radius: 999px; background: #fff; color: #000; }
.sound[hidden] { display: none; }
.idle { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-size: 22px; color: rgba(255, 255, 255, 0.45); }
`;

class VamChatStageCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._token = "";
    this._actors = new Map(); // key -> { name, pc, wrap, video, lostTimer, up }
    this._speaking = "";
    this._unsubscribe = null;
    this._pending = null;
    this._render();
  }

  setConfig(config) {
    this._config = config || {};
  }

  getCardSize() {
    return 12;
  }

  set hass(hass) {
    this._hass = hass;
    if (this.isConnected) this._subscribe();
  }

  connectedCallback() {
    this._subscribe();
  }

  disconnectedCallback() {
    if (this._unsubscribe) this._unsubscribe();
    this._unsubscribe = null;
    this._closeAll();
  }

  _render() {
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><div class="stage"><div class="idle"></div><div class="caption"></div><button class="sound" hidden></button></div>`;
    this._stage = this.shadowRoot.querySelector(".stage");
    this._idle = this.shadowRoot.querySelector(".idle");
    this._caption = this.shadowRoot.querySelector(".caption");
    this._sound = this.shadowRoot.querySelector(".sound");
    this._sound.addEventListener("click", () => this._unmute());
  }

  _subscribe() {
    if (!this._hass || this._unsubscribe) return Promise.resolve();
    if (!this._pending) {
      const viewer = this._config.viewer;
      this._pending = this._hass.connection
        .subscribeMessage((msg) => this._on(msg), { type: `${STAGE_WS}/subscribe`, ...(viewer ? { viewer } : {}) })
        .then((unsubscribe) => {
          // Taken off the page while it subscribed: nobody is left to hear it.
          if (this.isConnected) this._unsubscribe = unsubscribe;
          else unsubscribe();
        })
        .catch((err) => console.warn("Chat stage: could not subscribe", err))
        .finally(() => {
          this._pending = null;
        });
    }
    return this._pending;
  }

  /** Whether the card is watching the stage, once it has tried: a chat on this page waits for it. */
  watching() {
    return this._subscribe().then(() => !!this._unsubscribe);
  }

  /** Tells the page around the card whether a chat is on it, so it can show and hide the card. */
  _active(on) {
    this.dispatchEvent(new CustomEvent("vam-stage", { detail: { active: on } }));
  }

  _on(msg) {
    if (msg.event === "start") {
      if (msg.token !== this._token) {
        this._closeAll();
        this._token = msg.token;
        this._active(true);
        this._captions = !!msg.captions;
        for (const actor of msg.actors) this._open(actor);
      }
      this._show(msg.speaking || (msg.actors[0] && msg.actors[0].key), msg.text);
    } else if (msg.event === "line" && msg.token === this._token) {
      this._show(msg.speaking, msg.text);
    } else if (msg.event === "end" || msg.event === "idle") {
      if (!msg.token || msg.token === this._token) this._closeAll();
    }
  }

  _show(speaking, text) {
    this._speaking = speaking || "";
    for (const [key, actor] of this._actors) {
      const main = key === this._speaking;
      actor.wrap.classList.toggle("main", main);
      actor.wrap.classList.toggle("inset", !main);
    }
    this._caption.textContent = this._captions && text ? text : "";
    this._idle.textContent = "";
  }

  async _open({ key, name }) {
    const wrap = document.createElement("div");
    wrap.className = "actor inset waiting";
    const video = document.createElement("video");
    video.autoplay = true;
    video.playsInline = true;
    const label = document.createElement("div");
    label.className = "name";
    label.textContent = name;
    wrap.append(video, label);
    this._stage.insertBefore(wrap, this._caption);

    const token = this._token;
    const pc = new RTCPeerConnection({ iceServers: this._config.ice_servers || [] });
    const actor = { name, pc, wrap, video, lostTimer: null, up: false };
    this._actors.set(key, actor);
    const stream = new MediaStream();
    video.srcObject = stream;
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.ontrack = (ev) => {
      stream.addTrack(ev.track);
      this._play(video);
    };
    pc.onconnectionstatechange = () => this._state(token, key, actor);
    try {
      await pc.setLocalDescription(await pc.createOffer());
      await this._gathered(pc);
      const answer = await this._hass.callWS({
        type: `${STAGE_WS}/offer`,
        token,
        key,
        sdp: pc.localDescription.sdp,
      });
      if (token !== this._token) return;
      await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp });
    } catch (err) {
      console.warn(`Chat stage: ${name} could not connect`, err);
      this._report(token, key, false);
    }
  }

  _gathered(pc) {
    if (pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        if (pc.iceGatheringState !== "complete") return;
        pc.removeEventListener("icegatheringstatechange", done);
        resolve();
      };
      pc.addEventListener("icegatheringstatechange", done);
      setTimeout(resolve, GATHER_MS);
    });
  }

  async _play(video) {
    try {
      await video.play();
    } catch (err) {
      // A device that will not play sound unasked still shows the picture.
      console.warn("Chat stage: playing muted", err);
      video.muted = true;
      video.play().catch(() => {});
      // On a page a person can tap for the sound; nobody can on a cast device.
      if (this._config.viewer) {
        this._sound.textContent = this._config.sound_text || "Tap for sound";
        this._sound.hidden = false;
      }
    }
  }

  _unmute() {
    this._sound.hidden = true;
    for (const actor of this._actors.values()) {
      actor.video.muted = false;
      actor.video.play().catch((err) => console.warn("Chat stage: still no sound", err));
    }
  }

  _state(token, key, actor) {
    const state = actor.pc.connectionState;
    clearTimeout(actor.lostTimer);
    actor.lostTimer = null;
    if (state === "connected") {
      actor.wrap.classList.remove("waiting");
      if (!actor.up) {
        actor.up = true;
        this._report(token, key, true);
      }
    } else if (state === "failed" || state === "closed") {
      this._lost(token, key, actor);
    } else if (state === "disconnected") {
      actor.lostTimer = setTimeout(() => this._lost(token, key, actor), LOST_AFTER_MS);
    }
  }

  _lost(token, key, actor) {
    actor.wrap.classList.add("waiting");
    if (token !== this._token) return;
    actor.up = false;
    this._report(token, key, false);
  }

  _report(token, key, up) {
    if (!this._hass || token !== this._token) return;
    this._hass
      .callWS({ type: `${STAGE_WS}/connected`, token, key, up })
      .catch((err) => console.debug("Chat stage: report not taken", err));
  }

  _closeAll() {
    if (this._token) this._active(false);
    this._token = "";
    if (this._sound) this._sound.hidden = true;
    for (const actor of this._actors.values()) {
      clearTimeout(actor.lostTimer);
      actor.pc.onconnectionstatechange = null;
      actor.pc.close();
      actor.video.srcObject = null;
      actor.wrap.remove();
    }
    this._actors.clear();
    this._speaking = "";
    if (this._caption) this._caption.textContent = "";
    if (this._idle) this._idle.textContent = this._config.idle_text || "";
  }
}

if (!customElements.get("vam-chat-stage-card")) {
  customElements.define("vam-chat-stage-card", VamChatStageCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "vam-chat-stage-card",
    name: "Chat stage",
    description: "Shows the avatars of a Voice Assistant Manager chat show; cast to a Google Cast device.",
  });
}
