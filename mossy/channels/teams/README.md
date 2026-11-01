# Teams Channel

Connects Mossy to Microsoft Teams through the Bot Framework. Unlike Slack Socket Mode, Teams sends
inbound HTTPS requests to Mossy, so the app needs a public HTTPS address.

## Configure

```bash
pip install -c requirements.lock -r requirements-teams.txt
```

Add to `.env`:

```text
MICROSOFT_APP_ID=<azure-bot-app-id>
MICROSOFT_APP_PASSWORD=<azure-bot-client-secret>
MOSSY_PUBLIC_BASE_URL=https://<your-host>
```

Optional:

```text
MicrosoftAppType=MultiTenant        # or SingleTenant with MicrosoftAppTenantId
TEAMS_PATH=/api/messages
PLATFORMER_TEAMS_MODEL=
TEAMS_HISTORY_MAX_MESSAGES=40
TEAMS_HISTORY_TTL_SECONDS=7200
TEAMS_HISTORY_MAX_CONVERSATIONS=500
```

The channel starts automatically when both credentials are set. Disable it with `--no-teams`.

## Azure Bot registration

1. Create an Azure Bot in the Azure portal.
2. Set the messaging endpoint to `https://<your-host>/api/messages`.
3. Create a client secret and put the app id and secret into `.env`.
4. Enable the Microsoft Teams channel on the bot.

## Security

The Bot Framework adapter authenticates Teams requests with signed tokens, so this path skips
`MOSSY_API_KEY`.

To limit a skill to named users, use `MOSSY_SKILL_ACCESS` with their AAD object ids (see the main
README). Display names are never used for this, because anyone can share one.

## File downloads

Reply links to `/files/...` become absolute through `MOSSY_PUBLIC_BASE_URL`. The download endpoint
still requires `MOSSY_API_KEY`, which Teams users don't have, so with a key set those links return
401 in a browser.
