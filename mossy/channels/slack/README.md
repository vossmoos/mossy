# Slack Channel

This channel connects Mossy to Slack using Slack Socket Mode. Socket Mode means
Mossy opens an outbound WebSocket connection to Slack, so you do not need a
public URL, ngrok, reverse proxy, or Slack request signing endpoint.

Slack messages are handled like the CLI chat: Mossy answers directly by default
with per-thread in-memory history, and only uses the task queue when a skill or
request calls for background, scheduled, or long-running work.

## 1. Install Dependencies

From the repo root:

```bash
pip install -r requirements.txt
```

The Slack channel uses `slack-bolt`, which is listed in `requirements.txt`.

## 2. Create A Slack App

1. Open <https://api.slack.com/apps>.
2. Click **Create New App**.
3. Choose **From scratch**.
4. Pick an app name, for example `Mossy`.
5. Select the workspace where you want to install the bot.

## 3. Enable Socket Mode

1. In the Slack app settings, open **Socket Mode**.
2. Enable Socket Mode.
3. Create an app-level token when Slack asks for one.
4. Add the `connections:write` scope.
5. Copy the generated token. It starts with `xapp-`.

This token becomes:

```text
SLACK_APP_TOKEN=xapp-...
```

## 4. Add Bot Token Scopes

In the Slack app settings, open **OAuth & Permissions**.

Under **Bot Token Scopes**, add:

```text
app_mentions:read
chat:write
im:history
im:read
im:write
```

These scopes allow Mossy to:

- receive `@Mossy` mentions in channels
- read and reply to direct messages
- post replies back to Slack

## 5. Install The App To The Workspace

Still in **OAuth & Permissions**:

1. Click **Install to Workspace**.
2. Approve the requested permissions.
3. Copy the **Bot User OAuth Token**. It starts with `xoxb-`.

This token becomes:

```text
SLACK_BOT_TOKEN=xoxb-...
```

If you later change scopes, Slack will ask you to reinstall the app. Do that and
keep using the updated bot token.

## 6. Subscribe To Bot Events

Open **Event Subscriptions**.

1. Enable events.
2. Under **Subscribe to bot events**, add:

```text
app_mention
message.im
```

You do not need to set a Request URL when using Socket Mode.

## 7. Configure Mossy

Copy `.env.example` to `.env` if you have not already:

```bash
cp .env.example .env
```

Set the Slack values:

```text
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

Optional Slack-specific settings:

```text
PLATFORMER_SLACK_MODEL=
SLACK_HISTORY_TTL_SECONDS=7200
SLACK_HISTORY_MAX_CONVERSATIONS=500
SLACK_HISTORY_MAX_MESSAGES=40
```

If `PLATFORMER_SLACK_MODEL` is empty, Mossy falls back to
`PLATFORMER_CLI_MODEL`, then `PLATFORMER_SKILL_MODEL`.

The history settings are in-memory only:

- `SLACK_HISTORY_TTL_SECONDS`: forget a conversation after this many idle seconds
- `SLACK_HISTORY_MAX_CONVERSATIONS`: maximum cached Slack threads/DMs
- `SLACK_HISTORY_MAX_MESSAGES`: maximum model-history messages kept per conversation

## 8. Run Mossy

Start Mossy from the repo root:

```bash
python main.py
```

Slack starts automatically when both `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are
set. To run Mossy without Slack:

```bash
python main.py --no-slack
```

## 9. Add Mossy To Channels

**What triggers a reply**

- In a **public or private channel**, Mossy only handles **@-mentions of the
  bot** (for example `@Mossy hello`). A normal message in the channel
  **without** an `@` to the app does **nothing** — that is how this channel is
  built, not a bug. You must type `@` and pick the app from the list.
- In a **1:1 DM** with the app, any plain message is handled (you do not need to
  `@` mention the bot).
- In a **thread**, you must still **@-mention the bot** in that thread for each
  message you want it to see (unless you are in a DM).

For public or private channels, invite the Slack app before using it:

```text
/invite @Mossy
```

Then mention it:

```text
@Mossy summarize what this channel is about
```

Mossy replies in the same thread. Thread history is remembered while the process
is running and while the conversation is still inside the in-memory TTL cache.

For direct messages, open a DM with the app and send a message. Mossy replies in
that DM and keeps a separate in-memory history for the DM channel.

### Wrong `@` target (very common)

Slack autocomplete may show a **human** and an **app** with similar names. If you
pick a person instead of the Mossy **app**, Slack never sends `app_mention` to
your bot — Mossy will stay silent.

After Mossy starts, the terminal prints `bot_user_id='U…'`. In Slack, the
mention pill for the **app** should resolve to that same app user.

## 10. Current Limitations

- Queued task results do not automatically post back to Slack yet. If Mossy
  enqueues work, it should give you the task id so you can inspect it through the
  normal queue/status tools.
- Chat history is in-memory only. Restarting Mossy clears Slack conversation
  history.
- The bot ignores messages from bots and Slack message subtypes to avoid loops.

## Troubleshooting

### Mossy is running but Slack does not react

Restart Mossy after editing `.env`. On startup, you should see:

```text
Slack channel enabled (Socket Mode).
Starting Slack Socket Mode channel.
Slack auth.test ok: team='…' bot_user='…' url='…'
```

The `auth.test` line confirms `SLACK_BOT_TOKEN` works and prints the **bot user
name** Slack reports — use an `@` mention that resolves to that bot (pick it from
the `@` autocomplete list).

For verbose tracing, set `SLACK_DEBUG=1` in `.env`. You should see lines like
`[slack debug] incoming event type=app_mention …` whenever Slack delivers an
event.

If you do not see those lines:

- make sure both `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set in `.env`
- make sure you started Mossy from this repo, or that this repo's `.env` is the
  one being loaded
- run `pip install -r requirements.txt` in the same Python environment used to
  start Mossy
- make sure you did not start with `--no-slack`

If you see the startup lines but no `Slack app mention received.` or
`Slack DM received.` after sending a Slack message, Slack is not delivering
events to Mossy. **In a channel,** if you only wrote normal text and never
`@`-mentioned the app, Mossy will not run — that produces no `app_mention`
event. Try: `@YourBotName testing` and watch the terminal for
`Slack app mention received.`

Check:

- **Socket Mode** is enabled
- `SLACK_APP_TOKEN` starts with `xapp-` and has `connections:write`
- **Event Subscriptions** is enabled
- bot events include `app_mention` and `message.im`
- the app was reinstalled after adding scopes
- for channel messages, the app was invited with `/invite @Mossy`
- for DMs, **App Home** has the Messages Tab enabled and allows messages

If you see `Slack app mention received.` or `Slack DM received.` but no Slack
reply, check the Mossy terminal for:

- `Slack chat.postMessage failed: error=…` — common values:
  - `not_in_channel`: the bot is not in that channel; run `/invite @YourBot`
    there
  - `missing_scope` or `invalid_auth`: fix OAuth scopes or reinstall the app
  - `channel_not_found`: wrong workspace or deleted channel

The most common scope mistake is skipping **`chat:write`** or not reinstalling
after changing scopes.
