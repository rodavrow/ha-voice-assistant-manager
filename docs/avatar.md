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
| Send speech to the avatar | When off, the assistant still speaks through the avatar entity, but nothing is sent to LiveTalking. |

Then open **Settings > Voice assistants**, open the assistant's pipeline, set **Text-to-speech** to the new avatar entity, and choose the voice again. The voice list is the engine's own list.

To change anything later, press **Change** next to the avatar in the **Avatar** card (or **Reconfigure** on the avatar under the assistant's entry). **Remove** there removes the avatar and its entity. A new engine, address or switch applies to the next thing the assistant says, without a restart.

### Switching engines

1. Change the avatar and choose the other engine.
2. Open the pipeline and choose a voice again. Voices differ per engine, and the pipeline keeps the voice name of the old engine until you change it.

The avatar entity stays the same, so the pipeline does not need a new engine.

Home Assistant keeps speech it made under the avatar's name, not the engine's. If both engines use the same voice name, or neither has one, a line said before the switch can still be played in the old engine's voice. Clear the kept speech (the `tts.clear_cache` action) after switching to avoid that.

Adding, removing or renaming an avatar reloads its assistant, which ends a conversation that is going on with it at that moment. Changing the engine, the address or the switch does not.

## What LiveTalking receives

For each complete clip, Voice Assistant Manager asks LiveTalking for its viewers (`GET /api/admin/sessions`). For each viewer, it stops what the avatar is saying (`POST /interrupt_talk`) and sends the clip (`POST /humanaudio`). With no viewers, nothing is sent.

- **The audio is sent as the engine made it.** MP3, WAV, FLAC and OGG are recognised from the audio itself and sent unconverted. Raw PCM without a header is wrapped as WAV, but only when the request said its sample rate. Without one it is not sent, and the log says so once.
- **At most 8 viewers** get a clip, and a clip over 10 MB is not sent.
- **Short time limits.** Two seconds to connect, ten seconds in all.

## What it never does

- **It never delays or changes the speech.** The copy is sent after the clip is complete, from a background task.
- **It never fails the speech.** If LiveTalking cannot be reached or answers oddly, the log shows one warning per outage, and the satellite speaks as usual. What was said is never logged.
- **It never invents a fallback.** If the avatar's engine no longer exists, the avatar offers no languages, so Home Assistant refuses the request ("does not support language"), and nothing is spoken until you choose an engine again. An error from the engine reaches Home Assistant unchanged.

## Limits

- **A reply Home Assistant kept does not animate.** Home Assistant keeps recent speech in memory (five minutes by default) and, unless turned off, on disk. When the same text is said again with the same voice, Home Assistant plays the kept audio and never asks the engine, so the avatar never sees it. Streamed replies (most longer answers) are never kept, so they always animate. Short repeated lines, such as "Okay.", may not.
- **The avatar starts a little after the satellite.** The copy is sent when the clip is complete, while the satellite may already be playing streamed speech.
- **Everyone watching LiveTalking hears everything this assistant says.** LiveTalking has no login. Only use it on a network you trust, and keep its page away from guests if what the assistant says is private.
- **Only the speech of pipelines that use the avatar entity.** Announcements and the [Chat show](chat-show.md) use the satellite's pipeline or the assistant's chat voice; choose the avatar entity there too if you want those animated.
