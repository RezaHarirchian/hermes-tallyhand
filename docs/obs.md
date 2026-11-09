# OBS

Tallyhand can drive OBS Studio 28 or later through OBS's own WebSocket server. Chat works without it.

## Setup

1. In OBS, open Tools, WebSocket Server Settings.
2. Tick "Enable WebSocket server" and leave "Enable Authentication" on.
3. Click "Show Connect Info" to see the password.
4. Set it in the bot's Hermes profile, then restart the gateway:

   ```bash
   hermes config set OBS_WEBSOCKET_PASSWORD 'the-password'
   ```

Add `-p tallyhand` after `hermes` if the bot runs in its own profile. These are the other OBS variables:

- **`OBS_WEBSOCKET_URL`** (`ws://127.0.0.1:4455`). Set it only if OBS listens on another port or address. Keep the port in the URL. If you turned authentication off in OBS, set this one anyway, so Tallyhand knows OBS is there.
- **`TALLYHAND_BRB_SCENE`** and **`TALLYHAND_LIVE_SCENE`**. The scenes `!brb` and `!back` switch to.
- **`TALLYHAND_CAPTION_SOURCE`** (`tallyhand_caption`). The Text source captions go to.
- **`TALLYHAND_OBS_ALLOW_REMOTE`** (`false`). See below.

The URL must start with `ws://` or `wss://` and point at this computer: `localhost` or a loopback address such as 127.0.0.1 or ::1. Once connected, Tallyhand checks the address it actually reached before it sends the WebSocket handshake or the login. For OBS on another machine, also set `TALLYHAND_OBS_ALLOW_REMOTE=true`, and know that a `ws://` connection there is not encrypted. The password goes in `OBS_WEBSOCKET_PASSWORD`; a URL with a user name or password in it is refused, and OBS then stays off as if it were not set.

Each tool call and chat command opens its own short connection to OBS and closes it right after. It gives OBS five seconds to connect, five to log in and ten to answer each request. Tallyhand does not listen to OBS's events.

## Who can use OBS

In Twitch chat the OBS tools are yours alone: Tallyhand refuses them for moderators and viewers. Outside Twitch chat they follow the Twitch tools' rule. The CLI, the TUI, the desktop app and ACP may use them. Other messaging platforms, A2A, group chats and cron jobs may use them only with `tools_from_other_platforms: true`.

Until `OBS_WEBSOCKET_PASSWORD` or `OBS_WEBSOCKET_URL` is set, with a URL Tallyhand accepts, OBS stays out of the way: your turns do not list the OBS tools, the chat commands do not answer and `caption_answers` does nothing. Once it is set and OBS is closed, the tools say OBS is not running. The chat commands also need `chat_commands` on, which is the default.

## The tools

Your own turns get the `obs` toolset:

- **`obs_status`**: the live scene, whether OBS is streaming or recording and for how long, fps, CPU use and dropped frames. It changes nothing.
- **`obs_list_scenes`**: the scenes, top to bottom, and which one is live.
- **`obs_switch_scene`**: switches the live scene. A name matches exactly, or ignoring case when only one scene fits.
- **`obs_list_audio_inputs`**: the inputs that carry audio, and whether each is muted.
- **`obs_set_mute`**: mutes, unmutes or toggles one audio input.
- **`obs_show_caption`**: shows a one-line caption on stream (see [Captions](#captions)).
- **`obs_clear_caption`**: removes the caption now.
- **`obs_set_source_visible`**: shows or hides a source in a scene, every copy of it in that scene.
- **`obs_save_replay`**: saves the replay buffer. The buffer must be turned on in OBS's output settings and already running.

Not exposed, on purpose: starting or stopping the stream, a recording or the replay buffer, changing OBS settings, profiles or scene collections, and OBS's event feed.

## Chat commands

- **`!brb`** (moderators and you, in your channel): switches to `TALLYHAND_BRB_SCENE` and remembers the scene it left.
- **`!back`** (moderators and you, in your channel): switches to `TALLYHAND_LIVE_SCENE`, or back to the scene `!brb` left.
- **`!scene <name>`** (you): switches to that scene. `!scene` alone lists the scenes and says which one is live.

`!brb` and `!back` answer only in your own channel, the first in `TWITCH_CHANNEL`, so a moderator of another channel the bot joins cannot switch your scenes. `!commands` lists `!brb`, `!back` and `!scene` only where the caller can use them. Each answer is one short line. A failure tells chat only the short reason, never the address or OBS's own message, which go to the log.

## Captions

Add a Text source named `tallyhand_caption` to the scenes where captions should show, or set `TALLYHAND_CAPTION_SOURCE` to another Text source. A caption replaces that source's text. It is one line of at most 240 characters, cut at a word with "..." when longer.

A caption clears itself after 12 seconds. The agent may pick anything from 3 to 60. A newer caption replaces the old one, and the clearing only removes text that is still the caption it set. The timer runs inside Hermes, so if Hermes stops first, the caption stays until you clear it.

With `caption_answers: true`, the bot's answers in your own channel also show as a caption for 12 seconds. Answers often name the viewer, so those names show on stream. The caption goes out after the chat message and never holds it up: when OBS is closed or slow, chat gets the answer as usual and the log says why the caption did not show.

These are not captioned: answers in your other channels, chat commands, the results of your own slash commands and approval answers, thank-you lines, automatic clip links, cron output, redelivered replies and Hermes' notices.
