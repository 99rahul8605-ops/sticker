# Sticker Guard Bot

Telegram group sticker firewall with per-group approved sticker packs, short group codes, inline sticker search, owner-authorized pack managers, MongoDB stats, and Render/Docker deployment support.

## What it does

- Stickers remain enabled in the Telegram group.
- Every group automatically receives a unique 5-character code such as `K7M2Q`.
- Users type `@YourBot K7M2Q` to see that group's approved stickers.
- Users can add an emoji or pack name after the code to filter results.
- The group owner or an admin explicitly authorized with `/auth` can change the code with `/code NEWCODE` if the new code is available.
- Direct/manual stickers from regular members and non-authorized Telegram admins are deleted.
- The group owner and `/auth`-authorized admins may send stickers directly.
- Inline sticker messages from other bots are deleted for regular members.
- A sticker sent through this bot is kept only if its sticker pack is allowed in that exact group.
- When a member sends a blocked sticker, the bot posts a short English tutorial with an **Open Sticker Search** button prefilled with that group's code.
- Tutorial notices are rate-limited to one per user per group every 5 minutes and automatically delete after 5 minutes.
- Plain `@YourBot` shows a global **How to send stickers** inline help result.
- Wrong group codes show an **Invalid group code** inline result.
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
- `/mods` — list authorized admins
- `/code` — show this group's code
- `/code NEWCODE` — owner/authorized admin changes the code; 3-12 letters, numbers, or `_`
- `/search` — open inline search with this group's code already filled in
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

## Inline usage

If a group's code is `K7M2Q`:

```text
@YourBot K7M2Q
```

shows that group's approved stickers immediately.

```text
@YourBot K7M2Q 😂
```

filters by emoji.

```text
@YourBot K7M2Q packname
```

filters by sticker pack name/title.

Typing only:

```text
@YourBot
```

shows a global **How to send stickers** help result because Telegram does not provide the exact destination group ID to inline bots.

## Group-code behavior

- New groups automatically get a random 5-character code.
- Characters that are easy to confuse, such as `O/0` and `I/1`, are not used in automatically generated codes.
- Custom codes are case-insensitive because they are stored in uppercase.
- Custom code length: 3-12 characters.
- Allowed custom characters: `A-Z`, `0-9`, `_`.
- Codes are globally unique across all groups using the bot.
- Existing MongoDB groups from older bot versions are automatically assigned a code on startup. No database reset is needed.

## BotFather setup

1. Create the bot and copy its token.
2. Enable inline mode with `/setinline` in BotFather. A placeholder such as `Enter group code or search stickers...` is fine.
3. Add the bot to each group as an admin.
4. Give it **Delete messages** permission. Without this, moderation cannot work.

The bot does not need unrelated Telegram admin permissions.

## MongoDB Atlas

Create a MongoDB Atlas database and put its connection string in `MONGO_URI`. The bot creates its collections and indexes automatically.

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

## Current custom-emoji status

Custom-emoji packs are intentionally rejected for now. Regular, animated, and video sticker packs are supported. Custom emoji can be added later without changing the core permission/database model.
