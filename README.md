# Sticker Guard Bot

Telegram group sticker firewall with per-group sticker packs, emoji inline search, owner-authorized pack managers, MongoDB stats, and Render/Docker deployment support.

## What it does

- Stickers stay enabled in the Telegram group.
- Manual stickers are deleted.
- Inline messages from other bots are deleted.
- A sticker sent through this bot is kept only if its pack is allowed in that exact group.
- Every group has its own independent pack list.
- Only the group owner or an admin explicitly authorized by the owner can add/remove packs.
- `/search` opens inline mode scoped to that group. Users can type an emoji or a pack name.
- Per-user sticker rate limiting is built in.
- Group stats and bot-owner global stats are stored in MongoDB.
- Activity/error logs expire automatically after 30 days.

## Commands

### Group

- `/add` — reply to a sticker, or `/add <pack link/name>`
- `/rm` — reply to a sticker, or `/rm <pack link/name>`
- `/packs` — list this group's packs
- `/auth` — group owner replies to a Telegram admin to authorize them
- `/unauth` — group owner replies to an authorized admin to revoke them
- `/mods` — authorized admins list
- `/search` — open this group's inline sticker search
- `/stats` — group stats
- `/top` — top packs/emojis
- `/limit` — show limit; `/limit 5 30` sets 5 stickers per 30 seconds per user
- `/help`

### Bot owner (DM only)

- `/bstats`
- `/activity`
- `/groups`
- `/users`
- `/errors`

## BotFather setup

1. Create the bot and copy its token.
2. Enable inline mode with `/setinline` in BotFather. A placeholder such as `Type an emoji…` is fine.
3. Add the bot to each group as an admin.
4. Give it **Delete messages** permission. Without this, moderation cannot work.

The bot is an admin anyway for deletion, so it receives the messages it needs to moderate. Do not give it more Telegram permissions than necessary.

## MongoDB Atlas

Create a MongoDB Atlas database and put its connection string in `MONGO_URI`. The bot creates its own collections/indexes automatically.

For Atlas network access, allow connections from your Render service. The simplest deployment setting is `0.0.0.0/0` plus a strong database username/password; tighter network controls are preferable if your plan/setup supports them.

## Local run

```bash
cp .env.example .env
# Edit .env and leave WEBHOOK_URL empty
pip install -r requirements.txt
python bot.py
```

With no `WEBHOOK_URL`, the bot automatically uses long polling.

## Render deployment

Use a **Web Service** with the Dockerfile.

Environment variables:

```text
BOT_TOKEN=...
BOT_OWNER_ID=your_numeric_telegram_user_id
MONGO_URI=...
DB_NAME=sticker_guard
WEBHOOK_URL=https://YOUR-SERVICE.onrender.com
WEBHOOK_SECRET=a_long_random_AZaz09_-_secret
```

Render supplies `PORT`; the Docker app binds to it and exposes `/health`.

After the service starts, the bot automatically sets its Telegram webhook to:

```text
https://YOUR-SERVICE.onrender.com/telegram/webhook
```

## Important behavior

A group's inline search is opened through `/search`. Telegram inline queries do not provide the destination group ID, so the button inserts an opaque per-group token into the inline query. The bot also checks that the querying user is still a member of that group before returning results.

Even if somebody reuses a result elsewhere, final moderation checks the destination group's allowed pack list. A manually sent sticker from an otherwise allowed pack is still deleted because it did not come through this bot's inline mode.

## Current custom-emoji status

Custom-emoji packs are intentionally rejected for now. Regular, animated, and video sticker packs are supported. Custom emoji can be added later without changing the core permission/database model.
