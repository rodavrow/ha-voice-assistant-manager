# Favorites

Part of the Voice Assistant Manager documentation. Back to the [README](../README.md).

Favorites are the radio stations, podcasts and playlists you ask for by name. Say "Play Radio 2" and your Radio 2 starts at once on the speaker you are talking to, without the main agent guessing and without a search that might pick the wrong station.

Favorites are optional and off until you [switch them on](#switch-favorites-on). Everything else works as before without them.

## What you can say

| You say | What happens |
| --- | --- |
| "Play Radio 2." | Radio 2 starts on the speaker of the satellite you spoke to. Your own favorites are checked first, then the household's. |
| "Play Radio 2 on the Denon." | The same, on the speaker called Denon. |
| "Save Radio 2 as a favorite." | Radio 2 is found in Music Assistant and saved to your own list. |
| "Save this as Radio 2." | The station or stream that is playing on that speaker is saved to your own list as Radio 2. |
| "Save this as Jazz for the household." | The same, saved as Jazz to the household's list, which everyone can play. |
| "Forget Radio 2." | Radio 2 is removed from your favorites. |

Saving and forgetting are understood by your main agent, so the exact words do not matter much. Playing is stricter: the instant "quick path" only plays a favorite when the whole request is "play" and its name. See [The quick path and the tools](#the-quick-path-and-the-tools).

An unknown voice never reaches favorites, just as it never reaches the main agent.

## Switch favorites on

Favorites are switched on for each assistant.

1. Open **Voice Manager** in the sidebar and go to **Favorites**.
2. Choose the assistant and switch on **Use favorites**. The same switch is under **Favorites** in the assistant's settings, see [Settings](settings.md#favorites).

Changes apply to the next request. No restart is needed.

## Switch the tools on for the main agent

The tools let the main agent handle everything except the exact "play" request: saving, forgetting, and any other wording of a play request. They are offered by Voice Assistant Manager under the name **Voice Assistant Manager**, and each main agent has to be told to use them.

1. In Home Assistant, go to **Settings > Devices & services** and open the integration your main agent belongs to (the agent you chose as **Main agent** for the assistant).
2. Open the settings of that agent.
3. Where you choose what the agent may use, switch on **Voice Assistant Manager** next to **Assist**, and save.

Why this is needed: the quick path is fast but narrow. It only catches a request that is exactly "play" followed by a favorite's name. Anything else goes to the main agent, and without the tools the main agent does not know your favorites. It then falls back to searching Music Assistant, which may pick the wrong station.

The Voice Manager page warns you when favorites are on for an assistant whose main agent does not have the tools switched on. When your main agent's integration does not say which tools it uses, the check cannot be made and no warning appears.

## The quick path and the tools

| | Quick path | Tools |
| --- | --- | --- |
| Handles | "Play Radio 2", "Play Radio 2 on the Denon" | Every other wording, and saving and forgetting by voice |
| Speed | Instant, no model call | One extra round trip to your main agent |
| Needs | Favorites switched on | Favorites switched on, and the tools switched on for the main agent |
| Language | English only | Any language your main agent understands |

The quick path ignores "please", "can you", "could you", "would you" or "will you" at the start, and "please" and punctuation (such as a full stop or a question mark) at the end. Anything else in the request means it is not the quick path's, and the request goes on unchanged to the built-in commands and the main agent. A name that contains "on" or "in" ("Radio on the hour") still works, because the whole text is tried as a name first.

When the quick path plays something, the reply is "Playing Radio 2." or "Playing Radio 2 on Denon." The exchange is kept in the conversation history, but not in memory.

## Whose list is used

Each person has their own list, and the household has one. When someone asks for a favorite, their own list is checked first and the household's second. A person's favorite wins over a household favorite with the same name.

- **Your list is shared across assistants.** Alex's favorites are the same on the kitchen assistant and the office assistant.
- **The household's list is for everyone.** Anyone recognised as Person 1 or Person 2 can add to it and remove from it, by voice or on the pages.
- **The same person everywhere.** Alex's own list is only used while Person 1 is the same person in every assistant that has favorites on: the same name and, where one is chosen, the same Home Assistant user. If two assistants disagree, that person's own list is not used on any of them until the settings agree. The household's list still works. The Voice Manager page names the assistants that disagree, and My assistant tells the person to ask an administrator. Unlike [memory](memory.md#who-knows-what), this does not depend on memory's "Use the lists shared by all assistants" switch.
- **Names are compared loosely.** Capitals, punctuation and a leading "the" are ignored, so "the news", "The News" and "news" are the same name. A list may not have two favorites with the same name or other name.
- **Other names.** A favorite can have up to 5 other names, so "Radio 2" can also be played as "BBC Radio 2" or "the radio".

## Which speaker plays

- **Naming a speaker.** "…on the Denon" is matched against your media players by name, by entity id, by the aliases you gave them in Home Assistant, and by area ("in the kitchen"). If exactly one player matches, it plays there. If none or several match, nothing plays and the assistant says which.
- **Not naming one.** The favorite plays on the satellite's own speaker: the media player that belongs to the same device as the satellite. If that device has a Music Assistant player, it is preferred.
- **A speaker chosen for a satellite.** Under **Voice Manager > Favorites > Speakers** you can choose a different speaker for each satellite, for example when the satellite's own speaker is small and a better one sits next to it. A satellite with nothing chosen plays on its own.
- **The phone app and the browser** have no speaker of their own, so the request must name one: "Play Radio 2 on the Denon". Without one, the assistant says there is no speaker and asks which to use.
- **A player that is unavailable** is never chosen. Name one that is unavailable and the assistant says "Denon is not available right now" and plays nothing. When a satellite's chosen speaker is unavailable, another media player on the satellite's own device is used if there is one.
- **Nothing plays on a speaker you did not name or imply.**

On the quick path, a request that names a favorite but whose speaker cannot be worked out is not answered by it. It goes straight to the main agent, who can ask which speaker you mean. Home Assistant's built-in commands are skipped for it, so they cannot search Music Assistant and play a different station of the same name.

## Music Assistant or a stream address

A favorite is one of two things: an item in [Music Assistant](https://music-assistant.io), or the address of a stream (a link starting with `http://` or `https://`).

| | Music Assistant item | Stream address |
| --- | --- | --- |
| Can be | A radio station, podcast, playlist, album or track | A stream |
| Plays on | A Music Assistant speaker only | Any media player |
| Saved by name ("save Radio 2") | Yes | No, add it on a page |
| Needs Music Assistant | Yes | No |

- **Podcasts, playlists and anything found in Music Assistant need Music Assistant.** They play on a Music Assistant speaker and replace what is queued there. A podcast or playlist starts with whatever Music Assistant starts with by default.
- **A stream plays anywhere.** On a Music Assistant speaker it is played by Music Assistant, on any other media player it is played directly.
- **A Music Assistant item on another kind of speaker.** The assistant says "Radio 2 needs a Music Assistant speaker" and plays nothing. Name a Music Assistant speaker, or add the station again as a stream address.

## Saving by voice

### "Save this as Radio 2"

"Save this as Radio 2" saves what is playing on the speaker you are talking to, under the name you give. The name always comes from you: a speaker often reports the song on air rather than the station, so it is never used. A bare "save this" saves nothing; the assistant asks "What should I call it?" and saves it once you give a name.

It can only save a **station or stream**:

- On a Music Assistant speaker: a radio station, or an `http://` or `https://` stream.
- On any other media player: an `http://` or `https://` stream.

A song or a podcast episode cannot be saved this way. The assistant says "I can only save a station or stream that is playing. Ask me to save it by name." Podcasts and playlists are saved by name. With nothing playing, it says "Nothing is playing that I can save."

### "Save Radio 2 as a favorite"

Saving by name needs Music Assistant. The assistant searches it for a radio station, then a podcast, then a playlist, and looks at the first few results:

1. A result whose name is exactly what you said is saved.
2. A result whose name ends with what you said, as whole words, is saved too, so "Radio 2" finds "BBC Radio 2".
3. Otherwise nothing is saved. The assistant offers up to 3 of the closest names, and you say which you mean.

Without Music Assistant, the assistant says "I can only save that by name with Music Assistant." Add a stream address on a page instead.

A favorite goes to your own list. Say it is for the household or for everyone to save it to the household's list. If a favorite with that name is already in the list, or the list is full, the assistant says so and saves nothing.

"Forget Radio 2" removes it from your own list. Say it is from the household's favorites to remove it from there.

## Manage favorites on the pages

- **My assistant.** Each person's **Favorites** section shows their own list and the household's. A person can find something with Music Assistant, add a stream address, change a name or its other names, and remove one. Changes are saved at once and are the same on all of their assistants. Any user who is Person 1 or Person 2 in an assistant can change the household's list. While favorites are off for all of a person's assistants, the section shows only a notice saying so, without either list, the household's included.
- **Voice Manager.** Administrators see **Favorites** with the switch for the assistant, the lists of both people and the household's, all editable in the same way, and the **Speakers** for each satellite. Warnings appear here too: an assistant whose main agent does not have the tools, and people who are not the same on every assistant.

To add something from Music Assistant, choose **Find with Music Assistant**, search for a station, podcast or playlist, and choose **Add** next to the right result. To add a stream, choose **Add a stream address**, give it a name such as "Radio 2" and the address of the stream.

Favorites are kept in `.storage/voice_assistant_manager.favorites` in your Home Assistant configuration. They create no entities. Removing the last assistant deletes every list of favorites.

## Limits

| What | Limit |
| --- | --- |
| Favorites per list | 100. Each person's list and the household's count separately. |
| A name or another name | 80 characters |
| Other names per favorite | 5 |
| A stream address | 2000 characters, starting with `http://` or `https://`, without spaces |
| The quick path | English only |

See also [Limits](limits.md) and [Troubleshooting](troubleshooting.md).
