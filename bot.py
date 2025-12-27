import os
import time
import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

# =====================
# CONFIG
# =====================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optionnel
DB_PATH = "league.sqlite"

POINTS_WIN = 3
POINTS_DRAW = 1

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant")

# =====================
# UTILS
# =====================
def now():
    return int(time.time())

async def db_exec(db, query, params=()):
    await db.execute(query, params)
    await db.commit()

async def db_one(db, query, params=()):
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    await cur.close()
    return row

async def db_all(db, query, params=()):
    cur = await db.execute(query, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows

# =====================
# BOT
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

        # Tables
        await db_exec(self.db, """
        CREATE TABLE IF NOT EXISTS leagues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT,
            name TEXT,
            status TEXT
        );
        """)

        await db_exec(self.db, """
        CREATE TABLE IF NOT EXISTS players (
            league_id INTEGER,
            user_id TEXT,
            PRIMARY KEY (league_id, user_id)
        );
        """)

        await db_exec(self.db, """
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            league_id INTEGER,
            p1 TEXT,
            p2 TEXT,
            result TEXT,
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

        # Sync commandes
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

bot = LeagueBot()

# =====================
# HELPERS
# =====================
async def get_open_league(guild_id):
    return await db_one(
        bot.db,
        "SELECT * FROM leagues WHERE guild_id=? AND status='open'",
        (str(guild_id),)
    )

async def ensure_standing(league_id, user_id):
    row = await db_one(
        bot.db,
        "SELECT 1 FROM standings WHERE league_id=? AND user_id=?",
        (league_id, user_id)
    )
    if not row:
        await db_exec(
            bot.db,
            "INSERT INTO standings (league_id, user_id) VALUES (?,?)",
            (league_id, user_id)
        )

# =====================
# COMMANDES
# =====================
@bot.tree.command(name="league_create", description="Créer et ouvrir une ligue (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def league_create(interaction: discord.Interaction, name: str):
    await db_exec(
        bot.db,
        "INSERT INTO leagues (guild_id, name, status) VALUES (?,?,?)",
        (str(interaction.guild_id), name, "open")
    )
    await interaction.response.send_message(f"✅ Ligue **{name}** créée et ouverte")

@bot.tree.command(name="joinleague", description="S'inscrire à la ligue")
async def joinleague(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    await db_exec(
        bot.db,
        "INSERT OR IGNORE INTO players (league_id, user_id) VALUES (?,?)",
        (league["id"], str(interaction.user.id))
    )
    await interaction.response.send_message("✅ Inscription validée")

@bot.tree.command(name="winversus", description="Déclarer une victoire")
async def winversus(interaction: discord.Interaction, adversaire: discord.User):
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Impossible contre toi-même", ephemeral=True)

    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    await ensure_standing(league["id"], str(interaction.user.id))
    await ensure_standing(league["id"], str(adversaire.id))

    await db_exec(
        bot.db,
        "INSERT INTO matches (league_id, p1, p2, result, created_at) VALUES (?,?,?,?,?)",
        (league["id"], str(interaction.user.id), str(adversaire.id), "win", now())
    )

    await db_exec(
        bot.db,
        "UPDATE standings SET wins=wins+1, points=points+3 WHERE league_id=? AND user_id=?",
        (league["id"], str(interaction.user.id))
    )
    await db_exec(
        bot.db,
        "UPDATE standings SET losses=losses+1 WHERE league_id=? AND user_id=?",
        (league["id"], str(adversaire.id))
    )

    await interaction.response.send_message(
        f"🏆 <@{interaction.user.id}> gagne contre <@{adversaire.id}> (+3 pts)"
    )

@bot.tree.command(name="drawversus", description="Déclarer une égalité")
async def drawversus(interaction: discord.Interaction, adversaire: discord.User):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    for uid in (interaction.user.id, adversaire.id):
        await ensure_standing(league["id"], str(uid))
        await db_exec(
            bot.db,
            "UPDATE standings SET draws=draws+1, points=points+1 WHERE league_id=? AND user_id=?",
            (league["id"], str(uid))
        )

    await db_exec(
        bot.db,
        "INSERT INTO matches (league_id, p1, p2, result, created_at) VALUES (?,?,?,?,?)",
        (league["id"], str(interaction.user.id), str(adversaire.id), "draw", now())
    )

    await interaction.response.send_message(
        f"🤝 Égalité entre <@{interaction.user.id}> et <@{adversaire.id}> (+1 pt chacun)"
    )

@bot.tree.command(name="league_leaderboard", description="Afficher le classement")
async def leaderboard(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    rows = await db_all(
        bot.db,
        "SELECT * FROM standings WHERE league_id=? ORDER BY points DESC",
        (league["id"],)
    )

    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun match enregistré")

    text = "\n".join(
        f"{i+1}. <@{r['user_id']}> — {r['points']} pts ({r['wins']}/{r['draws']}/{r['losses']})"
        for i, r in enumerate(rows)
    )

    await interaction.response.send_message(f"📊 **Classement**\n{text}")

# =====================
# RUN
# =====================
bot.run(TOKEN)
