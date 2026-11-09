# Twitch tools, roles and approvals

Tallyhand adds a `twitch` toolset for stream info, clips, announcements, shoutouts and moderation. This page covers who may use which tool, how bans are approved and which token scopes each feature needs. The OBS tools are in [docs/obs.md](obs.md).

## Roles

- **Owner.** The broadcaster of the first channel in `TWITCH_CHANNEL`, wherever they chat. That should be you.
- **Moderator.** A moderator or the broadcaster of the channel in question, as Twitch's chat badges show.
- **Viewer.** Everyone else.

Tallyhand learns roles from chat. Someone who has not written in a channel since the gateway started counts as a viewer there until their first line.

## What each turn gets

- **Your turns** run with the toolsets Hermes gives `twitch` (`platform_toolsets.twitch`, which defaults to Hermes' full set), plus the `twitch` and `obs` toolsets. Only your slash commands reach Hermes.
- **A moderator's turns** run with a scratch to-do list and the moderator Twitch tools, and no MCP servers.
- **A viewer's turns** run with the to-do list only, and no MCP servers. Their slash commands are read as plain text.

Hermes adds some tools to every turn that a plugin cannot take away: toolsets from other plugins, the tools of a context engine other than the default, and Tallyhand's own `twitch` and `obs` toolsets. So Tallyhand also checks every tool call in a Twitch turn before it runs, and refuses anything the asker's role may not use. A viewer keeps the to-do list and Hermes' tool search, which only reads the tool catalog. A moderator also keeps the moderator Twitch tools. If the check itself fails, the call is blocked. Each Twitch and OBS tool then checks its caller a second time.

Hermes also lists a plugin's tools in every turn unless `known_plugin_toolsets.twitch` names the toolset, so without that key the model sees the `twitch` and `obs` tools in a viewer's turn, may tell viewers about them, and spends tokens on calls Tallyhand then refuses. Tallyhand warns at connect while the key leaves them out, with the lines to add. The setup wizard's recommended settings add `known_plugin_toolsets: {twitch: [obs, twitch]}` unless your `platform_toolsets.twitch` list names Hermes' default `hermes-twitch` set. To set it by hand, first give `platform_toolsets.twitch` a list that does not name `hermes-twitch`, such as `[todo]`, or your own turns lose both tools as well. Then add the key with `hermes config edit` (with `-p <profile>` for the bot's profile); other plugins' toolsets named there stay out of Twitch turns the same way. You still get both, and your moderators keep the `twitch` toolset.

If your turns also see recent chat (`chat_tail`), anyone in chat can write into them, so keep `platform_toolsets.twitch` small. A moderator's turns never carry the recent chat.

## The tools

Each tool takes an optional `channel`; without one it acts on your channel. A moderator can use the moderator tools, `twitch_stream_info` included, only in a channel they moderate. In your turns, and in other platforms' turns when `tools_from_other_platforms` is on, `twitch_stream_info`, which only reads, works for any channel, and every other tool acts only in channels the bot serves: those in `TWITCH_CHANNEL` and the home channel. A call for another channel is refused before any approval question, so a steered turn cannot ban or announce somewhere else. At your own keyboard there is no such limit. Viewers get none of the tools; `!uptime` covers what they usually ask.

- **`twitch_stream_info`** (you, moderators): whether the channel is live, with its title, game, viewer count and uptime.
- **`twitch_create_clip`** (you, moderators): clips the live stream and returns the clip link and its edit link.
- **`twitch_announce`** (you, moderators): posts a highlighted announcement of up to 500 characters, in `primary`, `blue`, `green`, `orange` or `purple`.
- **`twitch_shoutout`** (you, moderators): gives another streamer a Twitch shoutout. The channel giving it must be live.
- **`twitch_delete_message`** (you, moderators): deletes one chat message by its id.
- **`twitch_timeout`** (you, moderators): times someone out, for 600 seconds unless the agent asks for another length, up to two weeks. Moderators can give up to 600 seconds, and a longer timeout from a moderator is refused. Your own timeouts over 600 seconds need your approval.
- **`twitch_ban`** (you, after approval): bans someone, with an optional reason.
- **`twitch_unban`** (you, after approval): lifts a ban or a timeout.

When a call fails, the tool answers with a short reason, for example the scope the token lacks or that the bot must be a moderator. If a clip, announcement, shoutout or ban may have gone through before the connection failed, the answer says so and asks the agent to check before trying again.

## Outside Twitch chat

You at the CLI, the TUI, the desktop app or an editor connected over ACP are the operator and can use every tool. Turns from other messaging platforms such as Telegram or Discord, from other agents over A2A, from hosted group chats and from cron jobs cannot use them unless you set `tools_from_other_platforms: true`.

The tools are offered only where `TWITCH_BOT_TOKEN` is set. Outside the gateway they call Twitch with that token directly.

## Approval for bans

In your turns, `twitch_ban`, `twitch_unban` and timeouts over 600 seconds go through Hermes' approval first. In Twitch chat Hermes asks in the channel, where viewers can read it too, and waits five minutes (`approvals.timeout`). The question is Hermes' own wording around Tallyhand's one-line summary of the action, and one nobody answers expires without running the action. Answer in the same channel with `!hermes approve` or `!hermes deny`. `!hermes approve session`, `!hermes approve always`, `!hermes /approve` and `!hermes /deny` work too. Only your answer counts: a viewer's `!hermes approve` is just a question in their own conversation. If you ask the bot something else while it waits, Hermes by default takes that as an interruption, and the ban does not run.

A "session" or "always" answer covers the same action on the same person in the same channel, not every later ban. Hermes saves an "always" answer in the bot profile's `config.yaml`, under `command_allowlist`, as `plugin_rule:twitch_ban:<channel>:<login>`. That line names the person; delete it to take the approval back.

In slow mode the question may take up to three lines, so the answer words and the deadline are never cut off. With slow mode at 15 seconds or more, make the bot a VIP or moderator. Otherwise posting the question takes longer than Hermes waits for it: Hermes logs an error and posts no notice when the approval runs out, though the approval itself still works.

Tallyhand tells Hermes that people type its commands after `!hermes ` on Twitch, so Hermes' hints in chat read `!hermes ...` and answers to Hermes' own questions work as shown, such as `!hermes approve`. Every other command needs the slash after the trigger, for example `!hermes /new`, and so do Hermes' other hints: for "!hermes model" type `!hermes /model`.

In the CLI or TUI you get Hermes' usual prompt. Cron jobs and `hermes chat -q` runs are refused, since nobody is there to answer. A `hermes -z` run is different: Hermes treats it as unattended and approves every call without asking, a ban included, and a plugin cannot tell that a turn came from `-z`. Do not use `hermes -z` for anything that may moderate chat. The Hermes settings that skip the question are listed in [docs/configuration.md](configuration.md#approvals).

## Token scopes

Ask for these scopes when you mint the bot's token:

- **Chat (required):** `chat:read`, `chat:edit`.
- **Stream info, `!uptime` and account lookups:** no scope.
- **Clips (`twitch_create_clip`, `!clip`, automatic clips):** `clips:edit`.
- **Timeouts, bans and unbans:** `moderator:manage:banned_users`.
- **Deleting messages:** `moderator:manage:chat_messages`.
- **Announcements:** `moderator:manage:announcements`.
- **Shoutouts (`twitch_shoutout`, `!so`):** `moderator:manage:shoutouts`.

The moderation scopes also need the bot account to be a moderator in the channel. A missing scope turns off only that feature, and the tool's answer names the scope to add. `!so` still posts its chat line without the shoutout scope.

Mint the token with your own Twitch application. Tallyhand sends that application's client id with every API call, so a token from someone else's generator would speak for their app. After you mint a new token, update `TWITCH_BOT_TOKEN` and restart the gateway.
