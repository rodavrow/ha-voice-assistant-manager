# Chat show

Part of the Voice Assistant Manager documentation. Back to the [README](../README.md).

Two of your assistants talk to each other on a speaker, for fun. Each one keeps its own character and its own voice. People in the room can join in, or tell them to stop.

<img src="images/chat-show.png" alt="The Chat show section of the Voice Manager page: Jeeves and Ada chat about socks on the Kitchen satellite, with the limits, the Start and Stop buttons and the transcript" width="800">

> [!NOTE]
> The assistants can only talk. The lines are written by each assistant's announcement agent, which has no tools. The main agent, memory, reminders and the state of your home are never used.

## What you need

- **Two assistants** of this integration. Each needs an announcement agent without tools, chosen under **Reminders** in its settings. See [Settings](settings.md#reminders).
- **A speaker.** A voice satellite that can announce, or any media player that can play media.
- **An administrator.** The Chat show is on the Voice Manager page, which is for administrators only.

## Starting a chat

1. Open **Voice Manager** in the sidebar and go to **Chat show**.
2. Choose the **First assistant** (it speaks first), the **Second assistant** and the **Speaker**.
3. Type a **Topic**, for example `Socks`, or leave it empty. The first assistant then picks one. A topic is at most 200 characters.
4. Check the limits, then press **Start**.

The transcript fills in while the chat runs. **Stop** ends it, but lets a line that is playing finish first; the card says so until it has. When the chat has ended, the card says why.

Only one chat runs at a time. The chat is kept in memory only: it ends when Home Assistant restarts or one of the two assistants is reloaded, and the transcript is gone after that.

You can also start and stop a chat from a script or a dashboard button. See [Actions](#actions).

## Giving each assistant a character

Open the **Assistant** section and look for **Chat show**. Each assistant has two settings there.

| Setting | What it does |
| --- | --- |
| Character for chats | How this assistant behaves in a chat, up to 2000 characters. When empty, **Extra instructions for announcements** is used. When both are empty, the card warns that the assistant will sound plain. |
| Voice for chats | A text-to-speech engine and, optionally, a voice. When empty, the chat is spoken in the speaker's own voice. |

Two assistants may share one announcement agent. The character is what makes them sound different.

A good start is to copy the personality part of your main agent's prompt into **Character for chats**. Leave out everything about controlling the home, devices and tools. The announcement agent has no tools, so those lines would only confuse it.

Example for Jeeves:

> You are Jeeves, a dry, put-upon butler. You are polite, a little weary and never rude. You think very little of Ada's ideas, and say so gently.

Example for Ada:

> You are Ada, a cheerful and curious assistant. You love silly questions and you tease Jeeves kindly.

## Voices

- **A satellite** plays each line as an announcement, in the assistant's chat voice, or in the satellite's own voice when none is set.
- **A media player** plays the line as text to speech. When an assistant has no chat voice, Home Assistant's default text-to-speech engine is used.

## Showing who speaks

Some satellites can show which assistant is talking: those whose firmware offers a select whose entity id ends in `_next_announcement_assistant`, with the options **Default**, **Assistant 1** and **Assistant 2**. Before every line, that select is set to the assistant about to speak:

- the assistant on the satellite's first wake word shows as **Assistant 1**, the one on the second as **Assistant 2**
- an assistant on neither wake word shows as the one the other assistant does not use, so the two never look the same

The choice holds for that one line, so it is made again for each line. The text of the line is sent along, so the device can show it too. If the select cannot be set, the line is still spoken, with the default character.

Speakers without such a select, and media players, are never touched. To stop it everywhere, switch off **Show which assistant is speaking** at the bottom of the Voice Manager page; a device may also have a switch of its own for this. Either way, when a line listens afterwards the satellite listens with its first wake word's assistant, whichever character was shown.

## Limits

The chat ends at whichever limit it reaches first.

| Limit | Default | Range |
| --- | --- | --- |
| Lines at most | 12 | 2 to 50 |
| Words per line | 30 | 5 to 80 |
| Seconds between lines | 2 | 0 to 10 |
| Minutes at most | 5 | 1 to 30 |

The words limit is a hard maximum: the assistants are asked for a little under it, and a line that still runs over is cut back to its last complete sentence.

**Save as defaults** keeps the values on the card for the next chat.

On a satellite that supports it, **Listen after each round (best effort)** makes the satellite listen after the second assistant's line, so someone can join in without waking it first. The chat goes on if nobody speaks. The last line of a chat never listens. Listening is only offered on a satellite whose first wake word uses an assistant, so a reply always reaches an assistant and never your main agent. Not every satellite or voice pipeline supports this, and it is not offered for the others.

A line the satellite listens after stays in that satellite's conversation in Home Assistant, which the satellite keeps using until it has been idle for five minutes. The assistant therefore gives your main agent a conversation of its own for requests in that conversation, so the main agent never sees a chat line. This only works for requests that reach an assistant. **Do not use listening on a satellite whose second wake word uses an agent that is not an assistant** (for example your main agent directly): a request through that wake word would carry the chat lines to that agent, and no assistant can step in.

## Joining in and stopping

This works on a **satellite** whose wake word uses any assistant. It does not need to be one of the two in the chat. On a media player, the chat is listen-only: stop it from the card.

| Who | What they say | What happens |
| --- | --- | --- |
| Anyone | `stop`, `enough`, `quiet`, `that's enough`, `be quiet`, `shut up` or `end the chat`, alone or at the start of what they say | The chat ends. The satellite says "Okay." |
| Alex or Sam (recognised) | Anything else | It becomes a line in the chat, with their name. |
| Anyone else, including a voice that was not recognised | Anything else | It becomes a line in the chat as "A guest". The assistants are told not to assume who that is, even if they give a name. |

The satellite says nothing back. The next assistant answers what was said, and both keep the whole chat in front of them, so they can come back to it later. People's lines do not count towards the line limit.

**Everyone can join in.** While a chat runs on a satellite, everything heard there goes to the chat, before the usual handling of unknown voices. This is safe because the assistants in a chat have no tools and only see their character text (the chat character, or the announcement instructions when no chat character is set), the built-in chat rules, the topic and the chat so far. They never see memory, reminders, the persona text written for Person 1 or Person 2, or the state of your home. Speaker recognition is optional here: it only decides whether a line shows a name or "A guest". As soon as the chat and its grace (below) have ended, unknown voices are handled by your usual rules again.

**Half-finished turns are dropped.** A turn that is only a greeting and an assistant's name ("Hey Ada."), or one of a single word ("I'm") or ending on a word a sentence cannot end on ("Well I think the"), is not added. A question or an exclamation ("Why?", "Oh my!") is always added. In languages written without spaces, only a turn with nothing but the assistant's name is dropped. The chat goes on. If the satellite often cuts you off, look for a setting on the device for how long it waits before it decides you have finished speaking (ESPHome satellites often have a "Finished speaking detection" select).

The assistants say each other's names, so the satellite may wake on its own line when a name is its wake word. The assistant ignores what the satellite hears then (it answers with nothing) and the chat goes on with the next line. To join in or say stop, wait until a line has ended and wake the satellite between lines.

A line that is playing is never cut off. If the satellite is busy because someone woke it, the chat waits for it, and ends if it stays busy.

What people say while joining in is only used in the chat. It never reaches the main agent, memory or the shared history. This also holds for about a quarter of a minute after the chat ended, while the satellite may still be listening: a late reply gets no answer and joins nothing, and a late stop word gets "Okay.".

Each turn during a chat is logged at INFO by how it was taken (joined with a name, joined as a guest, fragment dropped, stop, self-wake, after the chat). What was said is never logged.

## Why a chat ended

| Shown on the card | Meaning |
| --- | --- |
| The chat reached its line limit. | It did as many lines as you allowed. |
| The chat reached its time limit. | It ran for the minutes you allowed. |
| The chat was stopped. | Someone pressed **Stop**, called `stop_chat` or said a stop word. |
| An assistant could not write its next line, so the chat ended. | The announcement agent failed, answered with nothing or took too long. |
| The speaker stayed busy, so the chat ended. | Someone kept the satellite busy. |
| The speaker could not play a line, so the chat ended. | The satellite or media player returned an error or became unavailable. |
| The chat stopped because of an unexpected error. | The Home Assistant log has the details. |

## What the assistants know

Only their own character, the other assistant's name (the assistant name from **Reminders**, else the first word of the assistant's title), the topic, the chat so far and the names of people who joined in. The topic and what people say are treated as conversation, not as instructions.

## Actions

Both actions need an administrator.

| Action | What it does |
| --- | --- |
| `voice_assistant_manager.start_chat` | Starts a chat. Fields: `a` and `b` (the config entries of the two assistants), `speaker`, and optionally `topic`, `max_lines`, `words`, `pause`, `minutes` and `listen`. A limit left out uses the saved default. |
| `voice_assistant_manager.stop_chat` | Stops the chat that is running. |

Example:

```yaml
action: voice_assistant_manager.start_chat
data:
  a: 0123456789abcdef0123456789abcdef
  b: fedcba9876543210fedcba9876543210
  speaker: assist_satellite.kitchen_satellite
  topic: Socks
  max_lines: 8
```
