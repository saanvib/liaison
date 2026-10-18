# Slack setup for the Liaison bot

Use a throwaway workspace. Takes about 10 minutes.

## 1. Create the app
1. Go to https://api.slack.com/apps, click **Create New App**, then **From an app manifest**, and pick your workspace.
2. Paste the contents of [`slack_manifest.yaml`](slack_manifest.yaml) (YAML tab), then **Create**.
   The manifest turns on Socket Mode and interactivity (buttons and the edit modal), adds the `/ask` command, and sets these scopes and events:
   - Bot scopes: `app_mentions:read`, `channels:history`, `chat:write`, `commands`, `groups:history`, `im:history`, `users:read`
   - Bot events: `app_mention`, `message.channels`, `message.groups`, `message.im`
3. Under **Install App**, click **Install to Workspace** and allow it.

To set it up by hand instead: turn on Socket Mode; under Interactivity & Shortcuts, turn interactivity on (Socket Mode needs no URL); under Slash Commands, create `/ask`; under Event Subscriptions, subscribe to the four bot events above; under OAuth & Permissions, add the seven scopes; under App Home, turn on the Messages tab and allow users to send messages.

## 2. Get the two tokens
- `SLACK_BOT_TOKEN` (`xoxb-...`): **OAuth & Permissions**, then **Bot User OAuth Token**.
- `SLACK_APP_TOKEN` (`xapp-...`): **Basic Information**, then **App-Level Tokens**, then **Generate Token and Scopes**, with the scope `connections:write`.

## 3. Create the channels and invite the bot
Create these channels (any names) and run `/invite @Liaison` in each:

| Purpose | Env var |
|---|---|
| R&D experts | `CHANNEL_RD` |
| Quality and Regulatory experts | `CHANNEL_QA_RA` |
| Engineering experts | `CHANNEL_ENGINEERING` |
| Sales Operations experts | `CHANNEL_SALES_OPS` |
| Flagged-question reviewer (D23) | `FLAGGED_FALLBACK_CHANNEL` |
| Where people ask, for example #ask-liaison | none: any channel the bot is in works |

To get a channel ID, open the channel, click its name, and copy the ID at the bottom (it starts with `C`). For `FLAGGED_FALLBACK_USER`, open the reviewer's profile, click **⋮**, then **Copy member ID** (it starts with `U`).

## 4. Point the expert directory at real people
`experts/directory.yaml` (Person 2's file) uses placeholder IDs such as `U_RND_1`. For a live demo, the people who answer must have those Slack IDs in the directory, otherwise their replies are ignored as "not allowed". Ask Person 2 to swap in your workspace's member IDs, or keep a local, uncommitted copy. Then regenerate the Vendor Agent's approver list:

```
python -m experts.print_allowlist
# with the committed placeholder directory this prints:
# APPROVER_ALLOWLIST=U_RND_1,U_RND_2,U_QARA_1,U_QARA_2,U_ENG_1,U_ENG_2,U_SALES_1,U_SALES_2
```
Paste that line into the Vendor Agent's env (A12). It must match the directory exactly.

## 5. Try it
- `/ask What is the storage temperature?` in a channel the bot is in. The bot posts your question and answers in its thread.
- A DM to the bot, or `@Liaison <question>`, works too.
- For a question the documents can't answer, the bot proposes an expert. Click **Approve and send**. The expert replies in the thread in the expert channel, and the answer comes back to your thread.
