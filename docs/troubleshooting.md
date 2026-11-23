# Troubleshooting

Part of the Voice Assistant Manager documentation. Back to the [README](../README.md).

Turn on debug logging for the integration (the entry's menu > **Enable debug logging**) to see whether a request was handled by a built-in command.

| Problem | Likely cause |
| --- | --- |
| Everyone is treated as Person 1 | No helper exists with the device id of that satellite, so the "device without speaker recognition" setting applies. On the phone app or browser, choose each person's Home Assistant user in the settings. |
| The phone app or browser is refused | Users are chosen in the settings, and the one logged in is not one of them. Or the assistant's settings were never saved: such an assistant refuses every typed request. Save the settings, or run the setup guide. |
| A VoIP phone is refused | A VoIP call runs as a system user, which cannot be chosen as Person 1 or Person 2. Once any user is chosen, it is an unknown voice. With none chosen, "A device without speaker recognition is" decides. |
| Everyone gets "Sorry, I can't help you." | The assistant could not work out who is speaking: the lookup failed (see the log), or the assistant has no saved settings and no "who is speaking" script, or that script failed or answered with nothing. The main agent is not asked. |
| Recognised people are refused | The helper still says `pending` when the wait runs out. Raise "Wait for the voice match", or check that your automation writes the role. |
| A satellite refuses everyone | Its helper is empty or holds a value other than `owner`, `partner`, `stranger` or `pending`. Check that the automation includes this satellite and writes exactly those words. |
| An unknown voice can still switch things | "Prefer handling commands locally" is still on for that voice assistant pipeline, or the command is a sentence-trigger automation. |
| The refusal is always a fixed line | The refusal agent is missing, is the same as the main agent, returned an error, or took longer than 6 seconds. |
| Unknown voices hear fixed lines, though a refusal agent is chosen | The refusal agent was refused, see [An agent was refused](#an-agent-was-refused). The status line on the Voice Manager page says so. |
| Nothing new is remembered, but old facts still show up | The memory agent was refused, see [An agent was refused](#an-agent-was-refused). |
| Announcements are plain text, though an announcement agent is chosen | The announcement agent was refused, see [An agent was refused](#an-agent-was-refused). |
| My script is refused when a non-admin runs it (`Unauthorized`) | `compose_announcement` can only be called by an administrator or by a call with no user. A script that calls it fails for a non-administrator who starts it by hand. Run it from an automation, or as an administrator. |
| My assistant says a persona cannot be saved until an administrator fixes the other settings | The assistant's settings have an unsafe agent (refusal, memory or announcement agent). An administrator opens the Voice Manager page, where the status line says which. |
| A calendar did not answer within 30 seconds (warning in the log) | The calendar integration is slow or stuck. That check counts as failed for this calendar, and it is caught up when it answers. My assistant lists it as unreadable. |
| A calendar had several events due at once (warning in the log) | At most 5 events are announced per calendar per check, the 5 soonest. The others are marked as announced without a line, so they are never announced later. This is normal after a bulk import or a long outage. |
| `Skipping <calendar id>: its lead time <value> is not between 0 and 1440 minutes` (warning in the log) | The lead time of that row is not a number of minutes from 0 to 1440. Fix it under Configure > Calendars. The other rows are checked. |
| Could not get the events of a calendar, or it did not answer with a list (warning in the log) | The calendar integration returned an error or something that is not a list of events. It is logged once until it works again. |
| A reminder is confirmed but never appears | The main agent did not call `create_reminder`. With reminders on, the assistant replaces such a reply when it can see the tool calls. Check that the script is exposed to Assist. |
| A reminder is set for the wrong date, or one in the past | The script asks the main agent for a full date, which a language model cannot know. Take "in so many minutes", a time and a day instead. See [What Home Assistant has to provide](reminders.md#what-home-assistant-has-to-provide). |
| "Remind me" sets a reminder for the wrong person | The script's `for_whom` values do not match the lower-case names of Person 1 and Person 2 in the assistant's settings. |
| `compose_announcement` returns empty text | No assistant goes by the `assistant` name given (or two do), the satellite's primary voice assistant pipeline does not use one, the recipient is not one of the two names or `household`, no announcement agent is set, or the agent took longer than 6 seconds. |
| A satellite always shows the same assistant | It needs a select ending in `_next_announcement_assistant` (EchoLocal 0.0.16 or later) that is enabled, and its **Animate announcements** switch on. **Show which assistant is speaking** must be on at the bottom of the Voice Manager page. An assistant that is on neither of the satellite's wake words shows the default character. For your own announcements, call [`prepare_announcement`](reminders.md#the-prepare_announcement-action) just before announcing. See [Chat show](chat-show.md#showing-who-speaks). |
| A reminder is spoken by the wrong assistant | The main agent passed a different `assistant` than your script expects. Check the Assistant name in the assistant's settings. |
| A calendar event is not announced | The calendar is not listed under Configure > Calendars, the event does not contain the "only events containing" text, no script is chosen, or the event became due more than the catch-up time ago. The log has a warning when a calendar cannot be read. |
| A calendar event is announced as its raw title | The `line` was empty: no announcement agent is set, it has tools, or it took longer than 6 seconds. |
| A calendar event is announced twice | The calendar is listed in two assistants, another automation announces it as well, or its title or times were changed after it was announced. |
| The Voice Manager page is missing from the sidebar | You are not logged in as an administrator, or the page was hidden. Open `/voice-assistant-manager` and turn on "Show this page in the sidebar". |
| The Voice Manager page looks old after an update | The browser kept the earlier version. Reload the page with the cache cleared. |
| The Overview says local commands are on | The pipeline a satellite uses still has "Prefer handling commands locally" on, so unknown voices can switch things before the assistant is asked. Use **Switch off local commands** on the Overview, or switch the option off in Settings > Voice assistants. |
| The guide finds no wake word or pipeline for a satellite | The guide could not find the pipeline select of that satellite. Set its pipeline in Settings > Voice assistants, then reopen the guide from the Overview with **Run the setup guide again** (or **Finish setting up** while the guide is not finished) and go to step 4. |
| The guide offers no tracking automation | The satellite has no `_last_speaker_score` sensor, or no `_last_speaker` sensor, so the guide cannot write the automation. Your speaker recognition has to write the helper. See [Set up](../README.md#setting-it-up-by-hand). |
| My assistant says to ask an administrator | The user is not chosen as Person 1 or Person 2 on any assistant. An administrator chooses them under People > Home Assistant user. |
| My assistant shows no reminders | No reminder calendars are listed for that person or the household under Settings > Reminders, or they have no events in the next 30 days. A calendar that cannot be read is named on the page. |
| My assistant cannot add a reminder | `script.create_reminder` does not exist, or it returned an error, whose message is shown. A reminder "For me" also needs the person's name under People. See [What Home Assistant has to provide](reminders.md#what-home-assistant-has-to-provide). |
| A reminder cannot be deleted | Its calendar does not allow deleting (for example a calendar that comes from the internet), or it is not one of that person's or the household's reminder calendars. |
| Saving reminder calendars is refused | A personal calendar is already used for another Home Assistant user, or is also a household calendar, or the person has no Home Assistant user. When another assistant already uses the calendar, the message names that assistant. When the clash is inside the assistant being saved (the same calendar for both people, or for a person and the household), the message says the calendar is chosen twice there. |
| Nothing plays, or the assistant says "<name> is not one of your favorites or the household's" | The name is not on your list or the household's, with the exact name or one of its other names. Check the spelling on My assistant or the Voice Manager page, or add another name for it. Your own list is also not used while the assistants disagree about who you are (see below); the assistant then says "<name> is not one of the household's favorites". |
| A favorite plays on the wrong speaker | A speaker was chosen for that satellite under Voice Manager > Favorites > Speakers, or the satellite's device has more than one media player (a Music Assistant one comes first). Choose the speaker you want there, or name it: "play Radio 2 on the Denon". |
| "<name> needs a Music Assistant speaker" | The favorite is a Music Assistant item (not a stream address) and the speaker is not a Music Assistant player. Name a Music Assistant speaker, or add the station again as a stream address. |
| "Favorites are off for this assistant" | The switch under Voice Manager > Favorites is off for the assistant that heard the request. |
| "Play Radio 2" is not played instantly, but searched for | The quick path only plays "play" and a favorite's name, in English, for Person 1 or Person 2. Anything else goes to the main agent, which needs the Voice Assistant Manager tools switched on to know your favorites. |
| "There is no speaker for this satellite" | The request came from the phone app or a browser, or the satellite has no media player on its device. Name a speaker ("on the Denon"), or choose one for the satellite under Voice Manager > Favorites > Speakers. |
| "<name> is not available right now" | The speaker is unavailable in Home Assistant. Check that it is on and connected. |
| My assistant says the assistants disagree about who I am | Person 1 or Person 2 is not the same person in every assistant that has favorites on (a different name or Home Assistant user). Your own favorites are not used until they agree, the household's still are. An administrator fixes it under People in the assistants' settings. The Voice Manager page names the assistants. |
| The Voice Manager page says the main agent cannot use favorites | The Voice Assistant Manager tools are not switched on for the main agent. In Settings > Devices & services, open the main agent's integration and its agent settings, and switch on Voice Assistant Manager next to Assist. See [Favorites](favorites.md#switch-the-tools-on-for-the-main-agent). |
| "I can't tell who is asking" | A favorites tool was called without the assistant's note of who asked. Either the main agent was used directly instead of through the assistant (a voice pipeline set to the main agent, an automation, or another assistant calling it), or the turn took longer than 60 seconds. Use the assistant itself, or ask again. |
| The assistant asks "What should I call it?" | "Save this" needs a name: say "save this as Radio 2". The name is never taken from the speaker, which often reports the song on air instead of the station. |
| Nothing is remembered | Memory is off, no memory agent is chosen, or the memory agent does not answer with JSON. Turn on debug logging to see one line per memory pass. |
| The Chat show will not start | A chat is already running, the same assistant is chosen twice, or an assistant has no announcement agent without tools (set under Reminders in its settings). The speaker must be a satellite that can announce or a media player that can play media. |
| The Chat show ends after one line | The announcement agent failed or answered with nothing, or the speaker stayed busy. The card says which, and the Home Assistant log has the details. |
| The two assistants sound the same | Both use the same announcement agent and have no "Character for chats". Give each one a character, and a voice. See [Chat show](chat-show.md#giving-each-assistant-a-character). |
| Saying something during a chat does nothing, or is refused | Joining in works on satellites only, and the wake word must use an assistant. A voice the assistant cannot identify is refused while a chat runs. On a media player the chat is listen-only. |
| The satellite wakes during a chat line | The assistants say each other's names, which may be its wake words. The assistant ignores what it hears then and the chat goes on. To join in or say stop, wake the satellite between lines. The Stop button always works. |
| The chat stopped after a restart | A chat is not kept when Home Assistant restarts or one of the two assistants is reloaded. Start it again. |

## An agent was refused

Three agents must have no tools: the one that writes refusals for unknown voices, the one that decides what to save, and the one that writes announcements. Each must exist, and may not be Home Assistant's own agent, an assistant of this integration, or the main agent of one. It may not be able to control the home either. This is checked when the settings are saved, and again before every use. If an agent fails it, the assistant never asks it:

- **Refusal agent:** unknown voices hear the fixed lines.
- **Memory agent:** remembered facts are still used, but nothing new is saved.
- **Announcement agent:** announcements stay plain text.

The Voice Manager page shows an error line for it, and the Home Assistant log says which agent it was. It is logged once for the refusal and memory agents, and each time a line is written for the announcement agent.

To fix it, choose an agent without tools (Assist and other tool providers off), or one that exists. Things to know:

- The assistant judges "can control the home" only from what the agent's integration reports. An agent whose integration does not report it is trusted, so make sure these agents have no tools yourself.
- An agent that has no state in Home Assistant is refused. That is an agent that is missing, or an old-style id that is not an entity.
- An agent that gained tools after you saved the settings is caught the next time it is used.
