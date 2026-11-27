# Avatar

Part of the Voice Assistant Manager documentation. Back to the [README](../README.md).

An assistant can have an avatar: a face on a screen that moves its lips to what the assistant says. Voice Assistant Manager sends a copy of the assistant's speech to a [LiveTalking](https://github.com/lipku/LiveTalking) server, which animates the face. The satellite still speaks as before.

> [!NOTE]
> The avatar is optional and does nothing until you add one. Assistants without an avatar are not changed in any way.

## How it works

Adding an avatar creates a new text-to-speech entity, for example `tts.ada_avatar`. You choose that entity as the text-to-speech engine of the assistant's voice pipeline. It does not make speech itself:

1. It passes every request to the engine you chose for the avatar (for example ElevenLabs or a local engine), with the same language, voice and options.
2. It gives Home Assistant that engine's audio back unchanged, so the satellite hears exactly what it heard before. Streamed speech is passed on chunk by chunk, as it arrives.
3. When the clip is complete, it sends a copy to LiveTalking in the background.

The speech is made once. There is no second request to the engine, so it costs no extra characters with a cloud engine and no second job on a local one.

Only pipelines that use the avatar entity send anything. Other assistants, and other pipelines of the same assistant, keep their own engine.

## Adding an avatar

1. Open **Voice Manager** in the sidebar, choose the assistant at the top, and go to **Assistant**.
2. In the **Avatar** card, press **Add an avatar**.
3. Fill in the fields and press **Save**.

The same can be done under **Settings > Devices & services > Voice Assistant Manager**, with **Add avatar** on the assistant's entry.

| Setting | What it does |
| --- | --- |
| Name | The name of the new entity. "Ada avatar" becomes `tts.ada_avatar`. |
| Text-to-speech engine | The engine that makes the speech. Any text-to-speech entity, except another avatar. |
| LiveTalking address | The address of the LiveTalking server, for example `http://livetalking.local:8010`. Only `http://` and `https://` addresses are accepted. |
| LiveTalking avatar | The avatar a satellite in avatar mode asks LiveTalking for, for example `ada`. Letters, digits, `_` and `-`, at most 64. Leave empty for the server's default. |
| Avatar on | When on, satellites whose wake word uses this assistant show the avatar (see [Satellites in avatar mode](#satellites-in-avatar-mode)), and the LiveTalking page gets a copy of the speech. When off, everything goes back to normal; the assistant still speaks through the avatar entity. |

Then open **Settings > Voice assistants**, open the assistant's pipeline, set **Text-to-speech** to the new avatar entity, and choose the voice again. The voice list is the engine's own list.

To change anything later, press **Change** next to the avatar in the **Avatar** card (or **Reconfigure** on the avatar under the assistant's entry). **Remove** there removes the avatar and its entity. A new engine, address or switch applies to the next thing the assistant says, without a restart.

### Switching engines

1. Change the avatar and choose the other engine.
2. Open the pipeline and choose a voice again. Voices differ per engine, and the pipeline keeps the voice name of the old engine until you change it.

The avatar entity stays the same, so the pipeline does not need a new engine.

Home Assistant keeps speech it made under the avatar's name, not the engine's. If both engines use the same voice name, or neither has one, a line said before the switch can still be played in the old engine's voice. Clear the kept speech (the `tts.clear_cache` action) after switching to avoid that.

Adding, removing or renaming an avatar reloads its assistant, which ends a conversation that is going on with it at that moment. Changing the engine, the address or the switch does not.

## Satellites in avatar mode

Some satellite firmware can show the avatar itself: the satellite plays the assistant's LiveTalking stream, video and sound, instead of its own animations, and its spoken replies come from LiveTalking, so the lips and the sound match. While listening, the avatar's idle video is shown; while speaking, the avatar speaks.

Voice Assistant Manager switches this for you. For every satellite whose wake word uses an assistant with an avatar that is on, it turns on that wake word's avatar mode and gives it the avatar's LiveTalking address, the LiveTalking avatar and the connection setting below. For a wake word that uses one of its assistants without an avatar that is on, it turns avatar mode off, so switching an avatar off or removing it (or the assistant) puts the satellite back to normal. Avatar mode it did not switch on itself, for a wake word that uses another kind of assistant, is left alone. It checks this when Home Assistant starts, when an avatar or assistant changes, and every minute, which also catches wake words and pipelines you changed. It only changes what differs.

- **Avatar connection** under **Assistant > Advanced**:
  - **Connect when woken** (the default) connects to LiveTalking when the satellite wakes. The avatar shows about a second later; until then the normal look is shown.
  - **Always connected** keeps a LiveTalking session open per satellite, so the avatar is there at once. A [Chat show](chat-show.md) keeps its assistants connected for the chat whichever you choose (see [For the whole chat](#for-the-whole-chat)).
- **The satellite sends its own speech to LiveTalking**, for its own session only, so two satellites never speak each other's replies. The copy for other viewers, such as the LiveTalking page, skips the satellites' sessions.
- **Announcements and Chat show lines** go through the avatar when the satellite's own **Animate announcements** switch is on. When it is off, they play as before.
- **Speech the satellite plays as a media player** (for example `tts.speak` to its media player) stays as before, with the normal animation.
- **When anything fails** (LiveTalking cannot be reached, the stream stalls, no sound comes back), the satellite falls back to its normal speech and look for that turn. It never goes silent.

What a satellite needs: firmware with avatar mode (a display with the LiveTalking support), offering per wake word, on the wake word's assistant device, the entities with the object ids `avatar_mode_N` (switch), `avatar_url_N` and `avatar_id_N` (text), `avatar_connection_N` (select: "Always connected" or "Connect when woken"), `avatar_session_N` (sensor) and, on newer firmware, `avatar_state_N` (sensor: off, connecting, connected or failed), N being 1 for the first wake word. Voice Assistant Manager finds them by those ids, not by their names, so renaming them does not matter. Satellites without them are never touched.

### Between the lines of a chat

A satellite shows the avatar only while it speaks, so between two lines of a [Chat show](chat-show.md) it would go back to its own screen. On firmware that offers a switch named **Avatar hold**, Voice Assistant Manager turns that switch on for the chat and off when the chat ends. While it is on, the avatar that spoke last stays on screen, idle, until the next one speaks. The satellite turns the switch off by itself a minute after its last announcement. Satellites without the switch are not changed, and the switch is never touched outside a chat.

### For the whole chat

LiveTalking's picture stutters for a moment on every session it is running whenever a new session starts. With **Connect when woken**, each change of speaker in a chat would close one session and open another, so every line would stutter. So when a chat starts, Voice Assistant Manager connects the avatar of each assistant in the chat on the satellite showing it, and waits until each one is connected (up to 15 seconds) before the first line. They stay connected until the chat ends, however it ends, and then each goes back to its own **Avatar connection**.

- **Only the chat's satellite, only its assistants.** Other satellites, and wake words that use an assistant not in the chat, are not touched.
- **Your setting is not changed.** For the length of the chat, the satellite's **Avatar connection** select reads **Always connected**; the setting under **Assistant > Advanced** stays as you chose it, and the select is set back to it after the chat. If you change the setting during a chat, the new one is what the select gets.
- **It never holds up a chat.** An avatar that does not connect in time or is refused, a satellite that is away, or LiveTalking having no room (4 sessions, satellites' own included) is logged, and that assistant connects when woken, as outside a chat.
- **It cannot stay on.** A satellite away when the chat ends is set back when it returns (up to 30 seconds), and otherwise by the next minute's check. The hold also ends by itself five minutes after the chat's time limit, and a restart of Home Assistant ends it at once.
- Firmware without the `avatar_state_N` sensor is connected the same way, but the chat does not wait for it.

### Replies streamed to the avatar

A satellite in avatar mode normally waits until the whole reply has been made, downloads it and uploads it to LiveTalking before its avatar speaks, which takes longer the longer the answer is. With **Stream replies to the avatar** under Advanced (on by default), Voice Assistant Manager sends the speech to the satellite's LiveTalking session while the engine is still making it, so the avatar starts sooner.

It needs satellite firmware with the switch **Avatar fed by Home Assistant** on each wake-word slot, and a LiveTalking with `/stream_audio`. Voice Assistant Manager turns that switch on and off with the setting; satellites without it are not touched and work as before.

- **The satellite always keeps the normal audio as a backup.** It is passed on unchanged. If LiveTalking does not take the reply, or drops out part-way, the satellite speaks from the backup.
- **Only when it is clear who asked.** The reply goes to the session of the satellite that asked. When that cannot be told (two satellites asking the same assistant at the same moment, or two wake words of one satellite on the same assistant), nothing is streamed and the satellite speaks as before.
- **Not every reply is streamed.** A reply Home Assistant already has in its speech cache, an announcement or a chat line is not made again, so the satellite sends it to the avatar itself.
- **It never holds up the speech.** LiveTalking gets 0.3 seconds to accept a reply. The audio is decoded with Home Assistant's ffmpeg. An interrupt on the satellite stops the stream.

### Voice for chats

While an avatar is on, the **Avatar** card reminds you to choose its entity as the text-to-speech engine of **Voice for chats**, with a button to do so. Satellites in avatar mode speak chat lines through the avatar anyway; the setting makes the lines reach the LiveTalking page too. Once the avatar is off, it reminds you to choose the normal engine again.

## Talking to the avatar on the Voice Manager page

An administrator can talk to an assistant's avatar without a satellite. In the assistant's **Avatar** section on the Voice Manager page, **Talk** opens the avatar over the page. Tap the microphone, speak, and the avatar answers with voice and lips from the same stream. It is an ordinary turn with that assistant: the same main agent, tools, memory and reminders as on a satellite.

**It needs:**

- an avatar of the assistant that is on
- a voice assistant under **Settings > Voice assistants** with speech-to-text, this assistant as its conversation agent and this assistant's avatar voice as its text-to-speech
- room for one more session on LiveTalking
- a page that may use the microphone: Home Assistant opened over https, or the Home Assistant app, with the microphone allowed
- a device that can reach LiveTalking

Otherwise the page says which of these is missing.

**How a turn goes:** the microphone listens after a tap and stops by itself when you stop speaking. The page shows what was heard and the answer as text. While the avatar speaks the microphone is closed; when the assistant asked you something, it opens again by itself once the avatar is quiet.

**Who you are** is the Home Assistant user you are logged in as, not your voice. If that user is Person 1 or Person 2 of the assistant, you are that person; if not, the assistant treats you as its settings say for a request without a voice match.

**What it does not do:** there is no wake word, and the avatar cannot be interrupted by speaking. One person at a time can talk to an assistant's avatar. Closing or leaving the page ends the talk. While a talk is open, anything the same user types to that assistant in Home Assistant's Assist dialog is also answered by the avatar on the page.

## What LiveTalking receives

For each complete clip, Voice Assistant Manager asks LiveTalking for its viewers (`GET /api/admin/sessions`). It leaves out the sessions of satellites in avatar mode, which send their own speech, and, when the avatar has a **LiveTalking avatar** set, every session showing another avatar, so one assistant never speaks with another's face. For each viewer, it stops what the avatar is saying (`POST /interrupt_talk`) and sends the clip (`POST /humanaudio`). With no viewers, nothing is sent.

- **The audio is sent as the engine made it.** MP3, WAV, FLAC and OGG are recognised from the audio itself and sent unconverted. Raw PCM without a header is wrapped as WAV, but only when the request said its sample rate. Without one it is not sent, and the log says so once.
- **At most 4 viewers** get a clip, LiveTalking's session limit on the household server, and a clip over 10 MB is not sent.
- **Short time limits.** Two seconds to connect, ten seconds in all.

## Emotions

Each persona can start its replies with one emotion tag: `[neutral]`, `[happy]`, `[sad]`,
`[angry]` or `[sarcastic]`, for example by adding to its prompt: "Start every reply with
exactly one of [neutral], [happy], [sad], [angry] or [sarcastic]." The same holds for chat
show lines and announcements.

**Emotion tags** under Advanced sets the tags an assistant passes on, separated by commas,
for example `neutral, happy, sad, excited`. Each is one word of letters, at most 18; any
other entry is left out, with one warning in the log. When empty, the five above apply.
LiveTalking needs talking frames for a tag, or it shows neutral, so add a tag there too.

- **The tag is never spoken or shown.** It is taken off before text-to-speech, the
  satellite's display and the log, also while a reply streams in (only its first 20
  characters are held back to tell). Assistants without an avatar have it taken off too.
  The conversation history keeps it, so the agent sees how it answered before.
- **A missing or unknown tag is neutral.** An unknown word, such as `[excited]`, is still
  taken off. Only a word of letters counts as a tag, so a reply that starts with `[1]` or
  `[2b]` keeps it. The reply always goes ahead.
- **LiveTalking is told the emotion** just before the reply's audio:
  `POST /set_emotion` with `{"avatar_id": …, "emotion": …}`, once for each avatar of the
  assistant that is on. It goes by avatar, not session, so a satellite in avatar mode gets
  it too. An avatar without a **LiveTalking avatar** set is not told. The call gives up
  after 300 ms and never holds up or fails the reply. A LiveTalking without
  `/set_emotion` answers 404, which is logged only at debug level.
- **On the chat stage**, each line's emotion goes with its audio instead (an `emotion` field
  on `/humanaudio`), so it reaches exactly the stage's session and no other session showing
  the same avatar. A LiveTalking that does not know emotions ignores the field.

## Audio tags for the voice

Some voices take directions in square brackets, such as `[laughs]`, `[whispers]`,
`[whispering, fearful]` or `[out of breath after running up the stairs]`. **Pass audio tags
to the voice** under Advanced says whether an assistant's voice is one of them. It is off
unless you turn it on.

- **On:** the voice is given the tags, and they are never shown: not on a satellite's
  display, in captions, or in the reply Home Assistant shows.
- **Off:** the tags are taken out of what is spoken too, so a voice that does not understand
  them never reads one aloud.
- **What counts as a tag:** square brackets around text that starts with a letter and has no
  digit, at most 80 characters. `[1]` and `[2b]` are never tags and stay in the reply.
- **The emotion tag comes first and is separate.** One word at the very start that is on the
  assistant's **Emotion tags** list sets the avatar's face and is never passed to the voice:
  in `[happy] [laughs] Good one.`, `[happy]` is for the face and `[laughs]` for the voice. A
  reply that starts with a tag that is not on that list, such as `[whispers]`, gives it to
  the voice, and the face is neutral.

### The face when a reply has no emotion tag

A model sometimes skips the emotion tag and opens with an audio tag, as in `[mischievous]
Well, my dear friend…`. The avatar's face then comes from that opening audio tag, through
**Face for audio tags** under Advanced. The reply itself is not changed: the audio tag still
goes to the voice (or is taken out, when the voice takes no tags).

- **One line per face:** the face, a colon, then the audio tags that give it, separated by
  commas, for example `sarcastic: mischievous, smug, drawn out`.
- **The whole tag is compared,** in any case. `[smug, tired]` is not `smug`. The first line
  that has the tag wins.
- **Only the opening counts:** the tags a reply starts with, before its first words. A tag
  with a digit, such as `[pause 2]`, is skipped and the next one is used. A tag later in the
  reply never changes the face.
- **A face must be on the Emotion tags list,** or its line is ignored, with a warning on the
  page for a table of your own. A tag found in no line gives neutral.
- **When the field is empty** this table is used:

```
sarcastic: mischievous, playful, smug, snappy, drawn out, disbelief
happy: excited, amazed, proud, triumphant, hopeful, content, intrigued, curious
laughing: laughs
sad: sighs, let down, tired, sad, softly
angry: shouts, bitter, groans, angry
flirty: whispers, hushed
```

With debug logging for `custom_components.voice_assistant_manager.emotion`, each face told to
LiveTalking says how it was chosen: `(face tag)`, `(from audio tag [mischievous])` or
`(no match for [thoughtful])`.

Where the voice gets its tags:

| What is spoken | Tags reach the voice |
| --- | --- |
| A longer reply (streamed) | Yes |
| A short reply | Yes, when the assistant's text-to-speech is its avatar voice |
| A chat show line | Yes, when the assistant has a **Voice for chats**, and the line was not cut to the word limit |
| An announcement or reminder | No |

Home Assistant's own Assist window shows the tags while such a reply streams in. Home
Assistant keeps the speech of a short reply by its shown text, so the same sentence with
other tags is spoken as it was the first time.

## What it never does

- **It never delays or changes the speech.** The copy is sent after the clip is complete, from a background task.
- **It never fails the speech.** If LiveTalking cannot be reached or answers oddly, the log shows one warning per outage, and the satellite speaks as usual. What was said is never logged.
- **It never invents a fallback.** If the avatar's engine no longer exists, the avatar offers no languages, so Home Assistant refuses the request ("does not support language"), and nothing is spoken until you choose an engine again. An error from the engine reaches Home Assistant unchanged.

## Limits

- **A reply Home Assistant kept does not animate.** Home Assistant keeps recent speech in memory (five minutes by default) and, unless turned off, on disk. When the same text is said again with the same voice, Home Assistant plays the kept audio and never asks the engine, so the avatar never sees it. Streamed replies (most longer answers) are never kept, so they always animate. Short repeated lines, such as "Okay.", may not.
- **The avatar starts a little after the satellite.** The copy is sent when the clip is complete, while the satellite may already be playing streamed speech.
- **Everyone watching LiveTalking hears everything this assistant says.** LiveTalking has no login. Only use it on a network you trust, and keep its page away from guests if what the assistant says is private.
- **Only the speech of pipelines that use the avatar entity.** Announcements and the [Chat show](chat-show.md) use the satellite's pipeline or the assistant's chat voice; choose the avatar entity there too if you want those animated.
