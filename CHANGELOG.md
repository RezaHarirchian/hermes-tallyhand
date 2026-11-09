# Changelog

## 0.1.0

First release. Needs Hermes Agent 0.22.7 or later.

- Twitch chat as a Hermes gateway platform. The bot answers questions asked with a trigger, an @mention or a reply, in plain text within Twitch's rate limits and slow mode. Each channel runs one question with three waiting, the owner's and moderators' first, and every viewer has a cooldown.
- Roles from chat badges: viewers get a to-do list, moderators the moderator Twitch tools and the owner their own toolsets. A hook checks every tool call in a Twitch turn, and the setup wizard can keep the Twitch and OBS tools out of viewers' turns.
- The `twitch` toolset: stream info, clips, announcements, shoutouts, message deletion, timeouts, and bans and unbans after Hermes' approval.
- The `obs` toolset and `!scene` for the owner, and `!brb` and `!back` for the owner and moderators in the owner's channel.
- Chat commands without a model call: `!uptime`, `!bot`, `!commands`, `!privacy`, `!so` and `!clip`.
- Viewer notes by consent with `!remember`, `!mynotes` and `!forgetme`, which also deletes the viewer's Hermes sessions in that channel.
- Optional thank-you lines for raids and subs, automatic clips, and OBS captions for answers.
- A setup wizard in `hermes gateway setup`, and delivery for cron jobs and `hermes send`.
- Replies in the stream's language. `reply_language` takes `channel`, `viewer` or a two-letter code, and `TALLYHAND_REPLY_LANGUAGE` wins over it. Title and tag markers, with strong and weak rules, can name the stream's language, including languages Twitch does not list, and the lookup refreshes about every two minutes. Command and event lines stay English.
- Reply cleanup: Markdown is flattened and a line never starts with `/` or `.`.
