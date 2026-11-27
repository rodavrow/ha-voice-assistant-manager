// SPDX-FileCopyrightText: 2026 rodavrow
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Talking to an assistant's avatar on the Voice Manager page (talk.py). The element covers
// the page with the avatar, opened as one LiveTalking stream through Home Assistant, and
// sends the microphone into Home Assistant's own voice pipeline for that assistant. The
// reply comes back on the avatar's stream, voice and lips together; the pipeline's own
// audio is never played.

const TALK_WS = "voice_assistant_manager/panel/talk";
// LiveTalking takes the whole offer at once, so the page waits for its candidates.
const GATHER_MS = 3000;
// The avatar has finished a reply when its sound has been this quiet for this long. A reply
// that never makes a sound is given up on after FIRST_SOUND_MS.
const QUIET_LEVEL = 0.01;
const QUIET_MS = 1200;
const FIRST_SOUND_MS = 15000;
const LEVEL_POLL_MS = 100;

// Hands the microphone's samples to the page, a block at a time.
const RECORDER = `
class VamTalkRecorder extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("vam-talk-recorder", VamTalkRecorder);
`;

const STYLE = `
:host { position: fixed; inset: 0; z-index: 1000; display: block; background: #000; color: #fff;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; background: #000; }
.top { position: absolute; z-index: 2; left: 12px; right: 12px; top: calc(12px + env(safe-area-inset-top, 0px));
  display: flex; align-items: center; gap: 12px; }
.name { flex: 1; font-size: 20px; text-shadow: 0 1px 4px #000; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
button { font: inherit; border: 0; cursor: pointer; }
.close { padding: 10px 18px; border-radius: 999px; background: rgba(255, 255, 255, 0.9); color: #000; font-size: 16px; }
.bottom { position: absolute; z-index: 2; left: 0; right: 0; bottom: calc(18px + env(safe-area-inset-bottom, 0px));
  display: grid; justify-items: center; gap: 12px; padding: 0 16px; }
.said { max-width: 60ch; padding: 8px 14px; border-radius: 12px; background: rgba(0, 0, 0, 0.6); font-size: 16px; line-height: 1.35; text-align: center; }
.said:empty { display: none; }
.said .you { opacity: 0.75; }
.status { font-size: 15px; text-shadow: 0 1px 4px #000; text-align: center; }
.status.bad { padding: 8px 14px; border-radius: 12px; background: rgba(160, 20, 20, 0.85); text-shadow: none; }
.mic { width: 76px; height: 76px; border-radius: 50%; background: #fff; color: #000; display: grid; place-items: center; }
.mic svg { width: 34px; height: 34px; fill: currentColor; }
.mic[data-state="listening"] { background: #d93025; color: #fff; animation: pulse 1.2s ease-in-out infinite; }
.mic:disabled { opacity: 0.45; cursor: default; }
@keyframes pulse { 50% { transform: scale(1.08); } }
@media (prefers-reduced-motion: reduce) { .mic[data-state="listening"] { animation: none; } }
`;

const MIC_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg>`;

const TEXTS = {
  close: "Close",
  speak: "Speak to the assistant",
  connecting: "Connecting to the avatar…",
  ready: "Tap the microphone and speak.",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "",
  not_heard: "I did not hear anything. Tap the microphone and try again.",
  no_microphone: "The microphone cannot be used here. Open Home Assistant over https or in its app, and allow the microphone.",
  lost: "The avatar's stream was lost. Close this and try again.",
  failed: "That did not work: {reason}",
  tap_for_sound: "Tap for sound",
};

function reason(err) {
  if (!err) return "";
  if (typeof err === "string") return err;
  return err.message || err.code || String(err);
}

class VamTalk extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._entryId = "";
    this._pipeline = "";
    this._pc = null;
    this._closed = false;
    // idle: the microphone may be tapped. listening, thinking, speaking: it may not.
    this._state = "connecting";
    this._conversationId = null;
    this._run = null; // the pipeline run under way: { unsubscribe, handler, ended }
    this._mic = null; // { stream, context, node } once the microphone was opened
    this._levels = null; // { context, analyser, data } listening to the avatar's own sound
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <video autoplay playsinline></video>
      <div class="top"><div class="name"></div><button class="close" type="button"></button></div>
      <div class="bottom">
        <div class="said" aria-live="polite"></div>
        <div class="status" role="status"></div>
        <button class="mic" type="button" disabled>${MIC_ICON}</button>
      </div>`;
    const root = this.shadowRoot;
    this._video = root.querySelector("video");
    this._name = root.querySelector(".name");
    this._said = root.querySelector(".said");
    this._status = root.querySelector(".status");
    this._micButton = root.querySelector(".mic");
    this._closeButton = root.querySelector(".close");
    this._closeButton.textContent = TEXTS.close;
    this._closeButton.addEventListener("click", () => this.close());
    this._micButton.setAttribute("aria-label", TEXTS.speak);
    this._micButton.addEventListener("click", () => this._listen());
  }

  set hass(hass) {
    this._hass = hass;
  }

  disconnectedCallback() {
    this._shutDown();
  }

  /** Open the avatar of this assistant and wait for its stream. Shows why not when it cannot. */
  async open({ hass, entryId, title }) {
    this._hass = hass;
    this._entryId = entryId;
    this._name.textContent = title;
    this._set("connecting");
    const pc = new RTCPeerConnection();
    this._pc = pc;
    const stream = new MediaStream();
    this._video.srcObject = stream;
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.ontrack = (ev) => {
      stream.addTrack(ev.track);
      this._play();
    };
    pc.onconnectionstatechange = () => {
      if (this._closed) return;
      if (pc.connectionState === "connected" && this._state === "connecting") this._set("idle", TEXTS.ready);
      else if (pc.connectionState === "failed" || pc.connectionState === "closed") this._fail(TEXTS.lost);
    };
    try {
      await pc.setLocalDescription(await pc.createOffer());
      await this._gathered(pc);
      const result = await hass.callWS({ type: `${TALK_WS}/open`, entry_id: entryId, sdp: pc.localDescription.sdp });
      if (this._closed) return;
      if (result.ok === false) {
        this._fail(Object.values(result.errors || {}).join(" "));
        return;
      }
      this._pipeline = result.pipeline;
      await pc.setRemoteDescription({ type: "answer", sdp: result.sdp });
    } catch (err) {
      this._fail(TEXTS.failed.replace("{reason}", reason(err)));
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

  async _play() {
    try {
      await this._video.play();
    } catch (err) {
      // A browser that will not play sound unasked: the first tap on the microphone unmutes.
      console.warn("Talk: playing muted", err);
      this._video.muted = true;
      this._video.play().catch(() => {});
    }
  }

  _set(state, text, bad) {
    this._state = state;
    this._micButton.dataset.state = state;
    this._micButton.disabled = state !== "idle";
    this._status.textContent = text === undefined ? TEXTS[state] || "" : text;
    this._status.classList.toggle("bad", !!bad);
  }

  /** Something that ends the talk: say it, and leave only Close to press. */
  _fail(text) {
    if (this._closed) return;
    this._endRun();
    this._set("failed", text, true);
  }

  _show(you, reply) {
    this._said.replaceChildren();
    if (you) {
      const line = document.createElement("div");
      line.className = "you";
      line.textContent = you;
      this._said.append(line);
    }
    if (reply) {
      const line = document.createElement("div");
      line.textContent = reply;
      this._said.append(line);
    }
  }

  // The microphone.

  /** Open the microphone once, in the tap that asks for it. Throws when it cannot be had. */
  async _openMic() {
    if (this._mic) {
      if (this._mic.context.state === "suspended") await this._mic.context.resume();
      return this._mic;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.AudioWorkletNode) {
      throw new Error("no microphone");
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const context = new AudioContext();
    const url = URL.createObjectURL(new Blob([RECORDER], { type: "application/javascript" }));
    try {
      await context.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    const node = new AudioWorkletNode(context, "vam-talk-recorder");
    context.createMediaStreamSource(stream).connect(node);
    this._mic = { stream, context, node };
    return this._mic;
  }

  /** One turn: listen until the person stops, then wait for the avatar to have answered. */
  async _listen() {
    if (this._state !== "idle" || this._closed) return;
    // The tap also lets a browser play the avatar's sound, if it would not before.
    if (this._video.muted) {
      this._video.muted = false;
      this._video.play().catch(() => {});
    }
    this._set("listening");
    this._show("", "");
    let mic;
    try {
      mic = await this._openMic();
    } catch (err) {
      console.warn("Talk: no microphone", err);
      this._set("idle", TEXTS.no_microphone, true);
      return;
    }
    if (this._closed) return;
    const run = { unsubscribe: null, handler: null, ended: false, sending: true, waiting: [], again: false, heard: "" };
    this._run = run;
    mic.node.port.onmessage = (ev) => this._audio(run, ev.data);
    try {
      run.unsubscribe = await this._hass.connection.subscribeMessage((event) => this._event(run, event), {
        type: "assist_pipeline/run",
        start_stage: "stt",
        end_stage: "tts",
        pipeline: this._pipeline,
        conversation_id: this._conversationId,
        input: { sample_rate: mic.context.sampleRate },
      });
    } catch (err) {
      this._endRun();
      this._set("idle", TEXTS.failed.replace("{reason}", reason(err)), true);
    }
  }

  /** A block of samples from the microphone: sent as 16-bit PCM once the run can take it. */
  _audio(run, samples) {
    if (run !== this._run || !run.sending) return;
    const pcm = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      const sample = Math.max(-1, Math.min(1, samples[i]));
      pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    if (run.handler === null) run.waiting.push(pcm);
    else this._send(run, pcm);
  }

  _send(run, pcm) {
    const bytes = new Uint8Array(1 + pcm.byteLength);
    bytes[0] = run.handler;
    bytes.set(new Uint8Array(pcm.buffer), 1);
    this._hass.connection.socket.send(bytes);
  }

  /** No more audio for this run: an empty block tells Home Assistant so. */
  _stopSending(run) {
    if (!run.sending) return;
    run.sending = false;
    run.waiting = [];
    if (run.handler !== null) this._hass.connection.socket.send(new Uint8Array([run.handler]));
  }

  _event(run, event) {
    if (run !== this._run) return;
    const data = event.data || {};
    if (event.type === "run-start") {
      run.handler = data.runner_data.stt_binary_handler_id;
      for (const pcm of run.waiting) this._send(run, pcm);
      run.waiting = [];
    } else if (event.type === "stt-vad-end") {
      this._stopSending(run);
    } else if (event.type === "stt-end") {
      this._stopSending(run);
      run.heard = (data.stt_output && data.stt_output.text) || "";
      this._show(run.heard, "");
      this._set("thinking");
    } else if (event.type === "intent-end") {
      const output = data.intent_output || {};
      this._conversationId = output.conversation_id || null;
      run.again = !!output.continue_conversation;
      const speech = output.response && output.response.speech && output.response.speech.plain;
      this._show(run.heard, (speech && speech.speech) || "");
    } else if (event.type === "error") {
      this._endRun();
      const quiet = data.code === "stt-no-text-recognized";
      this._set("idle", quiet ? TEXTS.not_heard : TEXTS.failed.replace("{reason}", data.message || data.code || "?"), !quiet);
    } else if (event.type === "run-end") {
      const again = run.again;
      this._endRun();
      this._afterReply(again);
    }
  }

  _endRun() {
    const run = this._run;
    if (!run) return;
    this._run = null;
    this._stopSending(run);
    if (this._mic) this._mic.node.port.onmessage = null;
    if (run.unsubscribe) run.unsubscribe();
  }

  /** The avatar is answering: the microphone waits until it is quiet again, then opens by
   * itself when the assistant asked something. */
  async _afterReply(again) {
    this._set("speaking");
    await this._untilQuiet();
    if (this._closed || this._state !== "speaking") return;
    this._set("idle", TEXTS.ready);
    if (again) this._listen();
  }

  _untilQuiet() {
    const levels = this._openLevels();
    if (!levels) return new Promise((resolve) => setTimeout(resolve, QUIET_MS));
    return new Promise((resolve) => {
      const started = performance.now();
      let lastSound = 0;
      const check = () => {
        if (this._closed) return resolve();
        levels.analyser.getFloatTimeDomainData(levels.data);
        let peak = 0;
        for (const sample of levels.data) peak = Math.max(peak, Math.abs(sample));
        const now = performance.now();
        if (peak > QUIET_LEVEL) lastSound = now;
        const over = lastSound ? now - lastSound > QUIET_MS : now - started > FIRST_SOUND_MS;
        if (over) resolve();
        else setTimeout(check, LEVEL_POLL_MS);
      };
      check();
    });
  }

  /** A meter on the avatar's own sound, to tell when it has finished speaking. */
  _openLevels() {
    if (this._levels) return this._levels;
    const context = this._mic && this._mic.context;
    const stream = this._video.srcObject;
    if (!context || !stream || !stream.getAudioTracks().length) return null;
    try {
      const analyser = context.createAnalyser();
      analyser.fftSize = 2048;
      context.createMediaStreamSource(stream).connect(analyser);
      this._levels = { analyser, data: new Float32Array(analyser.fftSize) };
    } catch (err) {
      console.warn("Talk: cannot hear the avatar's level", err);
      return null;
    }
    return this._levels;
  }

  // Ending.

  /** End the talk and take the element off the page. */
  close() {
    this._shutDown();
    this.remove();
    this.dispatchEvent(new CustomEvent("vam-talk-closed"));
  }

  _shutDown() {
    if (this._closed) return;
    this._closed = true;
    this._endRun();
    if (this._mic) {
      for (const track of this._mic.stream.getTracks()) track.stop();
      this._mic.context.close().catch(() => {});
      this._mic = null;
    }
    if (this._pc) {
      this._pc.onconnectionstatechange = null;
      this._pc.close();
      this._pc = null;
    }
    this._video.srcObject = null;
    if (this._hass && this._entryId) {
      this._hass.callWS({ type: `${TALK_WS}/close`, entry_id: this._entryId }).catch(() => {});
    }
  }
}

if (!customElements.get("vam-talk")) customElements.define("vam-talk", VamTalk);
