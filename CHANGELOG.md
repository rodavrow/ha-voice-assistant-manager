# Changelog

All notable changes to Voice Assistant Manager. Versions follow the `version` in
`custom_components/voice_assistant_manager/manifest.json`.

## Unreleased

### Added

- **Assistants can look reminders up.** "What reminders are there on Wednesday?" or "do
  we have a reminder for charging the car?" now gets an answer from the reminder
  calendars: each matching reminder with when it is next due, how it repeats, when that
  ends, whether it is spoken and which assistant took it. A person is shown their own
  reminders and the household's, never the other person's; the assistant decides that
  from who is speaking, not the main agent. Needs the new example script
  [`list_reminders`](examples/list_reminders.yaml), exposed to Assist, which uses the new
  `voice_assistant_manager.list_reminder_events` action. An answer about reminders that
  the main agent gives without looking them up is asked for once more, and replaced by
  "I could not look up the reminders. Please ask me again." if it still did not look.
  See [Reminders](docs/reminders.md#asking-which-reminders-there-are).
- **Avatars.** The **Avatar** card under **Assistant** on the Voice Manager page (or **Add
  avatar** on an assistant's entry) creates a text-to-speech entity that
  speaks through the engine you choose and sends a copy of each finished clip to a
  LiveTalking lip-sync server. The satellite's speech is passed on unchanged and made only
  once. The engine, address and an on/off switch can be changed at any time without a
  restart. Assistants without an avatar are not changed. See [Avatar](docs/avatar.md).
- **Satellites in avatar mode.** On satellite firmware with avatar mode, a satellite whose
  wake word uses an assistant with an avatar that is on shows the avatar's LiveTalking
  stream instead of its animations, and speaks through it, lip-synced. Voice Assistant
  Manager switches this on and off with the avatar, and sets the LiveTalking avatar and
  the new **Avatar connection** setting under Advanced (connect when woken, or always
  connected). The Avatar card reminds you to point **Voice for chats** at the avatar while it is
  on, and back at the normal engine once it is off.
- **The avatar stays on screen between chat lines.** On satellite firmware with an **Avatar
  hold** switch, a chat show turns it on, so the avatar that spoke last stays on screen,
  idle, until the next one speaks, and turns it off when the chat ends. See
  [Avatar](docs/avatar.md#between-the-lines-of-a-chat).
- **A chat on a cast device shows the avatars talking.** On a Nest Hub, Chromecast or
  Google TV, when both assistants have an avatar, the chat casts a stage: the speaking
  avatar fills the screen and the other waits in an inset, each with its voice from the same
  LiveTalking stream as its lips. Optional captions show each line. Set it up once with
  **Set up the stage** under **Chat show** on the Voice Manager page. It needs Home Assistant
  reachable over https. Whatever goes wrong, the chat plays as audio, as before. See
  [Chat show](docs/chat-show.md#on-a-cast-device).
- **A chat's avatars stay connected for the whole chat.** When a chat show starts, each
  assistant in it is connected to LiveTalking on the satellite showing it before the first
  line, and stays connected until the chat ends, also with **Connect when woken**. A new
  session opening between lines made LiveTalking's picture stutter on every speaker change.
  The assistant's own setting is not changed and applies again after the chat; other
  satellites and assistants are not touched; an avatar that cannot connect, or no room on
  the server, falls back to connecting when woken. See
  [Avatar](docs/avatar.md#for-the-whole-chat).
- **Satellites that can show which assistant is talking now do.** On a satellite whose
  firmware offers a select ending in `_next_announcement_assistant`, each Chat show line and each line from `compose_announcement` shows the assistant that speaks it.
  The new `prepare_announcement` action does the same for any other announcement, such as
  calendar events. Other speakers are never touched. Switch it off with **Show which
  assistant is speaking** on the Voice Manager page.
- **Audio tags for the voice.** A voice that takes directions in square brackets, such as
  `[laughs]`, `[whispers]` or `[out of breath after running]`, is given them when **Pass
  audio tags to the voice** is on under Advanced; they are never shown. With it off, the
  tags are taken out of what is spoken too. The one-word emotion tag at the very start
  still sets the avatar's face and is never passed to the voice. Replies and chat show
  lines keep their tags for the voice; announcements do not. See
  [Avatar](docs/avatar.md#audio-tags-for-the-voice).
- **The face from an opening audio tag.** When a reply opens with an audio tag and no
  emotion tag, the avatar's face comes from that tag, through the new **Face for audio
  tags** table under Advanced (`sarcastic: mischievous, smug`, one face per line), where it
  used to be neutral. The reply is not changed. A face must be on the Emotion tags list.
  With an empty field a built-in table is used, so `[excited]` now gives a happy face and
  `[smug]` a sarcastic one. See
  [Avatar](docs/avatar.md#the-face-when-a-reply-has-no-emotion-tag).
- **Chat shows keep moving.** While one assistant's line is spoken, the next line is already
  written and its audio made, so the silence between two lines is the pause you set and no
  longer also the time to write a line and make its voice. When someone joins in, the
  prepared line is thrown away and written again so it answers them. A satellite without a
  chat voice still makes its own audio when the line is spoken.
- **Replies streamed to the avatar.** For a satellite in avatar mode whose firmware has the
  switch **Avatar fed by Home Assistant**, a reply goes to its LiveTalking session
  (`/stream_audio`) while the engine is still making it, so the avatar starts speaking
  sooner instead of after the whole clip was downloaded and uploaded. The satellite keeps
  the normal audio as its backup. **Stream replies to the avatar** under Advanced turns it
  off; Voice Assistant Manager sets the satellite's switch to match. Other satellites are
  not affected. See [Avatar](docs/avatar.md#replies-streamed-to-the-avatar).
- **Emotion tags.** A reply, chat line or announcement may start with one tag: `[neutral]`,
  `[happy]`, `[sad]`, `[angry]` or `[sarcastic]`. The tag is never spoken or shown, also
  while a reply streams in, and on assistants without an avatar too. A missing or unknown
  tag counts as neutral; only a word of letters is a tag, so a reply that starts with
  `[1]` keeps it. For an assistant with an avatar, LiveTalking is told the emotion
  (`POST /set_emotion`) just before the audio, so the avatar can look the part. A
  LiveTalking without it is not affected. On the chat stage the emotion goes with each
  line's audio instead (an `emotion` field on `/humanaudio`). **Emotion tags** under
  Advanced sets which tags an assistant passes on (default: the five above). See
  [Avatar](docs/avatar.md#emotions).
- **Repeating reminders, and cancelling reminders by voice.** "Remind me every weekday at
  7", "every other Friday", "on the 1st of every month", "every day for 10 days": the new
  `voice_assistant_manager.create_reminder_event` action adds a repeating event to a Local
  Calendar, which Home Assistant's `calendar.create_event` cannot do.
  `voice_assistant_manager.cancel_reminder_event` removes a reminder by its title, a
  repeating one as a whole. The example `create_reminder` script has new `repeat` fields,
  and the new example `cancel_reminder` script is the tool for "stop reminding me". The
  assistant tells the main agent how to use both. Update your `create_reminder` script and
  add `cancel_reminder` to get them. See [Reminders](docs/reminders.md#repeating-reminders).
- **The main agent can cancel reminders**, under Settings > Reminders, on by default. Off,
  the main agent is told it cannot, and to point to the reminder calendar.

- **One more try when the main agent says it did something it did not.** When a reply says
  something was done ("I've set a reminder", "I've turned on the lights") and no tool was
  called in that turn, the main agent is asked once more, with a note that it called no
  tool, while its first reply is being spoken. If the second try does it, nothing more is
  said: what was spoken is now true. If it still does not, or fails, the honest line
  follows: "I could not set that reminder", or for anything else "I could not do that.
  Please ask me again." A second answer that says something else (it asks back, or says
  why it cannot) is spoken after the first, with "Sorry, let me put that right."

### Changed

- **A person can no longer cancel the other person's reminder.** Cancelling through an
  assistant now works for the reminders of the person speaking and the household's only;
  who is speaking comes from the assistant, not the main agent. **Update
  `script.cancel_reminder` from [the example](examples/cancel_reminder.yaml)**: it now
  passes `for_whom` to `cancel_reminder_event`, and without it a cancellation asked of an
  assistant is refused. Setting a reminder for the other person still works.

- **A clip is copied to at most 4 LiveTalking viewers**, down from 8, to match the
  server's session limit.
- **The voice for chats is chosen from a list.** When the engine names its voices, the Voice
  Manager page offers them instead of a text field, so a voice the engine does not have
  cannot be typed.
- **Everyone at the satellite can join a chat show.** While a chat runs, every turn from its
  satellite goes to the chat. A recognised voice joins with the person's name; anyone else
  joins as "A guest", and the assistants are told not to assume who that is. Unknown voices
  were refused before. The chat assistants have no tools and see no memory, so a guest
  reaches nothing; after the chat, the usual rules apply again.
- **No more "Mm-hm." when someone joins a chat.** The satellite says nothing and the next
  assistant answers. Turns of one word, or cut off mid-sentence, are dropped.
- **The two assistants in a chat never show as the same character.** One that is on neither
  of the satellite's wake words shows as the character the other does not use.
- **The favorites switch is now called "Use favorites".** It was "Play favorites by name",
  which read as if it only covered the quick "play …" requests. It switches the whole
  feature on for an assistant, and its description now says so. Nothing else changes.

### Changed

- **The wait for the voice match counts from the wake word.** "Wait for the voice match"
  (seconds after the person stopped speaking) is replaced by **The voice match arrives
  within** (seconds after the satellite started listening, default 6). A satellite matches
  on a fixed stretch of audio after the wake word, so its match lands a fixed time later
  however long the request is: a long request needs no wait at all, and a short follow-up
  only waits for what is left. A stored "Wait for the voice match" is not carried over,
  and no longer blocks saving the settings on the page.
  The wait counts from when `pending` was last written, also when the helper already
  said `pending` (left by a follow-up), so the next wake word is still waited for.

### Fixed

- **A reply that says something was done after every tool call failed is now caught.**
  The main agent asked for a light colour Home Assistant did not know, all its calls were
  refused, and it still said the lights were changed. A failed call counted as "a tool
  was used", so the reply was not checked. Now a turn in which no tool call succeeded is
  treated like one without any: the main agent is asked once more, told that its calls
  failed, and if it still says it was done the person hears "I could not do that. Please
  ask me again."
- **A recognised person is no longer told they are unknown when the voice match never
  arrives.** When a satellite gave no voice match (its helper stayed on `pending`, for
  example while its sensors were unavailable), the request was answered with the line for
  unknown voices. It is still refused, but now with "Sorry, I can't tell who is speaking
  right now. Please try again in a moment.", and a warning is logged once per satellite
  until a match arrives again.
- **Satellites no longer talk over a chat show playing elsewhere.** The assistants in a
  chat say each other's names, so a chat on a television could wake a satellite in
  earshot, which then refused the "unknown voice" aloud. While a chat plays on any
  speaker, and for a short time after, a voice that is not recognised gets no answer. A
  recognised person is answered as always.
- **A satellite's avatar no longer says a reply twice after Home Assistant restarts.** While
  a satellite's sensors were unavailable (after a restart, or when it dropped off the
  network), its LiveTalking session was no longer known, so its avatar got each reply from
  the satellite itself and a copy from Home Assistant, and said it two or three times. The
  last session a satellite reported now counts until it reports again, and when a
  session was never read no copy is sent at all, with one warning in the log.
- **A reply that was wrong is taken back out loud.** When the main agent said "I've set a
  reminder" without setting one, the text was corrected, but a reply long enough to be
  streamed had already been spoken: the voice said the reminder was set while the screen
  said it was not. The voice now follows such a reply at once with the truth: "Sorry,
  scratch that, I spoke too soon. I could not set that reminder. Please ask me again." A
  reply the voice had not started on is simply spoken as corrected. Replies still stream
  to the voice as they are written.
- **Chat lines on a cast device no longer lose their last syllables.** A voice whose engine
  makes WAV reached LiveTalking as an MP3 that Home Assistant converted on the fly. Such
  a file has no length in it, and LiveTalking's decoder guessed it short and dropped the
  end of the line, by up to a second on some lines. Lines for the stage are now made as
  WAV, whose length is exact.
- **A streamed WAV no longer loses its end on the avatar.** An engine that streams a WAV
  writes its header before it knows the length. LiveTalking read only as many samples as
  that header said, so a header that said too few cut the end of the line. The header is
  now set to what the clip really holds before it is sent.
- **A reply streamed to the avatar no longer loses its first words.** The audio sent to
  LiveTalking was missing its first 1.26 seconds ("Why did the scarecrow" of a joke), while
  the avatar stood idle: an ffmpeg option meant to cut delay made it throw that audio away.
  The whole reply is now sent, and the avatar gets its first audio about 1.3 seconds sooner.
- **A cancelled reminder is no longer reported as a reminder that could not be set.** After
  `cancel_reminder` removed a reminder, a reply such as "I've removed your reminder that was
  set for 8" was taken for a false "reminder set" and replaced by "I could not set that
  reminder", in the history too, so the assistant went on to believe it cannot cancel. A
  cancellation is now checked on its own: only a reply that claims one when
  `cancel_reminder` did not return ok is replaced, by "I could not cancel that reminder".
- **The end of a line is no longer cut on the chat stage.** On a cast device, the last
  syllable of a line could be cut when the other avatar took over or the chat ended:
  LiveTalking says it stopped speaking while the end of the line is still on its way to
  the device. Each line is now given at least as long as its audio lasts, plus a second
  for the device, and the avatars stay on screen for a few seconds after the last line
  before the stage closes. A chat started on the same device in those seconds is no longer
  stopped by the one before it ending.
- **A wake word reaches its own assistant, also right after the other one asked
  something.** On a satellite with two wake words, Home Assistant keeps one conversation
  for both, and sends a request in it to the assistant that last asked a question, so for
  five minutes after that the other wake word reached the wrong assistant. A request right
  after a wake word now goes to that wake word's assistant, with its follow-ups. It needs
  the satellite's **Last wake word** sensor. See
  [Two wake words on one satellite](docs/multiple-assistants.md#two-wake-words-on-one-satellite).
- **A satellite that drops its connection for a moment no longer ends the chat.** A satellite
  that reconnects during a line, as some do after a firmware update, ended the chat with
  "The speaker could not play a line". The chat now waits up to 15 seconds for it to come
  back and goes on; a line that had not started is spoken again. A chat that ends on a
  failure is logged as a warning, with the reason.
- **The avatar hold is turned off after a chat that ended while the satellite was away.** It
  stayed on until the satellite's own one-minute limit.
- **Nothing is written to a satellite in avatar mode while it is unavailable.** Each write was
  a warning in the Home Assistant log during a firmware update.
- **A chat line whose voice makes no audio is no longer silent.** When the engine of **Voice
  for chats** failed, for example for a voice it does not have, the speaker got an empty
  file: no sound, no avatar, and the chat ended as if all was well. The audio is now made
  before the line is announced. Without audio the line is spoken in the speaker's own
  voice and a warning names the engine and voice.
- **An avatar's copy only reaches its own face.** The copy of an avatar's speech for
  LiveTalking viewers went to every session, so with two avatars one assistant could speak
  with the other's face. With a **LiveTalking avatar** set, it now only goes to sessions
  showing that avatar; satellites' own sessions are still left out. Each clip logs, at
  debug level, every session and why it did or did not get the clip.
- **A main agent that fails no longer ends the turn in silence.** When the main agent
  raises an error (its AI provider is down, say) or answers with an error and no words,
  the assistant says a short line instead: "Sorry, I can't think right now. Please try
  again in a moment.", or your own under **Advanced**. The cause is logged; memory is not
  touched.
- **A follow-up keeps the person who was speaking.** A follow-up question has no wake word
  and usually no new voice match, and the tracking automation only let the last speaker
  carry on when the satellite started listening within 2 seconds of going idle. A
  satellite that took longer, such as a display speaking through its avatar, got the
  unknown-voice answer on follow-ups. Voice Assistant Manager now remembers the person per
  conversation: a follow-up on the same satellite within a minute of the previous turn
  (each kept follow-up starts the minute again) keeps them, unless a new voice match says
  otherwise (the other person switches, an unknown voice is refused). Every turn waits the
  voice match until its deadline (see below). A satellite whose "Last wake word" sensor reports a
  different wake word marks a new turn, which always needs its own match. An unchanged
  wake word never reaches Home Assistant, so its absence proves nothing.

## 0.5.0 - 2026-11-23

### Licence

- **Licensed under AGPL-3.0-or-later.** The full text is in [LICENSE](LICENSE) and every
  source file says so in its `SPDX-License-Identifier` line. Every earlier version is also
  made available under AGPL-3.0-or-later by its only copyright holder. Using the integration
  at home changes nothing; sharing a changed version, or offering one over a network, means
  offering its source under the same licence.

### Security

- **A VoIP phone call counts as a device without speaker recognition.** Home Assistant's VoIP
  integration sends every call as its own hidden user, which cannot be chosen for a person, so
  once anyone had a user chosen every call was refused. Now "A device without speaker
  recognition is" decides, as for any device without a helper. The same holds for other system
  users, such as add-ons, and a satellite's helper is never trusted for them.
- **The Voice Manager page warns when no Home Assistant user is chosen for anyone**, because anyone logged in who types to the assistant then counts as the person that "A device without speaker recognition is" picks.
- **An assistant whose saved agents are not in the expected form now refuses with fixed lines** instead of raising an error.
- **A stuck or flooded calendar can no longer stall the announcements.** A calendar that did
  not answer held up every calendar after it, and a burst of events (an import, or catching
  up after an outage) made one agent call and one announcement each. Now a calendar that does
  not answer within 30 seconds counts as failed for that check (the same path as an error:
  one warning, and it is caught up later), and the My assistant page lists it as unreadable.
  At most 5 events are announced per calendar per check; the others that were due are marked
  as announced and stay silent, with one warning naming the calendar and the count. A row
  with a lead time that is not a number, or any other error in one row, is logged and skipped
  and the other rows are still checked.
- **Only administrators and calls without a user (automations, their scripts, calendar
  announcements) can ask for a spoken line.** The
  `compose_announcement` action let any logged-in user have the assistant write a line, which
  uses the agent and so costs money. Now a user who is not an administrator is refused.
  Automations, scripts started by them and calendar announcements run without a user and work
  as before; a non-administrator who runs such a script by hand is refused.
- **A list of facts and the prompt block are bounded.** The memory editors accepted any number
  of lines of any length, and building the memory part of the prompt got slower with the
  square of the number of facts. Now a save carries at most 200 lines of about 250 characters
  each, a list never holds more than 200 facts (the editors say so in plain words), and the
  prompt block is built in one pass with the same result as before.
- **A typed request is judged by who is logged in, never by a satellite's helper.** Home
  Assistant lets any logged-in user, administrator or not, send a typed request (over the
  websocket, the REST API or an Assist pipeline run) that names any device. Since the
  assistant let a satellite's helper win over the user, and the helper keeps the last voice
  it matched, a guest could name the kitchen satellite and be answered as Person 1, with
  Person 1's persona and private facts in the prompt and new facts saved as them. Now a
  request that carries a Home Assistant user goes by the user mapping alone (a chosen
  user is that person; any other user is an unknown voice once a user is chosen; with
  none chosen, "A device without speaker recognition is" decides), and the helper is
  neither read nor waited for. Voice requests from satellites carry no user and are still
  decided by the helper.
- **An assistant whose settings were never saved refuses typed requests.** Such an assistant still
  asks the older "who is speaking" script, which is only given the device, so the same
  trick worked there. A request that carries a Home Assistant user now gets "Sorry, I
  can't help you." and never reaches the main agent or the script. Save the settings (or
  run the setup guide) to use the phone app and the browser with such an assistant. Voice
  requests from satellites keep using the script.
- **An agent that must have no tools is checked on every use, not only when saving.** The
  agent that writes refusals could be another assistant: one of this integration reports no control feature,
  but it answers with its own main agent and its tools, so an unknown voice reached them.
  An agent that gained tools after the settings were saved (Assist turned on for it), or
  that did not exist when they were saved, was not caught either, and the memory agent was
  never checked for tools at all. Now the refusal agent, the memory agent and the
  announcement agent must exist, may not be Home Assistant's own agent, one of the assistants or the
  main agent of any assistant, and may not be able to control the home (an agent whose
  features cannot be read counts as able to). Settings that break this are refused when
  saved; the announcement agent only while reminders are on or calendars are watched.
  Before each use it is checked again: an unsafe refusal agent is never asked and
  unknown voices get the fixed lines, an unsafe memory agent is never asked (remembered
  facts are still used, nothing new is saved), and an unsafe announcement agent leaves
  the plain text. Each shows as an error on the Voice Manager page and in the log. On My
  assistant, a persona that cannot be saved because of the other settings says to ask an
  administrator, without naming what is wrong; the reason is in the log.
- **A request is refused when the assistant cannot work out who is speaking.** A failed
  lookup, an assistant whose settings were never saved and that has no "who is speaking"
  script, and a script that fails or answers with nothing all used to hand the request to
  the main agent without a persona. Home Assistant keeps the previous turn's extra prompt
  when a turn gives none, so in an ongoing conversation that unknown speaker was answered
  with the last speaker's persona and remembered facts. Now the reply is "Sorry, I can't
  help you." and the main agent is never asked. "A device without speaker recognition
  is" still decides for a device without a helper, as before.
- **The main agent's prompt always names the person speaking now.** Its first line is
  "The person speaking now is <name>." ("Person 1" or "Person 2" without a name), followed
  by the persona, memory and rules as before. Even with all of those empty, the prompt is
  never empty, so Home Assistant never reuses the previous speaker's.

### Added

- **Favorites.** "Play Radio 2" plays that person's own Radio 2 at once on the speaker they
  are talking to, "play Radio 2 on the Denon" on another one, and "save this as Radio 2"
  (what is playing now), "save Radio 2 as a favorite" (found with Music Assistant), "save it
  for the household" and "forget Radio 2" keep the list. A favorite is a Music Assistant item or a stream address. Each person's list is shared
  by all assistants, and the household has one. Both can be managed on a new **Favorites**
  section of My assistant and the Voice Manager page, which also chooses a speaker per
  satellite. Switched on per assistant, and the Voice Assistant Manager tools must be
  switched on for the main agent. See [Favorites](docs/favorites.md).
- **A Chat show.** Two assistants talk to each other on a speaker, for fun, each in its
  own character and voice. A new **Chat show** section on the Voice Manager page (for
  administrators) picks the two assistants, a satellite or media player, an optional
  topic and the limits (lines, words per line, seconds between lines, minutes), starts and
  stops the chat, and shows the live transcript and why it ended. The actions
  `voice_assistant_manager.start_chat` and `stop_chat` do the same from a script.
  - New settings under Assistant > Chat show: **Character for chats** and **Voice for
    chats**.
  - Lines are written by each assistant's announcement agent, which has no tools. The main
    agent, memory, reminders and the state of the home are never used.
  - On a satellite, a recognised person can join in by voice, and anyone can stop the chat
    by saying stop. See [Chat show](docs/chat-show.md).

- **A "My assistant" page in the sidebar, for every logged-in user.** For each assistant
  where the user is Person 1 or Person 2 (by that person's Home Assistant user), they can
  read and change their own persona (up to 2000 characters), see the next 30 days of
  their own and the household's reminders, add one through `script.create_reminder` and
  delete one, see and edit their remembered facts (private ones included), and read the
  shared settings. They never see the other person's persona, facts or reminders; who
  the user is comes from their login, checked on the server. The Voice Manager page is
  unchanged and stays for administrators.
  - New settings under Reminders: **Reminder calendars of Person 1**, **of Person 2** and
    **of the household**. A personal calendar may belong to one Home Assistant user only,
    across all assistants, and may not also be a household calendar. A personal list needs
    that person's Home Assistant user.
  - Home Assistant has no per-user access to calendars, so every logged-in user can still
    read all calendars on Home Assistant's own Calendar page. My assistant only shows
    each person their own and the household's.

- **A setup guide** on the Voice Manager page, in seven steps: welcome, who lives here
  (with a ready-made style: Dry butler, Friendly, or Short and plain), satellites, agent
  and wake words, unknown voices, extras and a check. It creates the speaker helper of a
  satellite and, for a satellite with both a `_last_speaker` and a `_last_speaker_score`
  sensor, the tracking automation `voice_assistant_manager_speaker_<device id>`. It switches
  off local commands on the assistant's pipeline, or creates a copy of a wake word's
  pipeline for the assistant and puts it on that wake word. It never changes another
  agent's pipeline, or the preferred pipeline of another agent, and refuses when two
  pipelines share a name. Every change needs a click that names it.
  - It opens for an assistant that was never saved, resumes where it was left, can be skipped
    (the Overview shows **Finish setting up**) and can be run again from the Overview.
- **Overview checks for satellites and pipelines.** The Overview warns about a satellite
  without a helper, speaker sensors that no automation copies into the helper, and a
  pipeline with local commands on. Creating the helper and switching off local commands
  are one button each; the tracking automation opens the guide at its satellites step.
- **A settings page in the sidebar**, "Voice Manager", for administrators. It shows who
  each assistant knows and how each voice is answered, lists what needs attention, and
  holds every setting of the Configure dialog grouped by subject, with the remembered
  facts and the calendars editable in place. It follows the Home Assistant theme and
  works on a phone. A switch on the page takes it out of the sidebar; it stays reachable
  at `/voice-assistant-manager`. The Configure dialog still works, and both save through
  the same rules.
- **Calendar announcements.** An assistant can watch calendars and hand their due events to a
  script of yours that speaks them. Calendars are added under **Configure > Calendars**,
  each with who it is for, a time of day for all-day events (on the day or the day
  before), a lead time for timed events, an "only events containing" filter and a
  description for the agent that writes the line.
  - The assistant asks each calendar with `calendar.get_events` once a minute. For a due
    event it calls the chosen script, and fires `voice_assistant_manager_calendar_due`, with
    `owner`, `title`, `line`, `assistant`, `calendar`, `start`, `end`, `all_day` and
    `kind` (`calendar`).
  - `line` is written by the announcement agent (no tools) in the assistant's own
    language, with the event text as data. It is empty when that fails, so the script
    can fall back to `title`.
  - Each event is announced once, also across restarts. After a restart, events less
    than a configurable number of hours late (default 3) are caught up and later ones
    are dropped.
  - New settings under **Settings > Calendars**: the script that speaks, the default
    time for all-day events (09:00) and the catch-up time.
  - The README explains what the line is written from and where to steer its style.
    "Extra instructions for announcements" (under Reminders) applies to calendar lines
    too, and its description now says so.
- **Reminder support**, for a Home Assistant that keeps reminders in a `script.create_reminder`
  script exposed to Assist. Off by default. With "The main agent can set reminders" on:
  - The main agent is told how to call the tool: `assistant` is this assistant's own assistant
    name, `for_whom` comes from the name in front of the user's message ("Alex: …") for
    "remind me", and is `household` for "remind us".
  - A reply that confirms a reminder when `create_reminder` did not return status `ok` is
    replaced by an honest line, when the assistant can see the turn's tool calls.
  - With memory on as well, the remember/forget rule also says that a request to be
    reminded at a time goes to the tool, and may only be confirmed after status `ok`.
- **`voice_assistant_manager.compose_announcement` action.** Returns one short spoken line for
  a due reminder, for Home Assistant to announce. Fields: `satellite`, `kind` (`reminder`),
  `text`, `recipient` (a person's lower-case name or `household`) and the optional
  `assistant`.
  - The assistant is picked by `assistant` when it is given, and by the satellite's primary
    assistant only when it is empty. No matching assistant returns `{"text": ""}`.
  - The line is written by an agent without tools, never the main agent, from the persona
    text of the recipient and an instruction to deliver the reminder in character, content
    intact, in at most two sentences.
  - It does not go through the assistant's conversation handling: no speaker check, nothing
    added to the history, no memory pass. Any failure, or more than 6 seconds, returns
    `{"text": ""}` so the caller can fall back to plain text.
- A **Reminders** section in the settings: the switch, the assistant name, the
  announcement agent (no tools), extra instructions for announcements, and the line spoken
  when a reminder could not be set.
- A table of contents at the top of the README, and this changelog.

### Changed

- **Renamed to Voice Assistant Manager, with a new domain.** The integration is now
  `voice_assistant_manager`: the folder under `custom_components`, the actions
  (`voice_assistant_manager.compose_announcement`, `start_chat`, `stop_chat`), the event
  `voice_assistant_manager_calendar_due`, the storage files and the page address
  (`/voice-assistant-manager`) all changed with it. The repository is now at
  https://github.com/rodavrow/ha-voice-assistant-manager. Coming from 0.4.0, the assistants set up under the old name
  (Voice Persona Router) are not picked up: add them again, and change the action and
  event names in your own automations and scripts.
  The administrators' page in the sidebar, "Voice personas" before, is now called
  Voice Manager.
- **The agent that writes refusals may not be able to control the home.** Saving settings
  with such an agent (Home Assistant's own agent, or one with an LLM API such as Assist
  turned on) is refused, in the Configure dialog and on the settings page, so an unknown
  voice can never reach tools. It does not apply with fixed lines only. Existing setups
  that use such an agent see the error on their next save.
- **Timed reminders never reach memory**, whether or not reminders are turned on. "Remind
  me/us …", and "remind Sam at 6 to …" or any other request to be reminded at a time, no
  longer start the memory pass, and the memory agent is told never to save a timed
  reminder or its confirmation. "Remind Sam the plumber comes Friday", with no time to be
  alerted at, is still a message that memory keeps.
- "Remember to <do something>" with no time is no longer saved as a memory. With reminders
  on, the assistant asks whether a reminder is wanted, and when. Standing preferences
  ("remember to always …", "remember to never …", "remember to call me …") are still
  memories.

### Fixed

- **A chat line no longer wakes the satellite it plays on into a refusal or a stall.** The
  assistants say each other's names, which are often the wake words, so the satellite
  could wake during a line, hear the rest of it and send it to the assistant as a request:
  it was refused aloud, taken as a stop, or the chat stalled. A request from the chat's
  satellite whose listening began while a line played, or within about 1.5 seconds after
  an announced line, is now answered with silence: it adds no line, never stops the chat
  and never reaches the main agent, memory or history. The next line waits for that
  request, and a line whose announcement fails after the wake still counts as spoken.
  Joining in and stopping by voice work as before between lines; the Stop button
  always works.
- **A chat line no longer stops in the middle of a sentence.** The assistants are now asked
  for a little under the words limit, in complete sentences, and a line that still runs
  over is cut back to its last complete sentence (or clause) instead of after the last
  word that fits.

- **The lists shared by all assistants no longer mix two people.** Those lists belong to
  Person 1 and Person 2, so when Person 1 was Alex in one assistant and Sam in another, both
  assistants read and added to the same list. A role's shared list is now used only while it
  is the same person (same name and, where set, same Home Assistant user) in every assistant
  that has the shared lists switched on. Otherwise those assistants leave it out of the
  prompt, a request for all assistants is saved to the assistant's own list, My assistant
  does not offer it, and the Overview warns. Nothing is moved: the lists stay in the
  memory editor so an administrator can sort them out.

- **A reply that only mentions reminders is no longer replaced by "I could not set that
  reminder".** Asked "What can you do in a house?", an assistant that answered "If you want
  a reminder set, … I handle it better than anyone" got its reply swapped for the failure
  line. The check for false confirmations now only runs when the conversation is about a
  reminder: the person's message, or one of their two messages before it, asks for one or
  names a time to be alerted at. Sentences about what could happen ("if you want…",
  "I can…", "you could…") no longer count as a claim that a reminder was set.

### Documentation

- The README is now a short front page: what the integration does, a diagram of how a
  request is answered, setup, the settings page with screenshots, and a glossary. The
  details moved to `docs/`: settings (one table per section), memory, reminders,
  calendar announcements, more assistants than wake-word slots, limits and
  troubleshooting. Running the tests moved to `CONTRIBUTING.md`.
- Diagrams of how a reminder travels, how a calendar event is announced, and how wake
  words, pipelines and assistants fit together.
- Two new examples: `examples/create_reminder.yaml`, a `create_reminder` script that works
  out the date from `in_minutes`, `at_time` and `on_day`, and
  `examples/calendar_announce.yaml`, a script that speaks calendar announcements. Both are
  covered by tests.

### Home Assistant side (not part of this integration)

Reminder support expects, and does not create:

- `script.create_reminder`, exposed to Assist. The assistant depends on its fields `for_whom`
  (a person's lower-case name or `household`) and `assistant` (the assistant name of the
  assistant that took the reminder), and on the `status` it returns: `ok` or `error`, with a
  `message` on an error that says what to tell the person. How the time is passed is up to
  the script. A single date-and-time field did not work in practice, because the main
  agent does not know today's date. Fields such as `in_minutes`, `at_time` (`HH:MM`) and
  `on_day` do, with the script working out the date.
- A script that speaks due reminders with `assist_satellite.announce`, in the voice of the
  assistant that took the reminder, for example `script.reminder_announce`. When a switch
  such as `input_boolean.reminders_persona_phrasing` is on, it first calls
  `voice_assistant_manager.compose_announcement` with that `assistant` and falls back to
  plain text if that fails or returns empty text.

## 0.4.0 - 2026-11-21

### Added

- **Memory.** An assistant can remember what the two people who live here tell it. Shared
  between the two by default, private on request, per assistant by default or for all
  assistants when asked. Saved after the reply by a separate agent without tools. Editable
  under Configure > Memory. Never read or saved for unknown voices, and passwords, PINs
  and codes are filtered out.
- **Home Assistant user mapping.** Requests from the phone app, the browser and other
  devices without a speaker helper can be matched to the logged-in Home Assistant user.

### Changed

- **Configure opens a menu** with Settings and Memory. Existing settings are kept.
- **A satellite whose helper holds an unexpected value is refused.** An empty helper, or
  any value other than `owner`, `partner`, `stranger` or `pending`, counts as an unknown
  voice. Before, it was treated as Person 1.

## 0.3.0 - 2026-11-20

First public release: a persona per recognised speaker, refusals for unknown voices that
never reach the main agent or its tools, an optional "you sound familiar" reply for near
misses, built-in commands for recognised speakers only, and settings in the UI.
