# Privacy

This page says what Tallyhand and Hermes keep about your viewers, where it goes and how to delete it. You run the bot, so you are the one viewers will ask about their data.

## What Tallyhand stores

### Viewer notes

Tallyhand writes one thing about viewers to disk: the notes they ask it to keep.

- A viewer stores a note with `!remember`, for example `!remember I play on PC` or `!remember call me Kat`. Nothing is stored for a viewer who never does.
- A note holds its text, the viewer's Twitch user id, the channel's id and the time. It is one line of at most 200 characters. Each viewer keeps at most 5 notes per channel; a sixth drops the oldest, and the bot says so.
- When that viewer asks the bot a question, their own notes go along with it, framed as the viewer's own words and not as instructions. No other viewer's notes ever do.
- A note expires after `viewer_notes_days` days, 90 by default, and never goes along with a question after that. Expired notes are purged when the gateway starts and once a day, only while the bot is connected. A copy that went along with an earlier question stays in Hermes' session history (see below) until `!forgetme` or `hermes sessions delete`; the bot says so when it takes a note.
- `!mynotes` lists a viewer's notes in that channel. `!forgetme` deletes them at once.

The notes live in one SQLite file, `viewer_notes.db`, in Hermes' data folder for the plugin: `<Hermes home>/plugin-data/tallyhand/` of the profile that runs the bot. For the profile in Recommended setup that is `~/.hermes/profiles/tallyhand/plugin-data/tallyhand/`. Only your account can open the folder and read the file, and a deleted note is overwritten, not left behind in it. You can delete the file whenever you like. Removing the plugin does not delete it, and nothing purges it while the bot is not running, so if you stop using Tallyhand, delete the file yourself.

With `viewer_notes: false`, nothing new is stored and notes stay out of questions. Notes stored before can still be listed with `!mynotes` and deleted with `!forgetme` until they expire. While Hermes' `group_sessions_per_user` is off, `!remember` does not exist and notes stay out of questions, since every viewer's turn would read them in the shared session.

### Kept in memory only

These are never written to disk by Tallyhand and are gone when the gateway stops:

- viewers' roles and cooldowns,
- the recent-chat lines, while `chat_tail` is on,
- up to three waiting questions per channel, with the asker's login,
- chat message ids for ten minutes, to skip duplicates,
- Twitch account lookups, at most 2000, none used again after an hour, and the user ids that the logins in `TWITCH_ALLOWED_USERS` resolve to, kept while the bot runs,
- each joined channel's stream language code, title and tags from Twitch, refreshed about every two minutes,
- the bot's own lines, at most 200 per channel, none used after five minutes, to spot a reply Hermes sends again,
- with `auto_clip` on, the times of your channel's chat lines from the five minutes and 20 seconds before its latest line, and who typed "clip" in the 15 seconds before it.

### Logs

Tallyhand never logs chat lines, the bot's own included. With debug logging on, the log names viewers who asked the bot something or used a command. Hermes' own notices, which never go to chat, are written to the gateway log.

## What Hermes stores

Hermes keeps its own records, and Tallyhand cannot change how.

- Every question the bot answers is kept with its answer in the bot profile's Hermes session history, in its `state.db`. Each viewer has their own session in each channel while `group_sessions_per_user` is on, which is the default. The viewer's notes and the recent chat lines that went along with a question are stored as part of it.
- The gateway's routing index also holds the viewer's Twitch user id and display name. It lives with the gateway: in Recommended setup, your default profile's `state.db` and `sessions/sessions.json`. `gateway.write_sessions_json: false` in that profile stops the JSON copy; the `state.db` copy stays.
- Hermes keeps sessions until they are deleted. It does not end a chat session after a time, only on a reset, and `sessions.auto_prune` only removes sessions that have ended. `hermes -p tallyhand sessions list --source twitch` shows them, and `hermes -p tallyhand sessions delete <id>` deletes one.
- With an external memory provider, viewers' questions are also sent to it. `!forgetme` cannot reach that copy, and its answer says so.
- When you or a moderator has the bot time someone out or ban them, who and why stays in the session history of whoever asked. An "always" answer to a ban approval is also saved in the bot profile's `config.yaml`, under `command_allowlist`, as `plugin_rule:twitch_ban:<channel>:<login>`. Delete that line to take it back.

## What leaves your computer

Tallyhand itself contacts only Twitch, and OBS if you set it up:

- `irc.chat.twitch.tv` for chat, over TLS,
- `id.twitch.tv` to check the token when the bot connects and every hour after that,
- `api.twitch.tv` for the Twitch tools, the chat commands, automatic clips, looking up logins, and reading each joined channel's public stream language, title and tags. Only the matched language name is used; nothing about viewers is stored for that lookup.

OBS runs on the same computer unless you allow another one with `TALLYHAND_OBS_ALLOW_REMOTE`. Tallyhand sends OBS nothing about viewers on its own. With `caption_answers` on, the bot's answers in your channel show on stream, and they often name the viewer.

Hermes sends each turn to the model provider you configured: the question, the asker's display name, their notes, any recent chat lines that go along, and the system prompt Hermes builds. That prompt includes your `MEMORY.md` and `USER.md` unless you turn them off, and your `SOUL.md` always, which is why the README recommends a profile of its own for the bot. Tallyhand has no telemetry and contacts nothing else.

## Viewers' controls

- **`!privacy`** tells a viewer what applies, in one line. With the default settings it says: "I am an automated assistant and take notes only with !remember. Your questions go to the streamer's AI model and stay in their Hermes history. !forgetme deletes your notes and our conversation in this channel." The line changes to match your settings, for example when `chat_tail` is on or an external memory provider is set.
- **`!remember <text>`** stores a note. It only works for viewers who may ask the bot.
- **`!mynotes`** lists the viewer's notes in that channel.
- **`!forgetme`** deletes the viewer's notes, their own Hermes sessions in that channel and their lines in the recent-chat memory, then says what it deleted. When it deleted a conversation, it adds that logs keep short traces for a while. A session still answering one of their questions stays, and the bot asks them to send `!forgetme` again afterwards. Sending it again deletes again, but the bot answers once every 15 seconds.

`!privacy`, `!mynotes` and `!forgetme` answer anyone in chat, whatever `TWITCH_ALLOWED_USERS` says. All four stay on with `chat_commands: false`, and `!forgetme` always deletes, whatever the cooldown.

`!forgetme` works through Hermes' session database, and some traces stay:

- Hermes' log lines: the viewer's display name, and the first 80 characters of each question and of the message it replies to,
- the viewer's entry in the gateway's routing index,
- a copy of each answer in Hermes' delivery record, in the `state.db` of the profile that runs the gateway. Hermes deletes a copy once it is seven days old, the next time it records a reply. `gateway.delivery_ledger: false` there turns the record off.
- the last answer of each deleted conversation, which Hermes keeps as a cached result in `state.db` until seven days after the deletion, and the words of deleted messages in its search index until the index merges,
- the deleted rows, in free space of `state.db`, until `hermes -p tallyhand sessions optimize` compacts it,
- recent chat lines of that viewer that went along with someone else's question, in that person's session.

With `group_sessions_per_user: false`, everyone in a channel shares one session, and `!forgetme` cannot take one viewer's part out of it. Tallyhand then warns about the setting when it connects, and `!forgetme` tells the viewer to ask you.

`!forgetme` and the check at connect look at the profile that runs the bot. If the gateway's `profile_routes` send one of the bot's channels to another profile, they miss that profile, so leave the bot's channels unrouted.

## Your responsibilities

- **Tell your viewers a bot is in chat.** Say so on your channel page or in a pinned message, along with what it does with their questions. The bot answers `!bot` with "I am an automated assistant run by <your channel>, using Tallyhand. I am not a person. Ask me something with !hermes followed by your question." With `chat_commands: false`, `!bot` is off and `!privacy` is the only command that still says it is an automated assistant, so your channel page has to say it. Tallyhand tells the model never to claim to be human; a `SOUL.md`, a personality or `platform_hints.twitch.replace` that says otherwise can override that, and Tallyhand warns at connect about a replaced hint.
- **Follow Twitch's rules.** That means Twitch's Terms of Service and Community Guidelines for the bot account and what it posts, and the developer agreement that covers the application you minted the token with.
- **Answer deletion requests.** A viewer can use `!forgetme` themselves. A viewer who is banned, or who asks you by message, cannot, so do it for them. Find their sessions with `hermes -p tallyhand sessions list --source twitch` (a session's key ends in the channel and the viewer's Twitch user id) and delete each with `hermes -p tallyhand sessions delete <id>`. Delete their notes in `viewer_notes.db` with `sqlite3 viewer_notes.db "PRAGMA secure_delete = ON; DELETE FROM notes WHERE user_id = '<id>';"`, or remove the file.
- **Keep bystanders out unless you need them.** `chat_tail` is off by default. Turning it on stores other viewers' lines in the asker's session history, without those viewers asking. Neither their `!forgetme` nor a later moderator deletion removes lines Hermes already has.
