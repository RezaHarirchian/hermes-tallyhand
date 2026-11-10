# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's private vulnerability reporting on the public repository: open [RezaHarirchian/hermes-tallyhand](https://github.com/RezaHarirchian/hermes-tallyhand), go to the Security tab and choose "Report a vulnerability". The direct link is [github.com/RezaHarirchian/hermes-tallyhand/security/advisories/new](https://github.com/RezaHarirchian/hermes-tallyhand/security/advisories/new).

Say which Tallyhand and Hermes Agent versions you used, what you did, and what happened. Please do not open a public issue for a vulnerability, and do not test against a live channel you do not own. The latest release is the one that gets fixes.

## Threat model

This is what Tallyhand assumes about the people around the bot, and what it does about each risk.

### Viewers are untrusted

Anyone in chat can type anything. The bot takes only questions addressed to it, from people you allowed to ask; chat commands such as `!uptime` answer anyone, and moderators can use `!so`, `!clip`, `!brb` and `!back` whether or not they may ask. Every question costs you a model call, so by default each viewer waits 20 seconds between questions, and each channel runs one question with at most three waiting. Yours and your moderators' go ahead of viewers' in that queue, so a flood of viewers cannot lock you out. The bot's own lines (hints, command answers) leave the last messages of Twitch's rate limit free for answers.

A question goes to the agent as the viewer's own message. The recent chat lines and a viewer's notes sit inside a marked block that says not to follow instructions in it. Viewer text cannot close or fake that block, because square brackets in it are replaced. Tallyhand also breaks Hermes' `@file:`, `@url:` and similar references in chat text, so a viewer cannot make Hermes read a file or fetch a page. A moderator's turns never carry the recent chat lines, since they hold moderation tools. These measures lower the risk of prompt injection. They do not remove it: a viewer can still try to make the bot say something you would not.

Replies are plain text, a few lines long, and never start with `/` or `.`, so the bot cannot be talked into running a Twitch chat command. Its chat replies go only to the channels it serves, and so does everything the Twitch tools change, in your turns too: a call for another channel is refused before any approval question. Only reading stream info and the operator at their own keyboard reach other channels.

### The sandbox

Each Twitch turn gets toolsets by the asker's role: a to-do list for a viewer, the moderator Twitch tools on top for a moderator, and your own toolsets plus the Twitch and OBS tools for you. Hermes adds some tools to every turn that a plugin cannot take away, so Tallyhand's hook checks every tool call in a Twitch turn before it runs and refuses anything the asker's role may not use, and each Twitch and OBS tool checks its caller again before it acts. The hook fails closed, since guessing could let a viewer's turn through: if its own check fails in a Twitch turn, it blocks the call, and if it cannot read the session at all, it blocks every Twitch tool and, in the process that runs the Twitch adapter, every tool call, whatever platform the turn came from.

Roles come from Twitch's chat badges. A moderator counts as one in the channels where Twitch shows the badge, and only there.

With `group_sessions_per_user: false`, everyone in a channel shares one Hermes session, so a viewer's question that reaches Hermes while your turn or a moderator's is running can be folded into that turn, which carries that person's tools: Hermes redirects the turn with it in the default `display.busy_input_mode`, `interrupt`, and steers it in with `steer`. Keep `group_sessions_per_user` at its default, `true`, on a channel with viewers; Tallyhand warns at connect while it is off.

### Owner-only tools

Only you can use the OBS tools and `!scene`, ban or unban, time someone out for more than ten minutes, or send Hermes slash commands from chat. Everyone else's slash commands are read as plain text. Moderators can still switch to the BRB scene and back with `!brb` and `!back` in your channel. Outside Twitch chat, the Twitch and OBS tools work at your own keyboard (the CLI, the TUI, the desktop app and ACP) and nowhere else unless you turn on `tools_from_other_platforms`.

### Approval for bans

A ban, an unban or a timeout over ten minutes asks you first through Hermes' approval, in chat or at your terminal. A viewer's "approve" does not count. A "session" or "always" answer covers only the same action on the same person in the same channel. Cron jobs and `hermes chat -q` runs are refused by default. One gap remains: `hermes -z` approves everything without asking, and a plugin cannot see that a turn came from it, so do not use `-z` for anything that may moderate chat.

### Token handling

Tallyhand reads the bot token only through Hermes' own secret lookup for the active profile. The running bot never writes, refreshes or rotates it. Only the setup wizard saves a token, the one you type, through Hermes' own settings writer, which creates a new `.env` readable only by your account.

The token goes only to Twitch: chat over TLS, the validate endpoint and the Helix API. Tallyhand does not follow HTTP redirects, so the token cannot be forwarded to another host. Error messages leave out the token and the client id, and Tallyhand registers the token's shapes with Hermes' log redaction where Hermes supports it. It also blanks the token out of every line it posts to chat or delivers for cron, after formatting, so a token split by punctuation or invisible characters is caught too. It checks the token with Twitch when it connects and every hour, and stops when Twitch says it is invalid; any other answer is retried.

### OBS on loopback

By default Tallyhand only talks to OBS on the same computer. It checks the address it actually reached before it sends anything, the login included. Reaching another computer takes an explicit `TALLYHAND_OBS_ALLOW_REMOTE=true`, and a `ws://` connection there is not encrypted. The OBS password never appears in a message or log line, and a URL with a password in it is refused. There is no tool to start or stop the stream or a recording, or to change OBS's settings.

### What Hermes puts in every turn

Hermes gives every Twitch turn, a viewer's included, your `MEMORY.md`, `USER.md`, `SOUL.md`, your project's context files and an external memory provider's recall. A plugin cannot change that per viewer, so run the bot in a Hermes profile of its own, as the README's Recommended setup shows. Tallyhand warns in the log at connect while any of these is still on, except `SOUL.md`, which Hermes always includes. For the memory, the user profile and the memory provider it also enforces the rule: while one is on in the profile that runs the bot, questions from anyone but the owner are refused, unless you set `allow_profile_memory_for_viewers`. The settings are read again for every question, a question that waited in the queue is checked again when it runs, and a config Hermes cannot read counts as memory on.

If you turn on `chat_tail`, other viewers' lines reach your own turns, which run with your tools. A `platform_hints.twitch.replace` setting swaps out Tallyhand's hint to the model, including its instruction not to follow instructions from chat; Tallyhand warns about it at connect. Hermes also lists every plugin's tools in a viewer's turn unless `known_plugin_toolsets.twitch` names them: they run nowhere, but the model sees them. Tallyhand warns at connect while that list leaves out its own `twitch` and `obs` toolsets, with the lines to add.

### Code and dependencies

Tallyhand uses the Python standard library and what Hermes already depends on. It ships no binaries, does not update itself and does not patch Hermes at runtime.
