"""Runs before the tests: sets the environment bot.py needs when it is imported."""
import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")  # never a real token
os.environ["TIMEZONE"] = "Europe/Lisbon"              # keeps the timezone tests deterministic