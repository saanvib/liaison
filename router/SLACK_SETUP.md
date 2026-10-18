# Slack setup for the Liaison bot

Use a throwaway workspace. Takes about 10 minutes.

## How it works in Slack
- **Everyone asks in one channel,** for example #ask-liaison, with `/ask <question>`. The bot posts the question and answers in that post's thread.
- **If the documents can't answer it,** Person 2's matcher picks the right expert. The bot names them in the thread and asks the asker to approve.
- **After approval, the expert gets the question by DM.** They reply in that DM's thread, and the answer comes back to the asker's thread.
- **The experts channel** (for example #liaison-experts) only gets questions with no good expert match, or whose expert can't be DMed.
- **Flagged ("potentially classified") questions** are DMed to one fixed reviewer and never posted in a channel.

## 1. Create the app
1. Go to https://api.slack.com/apps, click **Create New App**, then **From an app manifest**, and pick your workspace.
2. Paste the contents of [`slack_manifest.yaml`](slack_manifest.yaml) (YAML tab), then **Create**.
   The manifest turns on Socket Mode and interactivity (buttons and the edit modal), adds the `/ask` command, and sets these scopes and events:
   - Bot scopes: `app_mentions:read`, `channels:history`, `chat:write`, `commands`, `groups:history`, `im:history`, `im:write`, `users:read`
   - Bot events: `app_mention`, `message.channels`, `message.groups`, `message.im`
3. Under **Install App**, click **Install to Workspace** and allow it.

To set it up by hand instead: turn on Socket Mode; under Interactivity & Shortcuts, turn interactivity on (Socket Mode needs no URL); under Slash Commands, create `/ask`; under Event Subscriptions, subscribe to the four bot events above; under OAuth & Permissions, add the eight scopes; under App Home, turn on the Messages tab and allow users to send messages (experts reply to the bot's DMs there).

## 2. Get the two tokens
- `SLACK_BOT_TOKEN` (`xoxb-...`): **OAuth & Permissions**, then **Bot User OAuth Token**.
- `SLACK_APP_TOKEN` (`xapp-...`): **Basic Information**, then **App-Level Tokens**, then **Generate Token and Scopes**, with the scope `connections:write`.

## 3. Create two channels and pick a reviewer
| Channel or person | Env var | Invite the bot? |
|---|---|---|
| #ask-liaison: where everyone asks | none (any channel the bot is in works) | yes, `/invite @Liaison` |
| #liaison-experts: no-match fallback | `EXPERTS_CHANNEL` | yes |
| The flagged-question reviewer (a person) | `FLAGGED_FALLBACK_USER` | n/a |

- **Channel ID:** open the channel, click its name, and copy the ID at the bottom (it starts with `C`).
- **Member ID:** open the person's profile, click **⋮**, then **Copy member ID** (it starts with `U`). For testing, the reviewer can be you.

## 4. Point the expert directory at real people
`experts/directory.yaml` (Person 2's file) uses placeholder IDs such as `U_RND_1`. The bot DMs whatever ID the matcher returns, so for a live demo the experts need real member IDs from your workspace.
- **With placeholder IDs,** the DM fails and the question falls back to #liaison-experts. Replies there are still only accepted from IDs in the directory.
- **To fix it,** ask Person 2 to swap in your workspace's member IDs, or keep a local, uncommitted copy.

Then regenerate the Vendor Agent's approver list:

```
python -m experts.print_allowlist
# with the committed placeholder directory this prints:
# APPROVER_ALLOWLIST=U_RND_1,U_RND_2,U_QARA_1,U_QARA_2,U_ENG_1,U_ENG_2,U_SALES_1,U_SALES_2
```
Paste that line into the Vendor Agent's env (A12). It must match the directory exactly.

## 5. Try it
1. In #ask-liaison, run `/ask What is the storage temperature?`. The answer appears in the thread under your question.
2. Ask something the documents can't answer. The bot proposes an expert; click **Approve and send**.
3. The expert gets a DM from Liaison and replies **in that message's thread**.
4. The answer appears in your thread in #ask-liaison.

A DM to the bot, or `@Liaison <question>`, also works for asking.
