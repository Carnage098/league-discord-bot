import os
import time
import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optionnel (sync plus rapide si tu mets l'ID du serveur)
DB_PATH = "league.sqlite"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant (Railway > Variables)")

def now() -> int:
    return int(time.time())

async def db_exec(db, query: str, params=()):
    await db.execute(query, params)
    await db.commit()

async def db_one(db, query: str, params=()):
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    await cur.close()
    return row

async def db_all(db, query: str, params=()):
    cur = await db.execute(query, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows

intents = discord.Intents.none()
intents.guilds = True

class LeagueBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db = None

    async def setup_hook(self):
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row

        # Tables principales
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
                created_at INTEGER,
                p1_deck TEXT,
                p2_deck TEXT
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

        # Répertoire de decks (par serveur)
        await db_exec(self.db, """
            CREATE TABLE IF NOT EXISTS decks (
                guild_id TEXT,
                name TEXT,
                PRIMARY KEY (guild_id, name)
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

# ---------- Helpers ligue / DB ----------

async def get_open_league(guild_id: int):
    return await db_one(
        bot.db,
        "SELECT * FROM leagues WHERE guild_id=? AND status='open' ORDER BY id DESC LIMIT 1",
        (str(guild_id),)
    )

async def ensure_standing(league_id: int, user_id: str):
    row = await db_one(bot.db, "SELECT 1 FROM standings WHERE league_id=? AND user_id=?", (league_id, user_id))
    if not row:
        await db_exec(bot.db, "INSERT INTO standings (league_id, user_id) VALUES (?,?)", (league_id, user_id))

async def is_player(league_id: int, user_id: str) -> bool:
    row = await db_one(bot.db, "SELECT 1 FROM players WHERE league_id=? AND user_id=?", (league_id, user_id))
    return row is not None

async def require_both_registered(interaction: discord.Interaction, league_id: int, opponent: discord.User) -> bool:
    if opponent.bot:
        await interaction.response.send_message("❌ Tu ne peux pas jouer contre un bot.", ephemeral=True)
        return False

    me_ok = await is_player(league_id, str(interaction.user.id))
    opp_ok = await is_player(league_id, str(opponent.id))
    if not me_ok:
        await interaction.response.send_message("❌ Tu n'es pas inscrit à la ligue. Fais `/joinleague`.", ephemeral=True)
        return False
    if not opp_ok:
        await interaction.response.send_message("❌ Ton adversaire n'est pas inscrit à la ligue.", ephemeral=True)
        return False
    return True

async def upsert_deck(guild_id: int, deck_name: str):
    deck_name = deck_name.strip()
    if deck_name:
        await db_exec(bot.db, "INSERT OR IGNORE INTO decks (guild_id, name) VALUES (?,?)", (str(guild_id), deck_name))

# ---------- Autocomplete decks (suggestions) ----------

async def deck_autocomplete(interaction: discord.Interaction, current: str):
    current = (current or "").strip()
    rows = await db_all(
        bot.db,
        "SELECT name FROM decks WHERE guild_id=? AND name LIKE ? ORDER BY name LIMIT 25",
        (str(interaction.guild_id), f"%{current}%")
    )
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

# ---------- Commandes ligue ----------

@bot.tree.command(name="league_create", description="Créer et ouvrir une ligue (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def league_create(interaction: discord.Interaction, name: str):
    await db_exec(
        bot.db,
        "INSERT INTO leagues (guild_id, name, status) VALUES (?,?,?)",
        (str(interaction.guild_id), name, "open")
    )
    await interaction.response.send_message(f"✅ Ligue **{name}** créée et ouverte")

@bot.tree.command(name="league_close", description="Fermer la ligue en cours (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def league_close(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    await db_exec(bot.db, "UPDATE leagues SET status='closed' WHERE id=?", (league["id"],))
    await interaction.response.send_message(f"🔒 Ligue **{league['name']}** fermée")

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

# ---------- Répertoire decks ----------

@bot.tree.command(name="deck_add", description="Ajouter un deck au répertoire (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def deck_add(interaction: discord.Interaction, name: str):
    name = name.strip()
    if not name:
        return await interaction.response.send_message("❌ Nom invalide", ephemeral=True)
    if len(name) > 50:
        return await interaction.response.send_message("❌ Nom trop long (max 50 caractères).", ephemeral=True)

    await upsert_deck(interaction.guild_id, name)
    await interaction.response.send_message(f"✅ Deck ajouté: **{name}**")

@bot.tree.command(name="deck_remove", description="Supprimer un deck du répertoire (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(name=deck_autocomplete)
async def deck_remove(interaction: discord.Interaction, name: str):
    name = name.strip()
    if not name:
        return await interaction.response.send_message("❌ Nom invalide", ephemeral=True)

    await db_exec(bot.db, "DELETE FROM decks WHERE guild_id=? AND name=?", (str(interaction.guild_id), name))
    await interaction.response.send_message(f"🗑️ Deck supprimé: **{name}**")

@bot.tree.command(name="deck_list", description="Afficher le répertoire des decks")
async def deck_list(interaction: discord.Interaction):
    rows = await db_all(
        bot.db,
        "SELECT name FROM decks WHERE guild_id=? ORDER BY name",
        (str(interaction.guild_id),)
    )
    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun deck dans le répertoire pour l’instant.")

    names = [r["name"] for r in rows]
    text = "\n".join(f"• {n}" for n in names)

    # Discord limite la taille des messages, donc on chunk si besoin
    if len(text) <= 1800:
        return await interaction.response.send_message(f"📁 **Répertoire des decks**\n{text}")

    await interaction.response.send_message("📁 **Répertoire des decks** (suite en messages)")
    chunk = ""
    for n in names:
        line = f"• {n}\n"
        if len(chunk) + len(line) > 1800:
            await interaction.followup.send(chunk)
            chunk = ""
        chunk += line
    if chunk:
        await interaction.followup.send(chunk)

# ---------- Matchs (decks obligatoires, texte libre autorisé) ----------

@bot.tree.command(name="winversus", description="Déclarer une victoire (decks obligatoires)")
@app_commands.autocomplete(deck_gagnant=deck_autocomplete, deck_adverse=deck_autocomplete)
async def winversus(
    interaction: discord.Interaction,
    adversaire: discord.User,
    deck_gagnant: str,
    deck_adverse: str
):
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Impossible contre toi-même", ephemeral=True)

    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    ok = await require_both_registered(interaction, league["id"], adversaire)
    if not ok:
        return

    deck_gagnant = deck_gagnant.strip()
    deck_adverse = deck_adverse.strip()
    if not deck_gagnant or not deck_adverse:
        return await interaction.response.send_message("❌ Tu dois renseigner les 2 decks.", ephemeral=True)
    if len(deck_gagnant) > 50 or len(deck_adverse) > 50:
        return await interaction.response.send_message("❌ Nom de deck trop long (max 50 caractères).", ephemeral=True)

    # Auto-ajout au répertoire
    await upsert_deck(interaction.guild_id, deck_gagnant)
    await upsert_deck(interaction.guild_id, deck_adverse)

    await ensure_standing(league["id"], str(interaction.user.id))
    await ensure_standing(league["id"], str(adversaire.id))

    await db_exec(
        bot.db,
        "INSERT INTO matches (league_id, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?)",
        (league["id"], str(interaction.user.id), str(adversaire.id), "win", now(), deck_gagnant, deck_adverse)
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
        f"🏆 <@{interaction.user.id}> gagne contre <@{adversaire.id}> (+3 pts)\n"
        f"🃏 Decks: **{deck_gagnant}** vs **{deck_adverse}**"
    )

@bot.tree.command(name="drawversus", description="Déclarer une égalité (decks obligatoires)")
@app_commands.autocomplete(deck_p1=deck_autocomplete, deck_p2=deck_autocomplete)
async def drawversus(
    interaction: discord.Interaction,
    adversaire: discord.User,
    deck_p1: str,
    deck_p2: str
):
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Impossible contre toi-même", ephemeral=True)

    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    ok = await require_both_registered(interaction, league["id"], adversaire)
    if not ok:
        return

    deck_p1 = deck_p1.strip()
    deck_p2 = deck_p2.strip()
    if not deck_p1 or not deck_p2:
        return await interaction.response.send_message("❌ Tu dois renseigner les 2 decks.", ephemeral=True)
    if len(deck_p1) > 50 or len(deck_p2) > 50:
        return await interaction.response.send_message("❌ Nom de deck trop long (max 50 caractères).", ephemeral=True)

    await upsert_deck(interaction.guild_id, deck_p1)
    await upsert_deck(interaction.guild_id, deck_p2)

    for uid in (interaction.user.id, adversaire.id):
        await ensure_standing(league["id"], str(uid))
        await db_exec(
            bot.db,
            "UPDATE standings SET draws=draws+1, points=points+1 WHERE league_id=? AND user_id=?",
            (league["id"], str(uid))
        )

    await db_exec(
        bot.db,
        "INSERT INTO matches (league_id, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?)",
        (league["id"], str(interaction.user.id), str(adversaire.id), "draw", now(), deck_p1, deck_p2)
    )

    await interaction.response.send_message(
        f"🤝 Égalité entre <@{interaction.user.id}> et <@{adversaire.id}> (+1 pt chacun)\n"
        f"🃏 Decks: **{deck_p1}** vs **{deck_p2}**"
    )

# ---------- Classement ----------

@bot.tree.command(name="league_leaderboard", description="Afficher le classement")
async def league_leaderboard(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    rows = await db_all(
        bot.db,
        "SELECT * FROM standings WHERE league_id=? ORDER BY points DESC, wins DESC, draws DESC LIMIT 50",
        (league["id"],)
    )
    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun match enregistré")

    text = "\n".join(
        f"{i+1}. <@{r['user_id']}> — {r['points']} pts ({r['wins']}/{r['draws']}/{r['losses']})"
        for i, r in enumerate(rows)
    )
    await interaction.response.send_message(f"📊 **Classement**\n{text}")

# ---------- Historique + Stats decks ----------

@bot.tree.command(name="league_history", description="Afficher les derniers matchs (avec decks)")
async def league_history(interaction: discord.Interaction, nombre: app_commands.Range[int, 1, 25] = 10):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    rows = await db_all(
        bot.db,
        """
        SELECT id, p1, p2, result, created_at, p1_deck, p2_deck
        FROM matches
        WHERE league_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (league["id"], int(nombre))
    )

    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun match enregistré")

    lines = []
    for m in rows:
        if m["result"] == "win":
            outcome = "🏆 Victoire"
        elif m["result"] == "draw":
            outcome = "🤝 Égalité"
        else:
            outcome = f"ℹ️ {m['result']}"

        t = f"<t:{m['created_at']}:R>" if m["created_at"] else ""
        p1 = f"<@{m['p1']}>"
        p2 = f"<@{m['p2']}>"
        d1 = m["p1_deck"] or "?"
        d2 = m["p2_deck"] or "?"

        lines.append(f"• #{m['id']} — {outcome} — {t}\n  {p1} (**{d1}**) vs {p2} (**{d2}**)")

    await interaction.response.send_message("🧾 **Derniers matchs**\n" + "\n".join(lines))

@bot.tree.command(name="stats_deck", description="Stats des decks (les plus joués)")
async def stats_deck(interaction: discord.Interaction, top: app_commands.Range[int, 1, 25] = 10):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    rows = await db_all(
        bot.db,
        """
        SELECT deck, COUNT(*) AS games
        FROM (
            SELECT p1_deck AS deck FROM matches WHERE league_id=? AND p1_deck IS NOT NULL AND p1_deck != ''
            UNION ALL
            SELECT p2_deck AS deck FROM matches WHERE league_id=? AND p2_deck IS NOT NULL AND p2_deck != ''
        )
        GROUP BY deck
        ORDER BY games DESC, deck ASC
        LIMIT ?
        """,
        (league["id"], league["id"], int(top))
    )

    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun deck enregistré pour l’instant.")

    text = "\n".join(f"{i+1}. **{r['deck']}** — {r['games']} match(s)" for i, r in enumerate(rows))
    await interaction.response.send_message(f"📈 **Top {top} decks les plus joués**\n{text}")

# ---------- Annuler dernier match (admin) ----------

@bot.tree.command(name="league_undo_last", description="Annuler le dernier match enregistré (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def league_undo_last(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    m = await db_one(
        bot.db,
        "SELECT * FROM matches WHERE league_id=? ORDER BY id DESC LIMIT 1",
        (league["id"],)
    )
    if not m:
        return await interaction.response.send_message("ℹ️ Aucun match à annuler", ephemeral=True)

    if m["result"] == "win":
        await db_exec(
            bot.db,
            "UPDATE standings SET wins=wins-1, points=points-3 WHERE league_id=? AND user_id=?",
            (league["id"], m["p1"])
        )
        await db_exec(
            bot.db,
            "UPDATE standings SET losses=losses-1 WHERE league_id=? AND user_id=?",
            (league["id"], m["p2"])
        )
    elif m["result"] == "draw":
        for uid in (m["p1"], m["p2"]):
            await db_exec(
                bot.db,
                "UPDATE standings SET draws=draws-1, points=points-1 WHERE league_id=? AND user_id=?",
                (league["id"], uid)
            )

    await db_exec(bot.db, "DELETE FROM matches WHERE id=?", (m["id"],))
    await interaction.response.send_message("↩️ Dernier match annulé.")

bot.run(TOKEN)

