# Changelog

All notable changes to Voice Assistant Manager. Versions follow the `version` in
`custom_components/voice_assistant_manager/manifest.json`.

## Unreleased

### Added

- **Satellites that can show which assistant is talking now do.** On a satellite whose
  firmware offers a select ending in `_next_announcement_assistant`, each Chat show line and each line from `compose_announcement` shows the assistant that speaks it.
  The new `prepare_announcement` action does the same for any other announcement, such as
  calendar events. Other speakers are never touched. Switch it off with **Show which
  assistant is speaking** on the Voice Manager page.

### Changed

- **The favorites switch is now called "Use favorites".** It was "Play favorites by name",
  which read as if it only covered the quick "play …" requests. It switches the whole
  feature on for an assistant, and its description now says so. Nothing else changes.

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
