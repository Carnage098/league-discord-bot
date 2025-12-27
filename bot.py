
import os
import time
import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite

# =====================
# CONFIG
# =====================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optionnel
DB_PATH = "league.sqlite"

POINTS_WIN = 3
POINTS_DRAW = 1
MAX_MATCHES_PER_PAIR = 5

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant")

# =====================
# DB UTILS
# =====================
def now():
    return int(time.time() * 1000)

async def db_exec(db, q, p=()):
    await db.execute(q, p)
    await db.commit()

async def db_one(db, q, p=()):
    cur = await db.execute(q, p)
    r = await cur.fetchone()
    await cur.close()
    return r

async def db_all(db, q, p=()):
    cur = await db.execute(q, p)
    r = await cur.fetchall()
    await cur.close()
    return r

# =====================
# BOT SETUP
# =====================
intents = discord.Intents.none()
intents.guilds = True

class LeagueBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db = None

    async def setup_hook(self):
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row

        await db_exec(self.db, """
        CREATE TABLE IF NOT EXISTS leagues (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          guild_id TEXT,
          name TEXT,
          status TEXT,
          created_at INTEGER
        )""")

               await db_exec(self.db, """
        CREATE TABLE IF NOT EXISTS leagues (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          guild_id TEXT,
          name TEXT,
          status TEXT,
          created_at INTEGER
        );
        """)

        await db_exec(self.db, """
        CREATE TABLE IF NOT EXISTS players (
          league_id INTEGER,
          user_id TEXT,
          active INTEGER,
          PRIMARY KEY (league_id, user_id)
        );
        """)

        await db_exec(self.db, """
        CREATE TABLE IF NOT EXISTS matches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          league_id INTEGER,
          p1 TEXT,
          p2 TEXT,
          outcome TEXT,
          status TEXT,
          created_at INTEGER
        );
        """)

        await db_exec(self.db, """
        CREATE TABLE IF NOT EXISTS standings (
          league_id INTEGER,
          user_id TEXT,
          wins INTEGER DEFAULT 0,
          draws INTEGER DEFAULT 0,
          losses INTEGER DEFAULT 0,
          points INTEGER DEFAULT 0,
          PRIMARY KEY (league_id, user_id)
        );
        """)
