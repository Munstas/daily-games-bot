# Daily Games Scoreboard Bot

A Discord bot that reads shared results from daily games (paste them in a channel), figures out who did best, and keeps a scoreboard and all-time records.

Supported games: **Fermi**, **Krillion**, **MapTap**, **Clues by Sam**.

## How it works

- Post a game's share text in the channel. The bot reacts with ✅ when it saves it, or 🔁 if you already submitted that puzzle. Other chat is ignored.
- Editing a message updates the stored result; deleting it removes the result.
- Each game is scored per puzzle by pairwise comparison: 1 point per opponent beaten, 0.5 per tie.

## Commands

- `/today` – today's results per game
- `/scoreboard [period]` – points leaderboard (today / week / month / all time)
- `/records` – all-time best results, overall and per player

## Setup

1. Create an application at <https://discord.com/developers/applications>, open the **Bot** tab, copy the token, and enable **Message Content Intent**.
2. Invite the bot with the `bot` and `applications.commands` scopes and these permissions: View Channels, Send Messages, Embed Links, Read Message History, Add Reactions.
3. Install and configure:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in DISCORD_TOKEN (and optionally the rest)
python bot.py
```

Data is stored in a local SQLite file (`daily.db`).
