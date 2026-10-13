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
- `/import [limit]` – scan recent messages in the current channel for results posted while the bot was offline
- `/settings` – view and change bot configurations
- `/help` – how the bot works and what it can do
- `/clear` – deletes saved results
- `/stats` – shows yours and other player statistics

## Setup

1. Create an application at <https://discord.com/developers/applications>, open the **Bot** tab, copy the token, and enable **Message Content Intent**.
2. Invite the bot to your server with this link, replacing `YOUR_APPLICATION_ID` with the **Application ID** from the **General Information** tab (just the number):

```
   https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=85056
```

It asks for the `bot` and `applications.commands` scopes and these permissions: View Channels, Send Messages, Embed Links, Read Message History, Add Reactions. You can also build the link under **OAuth2 > URL Generator**. 3. Install and configure:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in DISCORD_TOKEN (and optionally the rest)
python bot.py
```

4. When the bot joins a server it sends a short message to the server's owner (or, if their DMs are closed, to the system channel or the first channel it can write in) explaining the next step. In Discord, someone with the Manage Server permission runs `/settings channel` in the channel where you post your results. The bot then posts a short guide there; `/help` shows it again at any time.

Data is stored in a local SQLite file (`daily.db`).

## Adding a game

Write a `parse_<game>(text, now)` function that returns a `GameResult` (or `None`), where `sort_key` is "lower = better", then add it to `GAMES` and `PARSERS` in `bot.py`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests need no Discord connection or token. When a game changes its share format, add the new text as a test
before fixing the parser.
