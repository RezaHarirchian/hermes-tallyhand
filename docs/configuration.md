# Configuration

This page lists every setting Tallyhand reads and the Hermes settings that change how the bot behaves in chat. For setup, start with the [README](../README.md#quick-start).

There are two places to put things:

- **Environment variables** go in the profile's `.env`. The setup wizard (`hermes gateway setup`) writes the main ones. You can also use `hermes config set NAME value`, with `-p <profile>` for a named profile.
- **Plugin settings** go under `plugins.entries.tallyhand.settings` in the profile's `config.yaml`. `tools_from_other_platforms` applies at once. The others apply when the gateway restarts. A setting missing there is also read from `platforms.twitch` or `gateway.platforms.twitch` in the same file, where Hermes keeps each platform's own settings; the plugin settings win.

A true/false setting accepts `true`, `false`, `yes`, `no`, `on`, `off`, `1` or `0`. A number setting accepts a whole number, written as `20` or `20.0`. Anything else is logged as a warning and the default is used.

## Environment variables

### Twitch

- **`TWITCH_BOT_TOKEN`** (required). The bot account's user access token, with or without the `oauth:` prefix. It needs `chat:read` and `chat:edit`; without them the bot does not connect. The other scopes are listed in [Token scopes](twitch-tools.md#token-scopes).
- **`TWITCH_CHANNEL`** (required). The channels to join, comma-separated, your own first. The broadcaster of the first channel is the owner.
- **`TWITCH_ALLOWED_USERS`** (empty). Who may ask the bot: Twitch user ids or logins, comma-separated or as a JSON list. See [Who may ask](#who-may-ask).
- **`TWITCH_ALLOW_ALL_USERS`** (`false`). `true` lets everyone in chat ask.
- **`TWITCH_HOME_CHANNEL`** (unset). Where cron jobs sent to plain `twitch` go, and Hermes' messages to you. Make it one of your `TWITCH_CHANNEL` channels. Hermes also posts its restart and shutdown notices there (see [Hermes notices](#hermes-notices)). Unset, those stay out of chat. A value that is not a channel login is ignored with a warning.
- **`TWITCH_HOME_CHANNEL_NAME`** (`Home`). The name Hermes shows for the home channel.
- **`TALLYHAND_TRIGGERS`** (empty). Extra trigger prefixes besides `!hermes` and `!h`, comma-separated, for example `!ask,!q`.
- **`TALLYHAND_USER_COOLDOWN`** (unset). Seconds between two questions from one viewer. It wins over the `user_cooldown` setting.
- **`TALLYHAND_REPLY_LANGUAGE`** (unset). Wins over `reply_language`: `channel`, `viewer`, or a two-letter ISO 639-1 code.

### OBS

These are explained in [docs/obs.md](obs.md).

- **`OBS_WEBSOCKET_PASSWORD`** (unset). The password from OBS's WebSocket server settings.
- **`OBS_WEBSOCKET_URL`** (`ws://127.0.0.1:4455`). Set it only if OBS listens elsewhere, or if you turned authentication off in OBS.
- **`TALLYHAND_OBS_ALLOW_REMOTE`** (`false`). `true` lets the URL point at another computer.
- **`TALLYHAND_BRB_SCENE`** (unset). The scene `!brb` switches to.
- **`TALLYHAND_LIVE_SCENE`** (unset). The scene `!back` switches to. Unset, `!back` returns to the scene `!brb` left.
- **`TALLYHAND_CAPTION_SOURCE`** (`tallyhand_caption`). The OBS Text source that captions are written to.

## Plugin settings

- **`user_cooldown`** (`20`). Seconds between two questions from one viewer in a channel. `0` turns the wait off. The channel's broadcaster and moderators never wait. A question inside the wait gets no answer and is not queued; the viewer has to ask again later.
- **`max_reply_lines`** (`3`, from `1` to `20`, Twitch's limit for a bot in 30 seconds). The most chat messages one answer may take. A longer answer is cut and ends with "...".
- **`chat_tail`** (`0`). How many recent chat lines, not addressed to the bot, go along with each question. See [Chat context](#chat-context) before you turn it on.
- **`chat_commands`** (`true`). The [chat commands](#chat-commands) that need no model call. Off, only `!privacy`, `!remember`, `!mynotes` and `!forgetme` stay. Turn it off when your channel already runs another command bot.
- **`tools_from_other_platforms`** (`false`). Lets turns from other messaging platforms, other agents over A2A, hosted group chats and cron jobs use the Twitch and OBS tools. A value that is not true or false is logged once and counts as false. See [Outside Twitch chat](twitch-tools.md#outside-twitch-chat).
- **`viewer_notes`** (`true`). Viewers can store notes with `!remember`. Off, nothing new is stored and notes stay out of questions, but notes from before can still be listed and deleted until they expire. See [PRIVACY.md](../PRIVACY.md#viewer-notes).
- **`viewer_notes_days`** (`90`, at least `1`). Days a viewer note is kept.
- **`event_messages`** (`off`). `thanks` posts a thank-you line for raids and subs. See [Raids and subs](#raids-and-subs).
- **`min_raid_size`** (`1`). The smallest raid, in viewers, that gets a thank-you line.
- **`auto_clip`** (`false`). Clips your stream on two fixed rules. See [Automatic clips](#automatic-clips).
- **`auto_clip_announce`** (`true`). Posts the link to an automatic clip in chat.
- **`caption_answers`** (`false`). Also shows the bot's answers in your channel as an OBS caption. See [Captions](obs.md#captions).
- **`reply_language`** (`channel`). Language for every model reply in chat. `channel` uses the stream language Twitch reports for that channel (refreshed in the background about every two minutes). `viewer` answers in the language of each question. A two-letter ISO 639-1 code fixes one language for everyone. When the stream language is missing, `other`, or the lookup failed, the bot falls back to the question's language. Chat commands and event thank-you lines stay English in this version.
- **Stream language markers.** The title and tags can name the language. An explicit code (`[FA]`, `(FA)`, `#FA`, `lang:FA`), a native name or a non-English alias beats the language Twitch reports. A plain English name (Spanish, Dutch, Hindi, and also English, Tagalog and Afrikaans, whose native names are the same word) decides only when Twitch reports no real language, so ordinary English titles keep the reported language. Naming two languages, of either kind, falls back to the reported language, then to the question's language. Twitch's language list has no Persian. A Persian channel can set its Twitch language to Other and put the word Persian in the title or a tag, put `[FA]` or the native name in the title whatever language Twitch reports, or set `reply_language` to `fa`. A changed title or tag takes up to two minutes to be noticed.

## Who may ask

Without `TWITCH_ALLOWED_USERS` or `TWITCH_ALLOW_ALL_USERS=true` the bot answers nobody, you included. The wizard offers three choices (only you, you and a list, or everyone) and saves user ids.

A user id needs no lookup and keeps working when its owner changes their login. Logins work too. Tallyhand looks them up with Twitch when it connects and logs once about a login Twitch does not know. If Twitch does not answer, it tries again every minute, and until then the login lets nobody in. A login you add counts after the next restart. One you remove stops counting at once.

Hermes' gateway-wide settings count too. User ids in `GATEWAY_ALLOWED_USERS` may ask. With no allowlist set anywhere, `GATEWAY_ALLOW_ALL_USERS=true` lets everyone in chat ask; it does not open Twitch while `TWITCH_ALLOWED_USERS` is set.

A question from someone who may not ask is dropped quietly: no answer, no turn, no cooldown, and no log line naming them. The chat commands are different. They answer anyone in chat, except `!remember`, which only takes notes from viewers who may ask.

## How the bot answers

### Triggers

The bot takes a line as a question when it starts with `!hermes`, `!h`, an @mention of the bot or one of your extra triggers. Case does not matter. The trigger must be followed by a space, a comma, a colon or nothing. A Twitch reply to one of the bot's last 50 messages is a question too, trigger or not.

A trigger with nothing after it gets a one-line hint on how to ask, at most once a minute for each viewer, and only for viewers who may ask. Every other line is kept from the agent, unless you turn on `chat_tail`.

When someone uses Twitch's reply button, the replied-to text and its author go along with the question only if that message was the bot's own or the asker's own. For anyone else's message only its id goes along.

### Replies

An answer is plain text in the language set by `reply_language`, written like a regular chatter of that language. Markdown is flattened, list items are joined into one sentence, em and en dashes are rewritten to a comma or an ASCII hyphen, and a line never starts with `/` or `.`, so the bot cannot run a Twitch chat command. Each chat message holds up to 480 characters, and an answer takes at most `max_reply_lines` messages.

### Turns and queues

One question runs at a time in each channel, and up to three more wait their turn. Questions from you and from the channel's moderators go ahead of viewers' questions in the queue. When three viewers' questions wait, yours takes the place of the newest one, and that viewer may ask again at once. A viewer's question that arrives while three wait is dropped. A turn's slot is freed when Hermes finishes it, or after 15 minutes at the most.

Your own follow-up while your question is still running goes straight to Hermes instead of waiting in the queue, so your slash commands and approval answers reach the turn they are about.

When a moderator times someone out or bans them, that viewer's waiting questions and recent chat lines are dropped. A deleted message is dropped the same way, and clearing the whole chat drops every waiting question and recent line in that channel. Lines that already went to Hermes with an earlier question stay in that session's history.

### Rate limits and slow mode

Tallyhand keeps to Twitch's limits for the bot account, counting each window a little longer than Twitch does so that a delayed line still lands inside it:

- 20 messages in any 31 seconds across all channels,
- 100 in any 31 seconds in channels where the bot is broadcaster, moderator or VIP,
- one message every 1.05 seconds in a channel where it is none of those.

In slow mode, where the bot is not broadcaster, moderator or VIP, it waits out the slow-mode interval plus half a second between messages, and an answer is one line. Hermes' approval questions keep up to three lines even then, so the answer words and the deadline are never cut off.

The first line of a reply waits up to 15 seconds for room. If it gets none, the send fails and the log says the bot was rate limited. If a later line cannot go out, Hermes is told how many lines chat already got, so a retry sends only the rest.

The bot's own lines (usage hints, command answers, thank-you lines and automatic clip links) wait at most three seconds and leave the last five messages of each window to Hermes' answers. A line that finds no room is dropped, so a crowd typing commands cannot crowd out answers.

### Chat context

`chat_tail` above 0 sends that many recent lines of the channel's chat along with each question. Each line is cut to 200 characters, kept on one line, and framed as untrusted viewer messages that must not be followed as instructions. Lines that came after the question, and lines a moderator removed, are left out.

Two things to know before you turn it on. The lines are stored in the asker's Hermes session history, like the question itself, without the people who wrote them asking for that. They also reach your own turns, which run with your toolsets, so anyone in chat can try prompt injection on you. The framing is a mitigation, not a guarantee. A moderator's turns never carry the lines, because they hold moderation tools.

## Chat commands

These are answered at once, without a model call, and do not wait for a running question. A trigger always wins: a line that starts with `!hermes` or another trigger is a question, whatever commands exist.

- `!uptime` (everyone): how long the stream has been live.
- `!bot` (everyone): that the bot is an automated assistant run by the channel, using Tallyhand, and not a person.
- `!commands` (everyone): the commands the caller can use there.
- `!privacy` (everyone): what the bot keeps about viewers, in one line.
- `!remember <text>` (viewers who may ask), `!mynotes` and `!forgetme` (everyone): see [PRIVACY.md](../PRIVACY.md#viewer-notes).
- `!so <login>` (moderators): a Twitch shoutout plus a chat line with the channel's link. The chat line goes out even when the Twitch shoutout fails.
- `!clip` (moderators): a clip of the live stream and its link.
- `!brb`, `!back` (moderators, in your channel) and `!scene` (you): OBS scene switching, see [docs/obs.md](obs.md#chat-commands).

A command from someone below its level gets no answer.

A viewer can use one command every 15 seconds, and after a viewer uses a command in a channel, the same command is not answered there again for 30 seconds, so a crowd typing `!uptime` gets one answer. `!remember` and `!mynotes` wait only on the same viewer's last use of the same command. `!forgetme` always runs, one at a time for each viewer, but answers that viewer only once in 15 seconds, so repeating it deletes again without making the bot repeat itself. The broadcaster and moderators of the channel are not limited.

## Raids and subs

With `event_messages: thanks`, the bot posts one fixed line when someone raids, subscribes, resubscribes or gifts subs, without a model call:

- "Welcome, raiders! Thanks for the raid, {name}."
- "Thanks for subscribing, {name}!"
- "Thanks for resubscribing, {name}!"
- "Thanks for gifting, {name}!"

`{name}` is the person's display name, cut to 25 characters. A community gift gets one line, not one per recipient, and a gift from someone who hid their name is thanked as "anonymous gifter". A raid smaller than `min_raid_size` gets none. After a line, the same kind of event gets no line in that channel for 30 seconds, so a sub train gets one line and not a flood. This works in every channel the bot joins. Nothing about the event is stored.

## Automatic clips

With `auto_clip: true`, Tallyhand clips your stream by two fixed rules, without a model call:

- chat over the last 20 seconds has at least 15 messages and runs at least three times its usual pace, measured over the five minutes before, or
- three different people type the word "clip" within 15 seconds. A line that starts with `!`, such as `!clip`, or that holds a link does not count.

It works only in your own channel (the first in `TWITCH_CHANNEL`) and only while you are live. Once the bot finds you live, both rules rest for five minutes, even if Twitch then fails to make the clip. If you were offline, or Twitch did not answer the check, they rest for one minute. The pace rule starts five minutes and 20 seconds after the first chat line the bot sees.

It needs the `clips:edit` scope. Without it, Tallyhand logs one warning and makes no clips. With `auto_clip_announce` on, the bot posts "Clip:" and the link in your chat.

## Cron jobs and `hermes send`

`hermes cron create --deliver twitch ...` posts in the home channel, `TWITCH_HOME_CHANNEL`. With `--deliver twitch:<channel>` it goes to that channel, which must be one of `TWITCH_CHANNEL`'s or the home channel. `hermes send --to twitch:<channel>` follows the same rule. Tallyhand refuses every other channel, and the running bot never posts in a channel it does not serve. With `TWITCH_HOME_CHANNEL` set, `--deliver all` includes Twitch too.

Hermes puts a "Cronjob Response" header on cron output unless you set `cron.wrap_response: false`. Tallyhand cuts the output to `max_reply_lines` lines like an answer. Cron output is never shown as a caption.

When the gateway runs with Twitch connected, the running bot posts it. In a process without it (`hermes send`, or a script job run by `hermes cron tick`), Tallyhand opens a connection of its own: it checks the token, logs in over TLS, joins the channel, posts one line a second (and one line in slow mode) unless the bot is broadcaster, moderator or VIP there, then leaves. It posts at most 20 lines, and the bot token is blanked out of them. All of that must finish within 45 seconds.

When a cron job fails, Hermes posts its notice where the job delivers. To keep those notices out of chat, set `display.platforms.twitch.suppress_warning_notifications: true` or give the job `--failure-deliver local`.

## Hermes notices

Tallyhand registers Twitch with Hermes' low display tier. That turns off streaming, mid-turn updates and the "still working" notice for Twitch, and keeps busy notices short. Hermes never posts tool progress to Twitch, which cannot edit messages.

Your own `display` settings win over the tier, global ones included. If you set these under `display:` for every platform, set them again for Twitch:

```yaml
display:
  platforms:
    twitch:
      interim_assistant_messages: false
      long_running_notifications: false
```

`display.busy_ack_enabled: false`, or `HERMES_GATEWAY_BUSY_ACK_ENABLED=false`, turns the busy notice off for every platform. The config setting wins over the variable.

Hermes' setup and service notices do not go to Twitch chat: the one-time "No home channel is set" prompt, failed subagent notices, credit and usage notices and `/login` steps. Tallyhand always sets Hermes' `notice_delivery` to private for Twitch, logs a warning if you set something else, and writes those notices to the gateway log, since Twitch chat has no private place for them. To set a home channel, use `TWITCH_HOME_CHANNEL`, or type `!hermes /sethome` in the channel yourself. A bare `/sethome` does not work, because Twitch keeps a leading `/` for its own commands.

Once a home channel is set, Hermes posts its "gateway online" and shutdown notices there, where viewers read them. To stop that, add `gateway_restart_notification: false` under `gateway.platforms.twitch` with `hermes config edit`.

When a reply fails part of the way through, Hermes may send the whole reply again later with a "Recovered reply" note at the top. Tallyhand leaves the note out and skips the lines chat already got in the last five minutes. After a gateway restart it cannot tell what chat got, so that reply goes out whole, with Hermes' note.

## Approvals

Bans, unbans and timeouts over ten minutes go through Hermes' approval first. How to answer in chat is in [docs/twitch-tools.md](twitch-tools.md#approval-for-bans). These Hermes settings change it:

- `approvals.timeout` (300 seconds): how long Hermes waits for your answer.
- `approvals.mode: off` and yolo mode (`--yolo`, or `!hermes /yolo` from you) skip the question.
- `approvals.cron_mode` and `approvals.single_query_mode` default to deny, so cron jobs and `hermes chat -q` runs cannot ban. Setting either to `approve` skips the question there.
- `approvals.unattended_mode: approve` skips it for API server and webhook turns, which reach the Twitch tools only with `tools_from_other_platforms`.

## Hermes settings that matter for Twitch

When it connects, Tallyhand checks memory, the user profile, the memory provider, context files, `group_sessions_per_user`, the hint and the toolset keys below, and logs a warning for each one that is risky, with the command that changes it. After you change them, run `hermes sessions refresh-prompts` (with `-p <profile>`): a session that already exists keeps its old system prompt, even across a restart.

- **`memory.memory_enabled`** and **`memory.user_profile_enabled`**: on, every Twitch turn, viewers' included, carries your `MEMORY.md` and `USER.md`. Turn both off in the bot's profile.
- **`memory.provider`**: an external memory provider gets viewers' turns and puts its recall into their answers. `hermes memory off` clears it.
- **`gateway.platforms.twitch.skip_context_files`**: `true` keeps your project's context files (`AGENTS.md`, `.hermes.md` and the like) out of Twitch turns. Set it with the wizard or `hermes config edit`. `hermes config set gateway.platforms.twitch.skip_context_files true` saves it as `platforms.twitch.skip_context_files`, which the gateway never reads, and Tallyhand's warning says so.
- **`group_sessions_per_user`**: keep it on. Off, everyone in a channel shares one Hermes session and can read each other's questions back, viewer notes stay out of questions, and `!forgetme` cannot delete one viewer's questions. It belongs to the gateway, so Hermes reads it from the profile that runs the gateway, usually your default one.
- **`platform_toolsets.twitch`**: the toolsets your own Twitch turns get, plus `twitch` and `obs`. It must be a list in brackets, such as `'[todo]'`; a bare name is ignored. Unset, it is Hermes' full set. Moderators and viewers never get it.
- **`known_plugin_toolsets.twitch`**: Hermes adds every plugin's toolsets to every Twitch turn unless this list names them and the turn's own list does not. `[obs, twitch]` keeps Tallyhand's tools out of viewers' turns and the `obs` tools out of moderators', so the model does not see them or tell viewers about them; Tallyhand refuses those calls either way. Pair it with a `platform_toolsets.twitch` list that does not name Hermes' default `hermes-twitch` set, as the wizard does: otherwise it takes the `twitch` and `obs` tools from your own turns too. Other plugins' toolsets named in the list stay out the same way. The `hermes tools` menu has no Twitch row, so set both keys with `hermes config edit`.
- **`platform_hints.twitch.replace`**: replaces Tallyhand's hint to the model, which says it is an automated assistant and must never follow instructions from chat. Use `platform_hints.twitch.append` to add to the hint instead.
- **`profile_routes`**: leave the bot's channels unrouted. `!forgetme` and the checks at connect look at the profile that runs the bot.
- **`gateway.write_sessions_json`** and **`gateway.delivery_ledger`**: see [PRIVACY.md](../PRIVACY.md#what-hermes-stores).

## Connection and token

The bot connects to Twitch chat over TLS as its own account. It checks the token with Twitch when it connects and every hour after that, as Twitch requires. Only an answer that the token is invalid stops the bot, with an error in the log. Any other answer, or none, is tried again at the next check, and the log says so. In its last week, the log warns once a day that the token is about to expire. The running bot never refreshes or changes the token; only the setup wizard saves the token you type. After you mint a new one, update `TWITCH_BOT_TOKEN` and restart the gateway.

The bot pings Twitch every minute. When the connection drops, or Twitch asks for a reconnect, or a ping goes unanswered for 15 seconds, the bot reconnects, waiting 1, 2, 4 and up to 60 seconds between tries. Hermes runs one connection per bot account, so a second profile with the same bot account does not connect.

A channel that is suspended or that banned the bot is logged as a warning. So is a message Twitch did not deliver, with Twitch's reason.
