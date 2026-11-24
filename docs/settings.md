# Settings

Part of the Voice Assistant Manager documentation. Back to the [README](../README.md).

These are the settings of one assistant. They are the same on the [settings page](../README.md#the-settings-page) and in the Configure dialog (**Settings > Devices & services > Voice Assistant Manager > Configure**).

Changes apply to the next request. No restart is needed.

## Agents

| Setting | What it does |
| --- | --- |
| Main agent | Answers Person 1 and Person 2, with its normal tools. |

## Built-in commands

| Setting | What it does |
| --- | --- |
| Try built-in commands first | A request that exactly matches one of Home Assistant's own commands is carried out at once, without the main agent. Recognised speakers only. |
| Leave questions and music searches to the main agent | State questions ("is the door closed") and play-music requests go to the main agent even when a built-in command matches. |

## Memory

| Setting | What it does |
| --- | --- |
| Remember things about the people who live here | Turns memory on for this assistant. Needs the agent below. |
| Agent that decides what to save | Reads each exchange after the reply has been given. Must exist, and may not be the main agent, another assistant or an agent that can control the home. Give it no tool providers. It is checked again before each use. When it is refused, remembered facts are still used and nothing new is saved. |
| Save important facts without being asked | When off, only "remember" and "forget" requests change the memory. |
| Most facts kept per list | 10 to 200, default 50. Also the most facts shown to the main agent. |
| Use the lists shared by all assistants | Read the shared lists, and save to them when the person asks for all assistants to remember something. |
| Share what each person tells it with the other person | On by default. The assistant knows what either person told it, whoever is speaking, except facts a person asked to keep private. When off, a person's facts are only used when that person speaks. |

## Reminders

| Setting | What it does |
| --- | --- |
| The main agent can set reminders | Adds reminder rules to the main agent's prompt, including who is speaking, keeps reminders out of memory, and corrects a reply that confirms a reminder that was not set. See [Reminders](reminders.md). |
| Assistant name | The name this assistant goes by in reminders. When empty, the first word of the assistant's name. See [Assistant names](reminders.md#assistant-names). |
| Announcement agent (no tools) | Writes every line this assistant announces: spoken reminders (the `compose_announcement` action) and [calendar events](calendars.md). No tool providers, and not the main agent or an assistant. It may not be able to control the home. Checked when you save (only while reminders are on or calendars are watched) and again before each use; when it is refused, announcements fall back to plain text. When empty, the agent that writes the refusal is used. |
| Extra instructions for announcements | Applies to spoken reminders and to calendar events alike, although it sits under Reminders. Added after the recipient's persona and in front of the built-in instructions. See [Steering the style of the line](calendars.md#steering-the-style-of-the-line). |
| Reminder calendars of Person 1 | The calendars that hold Person 1's reminders, for example "Alex Reminders" and "Alex Reminders (announce)". [My assistant](../README.md#my-assistant) lists reminders from these. Needs Person 1's Home Assistant user. A calendar may belong to one user only, across all assistants, and may not also be a household calendar. |
| Reminder calendars of Person 2 | The same, for Person 2. |
| Reminder calendars of the household | The calendars that hold reminders for both. The same calendar may be listed in several assistants. |
| Line spoken when a reminder could not be set | Replaces a false confirmation. When empty, a plain line is used. |

## Favorites

| Setting | What it does |
| --- | --- |
| Use favorites | Off by default. Switches favorites on for this assistant: while it is off, the assistant does not use favorites at all, by voice or through the main agent's tools. When on, a request that is exactly "play" and a favorite's name ("play Radio 2", "play Radio 2 on the Denon") plays it at once, for Person 1 and Person 2 only, before the built-in commands and without the main agent. Each person's own favorites are shared by every assistant with this on. Other wordings, saving and forgetting need the Voice Assistant Manager tools switched on for the main agent. See [Favorites](favorites.md). |

The lists themselves, and the **Speakers** (which speaker plays a favorite on each satellite when none is named), are in the **Favorites** section of the Voice Manager page, not in the Configure dialog. Speakers are the same for every assistant.

## Chat show

Found under **Assistant** on the settings page. Used only when this assistant talks with another one in a [chat show](chat-show.md), never in normal conversations.

| Setting | What it does |
| --- | --- |
| Character for chats | How this assistant behaves in a chat, up to 2000 characters. A good start is the personality part of the main agent's prompt, without anything about controlling the home or tools. When empty, **Extra instructions for announcements** is used. |
| Voice for chats: text-to-speech engine | The engine that speaks this assistant's chat lines. When empty, the speaker's own voice is used. A media player without one uses Home Assistant's default text-to-speech engine. |
| Voice for chats: voice | The voice to ask the engine for. Optional. |

## Avatar

Found under **Assistant** on the settings page. An avatar is a lip-sync face for this assistant: a text-to-speech entity that speaks through the engine you choose and sends a copy of the speech to a LiveTalking server. Add, change or remove it there. The card's own **Save** stores the avatar at once; it does not need **Save changes**. See [Avatar](avatar.md).

## Calendars

| Setting | What it does |
| --- | --- |
| Script that speaks | Called for every calendar event that is due. See [Calendar announcements](calendars.md). When empty, only an event is fired. |
| All-day events are announced at | The time of day for a calendar that has no time of its own. 09:00 unless you change it. |
| Catch up after a restart for at most | Events that became due while Home Assistant was off are still announced when they are less than this many hours late. 3 unless you change it. |

## Person 1 and Person 2

| Setting | What it does |
| --- | --- |
| Name | Put in front of that person's messages in the shared history. |
| Home Assistant user | A typed request (phone app, browser, REST) made while logged in as this user counts as this person, whatever device it names. See [Phone app and browser](../README.md#phone-app-and-browser). |
| Persona | Instructions added to the main agent's prompt when that person speaks. |

## Unknown voices

| Setting | What it does |
| --- | --- |
| How to answer | A refusal written by an agent without tools, or fixed lines only. |
| Agent that writes the refusal | Must exist and have no tool providers. It may not be the main agent or another assistant of Voice Assistant Manager, and may not be able to control the home: saving is refused. It is checked again before each use. When it is refused, unknown voices get the fixed lines. See [An agent was refused](troubleshooting.md#an-agent-was-refused). |
| Instructions for the refusal | What the refusal agent is told about the person speaking. |
| Fixed lines | One per row. Used when fixed lines are chosen, and as a fallback when the refusal agent fails or takes longer than 6 seconds. |

During a [chat show](chat-show.md#joining-in-and-stopping), and for a short while after it, an unknown voice on the chat's satellite joins the chat as a guest instead.

## Almost recognised

| Setting | What it does |
| --- | --- |
| Ask to try again on a near miss | Turns the "you sound familiar" reply on. |
| Lowest score that counts as a near miss | Compared with the satellite's last speaker score sensor. |
| Instructions and fixed lines | The same idea as for unknown voices, for the try-again reply. |

## Advanced

| Setting | What it does |
| --- | --- |
| Shared history rule | Added to the main agent's prompt after the persona. Use it to explain the name labels. |
| When the main agent fails, say | Spoken when the main agent cannot answer, for example because its AI provider is down, instead of ending the turn in silence. When empty: "Sorry, I can't think right now. Please try again in a moment." The error is still logged. |
| Wait for the voice match | How long to wait while the helper says `pending`. If no match arrives in time, the voice counts as unknown. A follow-up in the same conversation waits just as long, then keeps the person of the turn before (see [Setting it up by hand](../README.md#setting-it-up-by-hand)). |
| A device without speaker recognition is | Who to assume when nothing else decides: a request with no helper and no user (an automation, say), a VoIP phone call, or a typed request when no user is chosen under Person 1 or Person 2. |
| Avatar connection | For satellites in [avatar mode](avatar.md#satellites-in-avatar-mode). **Connect when woken** (default): the satellite connects to LiveTalking when it wakes, about a second before the avatar shows. **Always connected**: the avatar is there at once, and each satellite keeps a LiveTalking session open. |

## What the main agent is told

Every request that reaches the main agent starts its extra instructions with "The person speaking now is Alex." (the person's name, or "Person 1" or "Person 2" when no name is set). The rest follows in this order, and each part is left out when it is empty:

1. The persona of that person.
2. What the assistant remembers.
3. The shared history rule, and the memory rules.
4. The reminder rules.
5. Any extra instructions that came with the request.

The first line is always there, so the prompt is never empty. Home Assistant reuses the previous turn's extra instructions when a turn has none, and this keeps the last speaker's persona and facts from leaking into the next turn.

If the assistant cannot work out who is speaking, the main agent is not asked at all. The reply is "Sorry, I can't help you." See [Limits](limits.md).

## Example texts

Say the household is Alex and Sam, and the assistant is a sarcastic butler.

**Person 1 persona**

> The person speaking is Alex. Their partner is called Sam. Behave exactly as your main instructions say. Use their name only when it comes naturally, not in every reply.

**Instructions for the refusal**

> You do not know the person speaking. They are not part of this household. In your usual style, politely and drily refuse to help. Do not carry out the request and do not give out any information about the house or the people who live in it. One or two sentences at most.

**Fixed lines**

```
I'm afraid I only take instructions from the household.
We have not been introduced, so the answer is no.
```

**Shared history rule**

> The conversation history is shared by the household. Every user message starts with the name of the person who said it, followed by a colon. Answer only the person named at the start of the latest message. Never start your own reply with a name and a colon, and never read these labels aloud.
