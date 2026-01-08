import os
import time
import io
import csv
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

# =========================
# Config
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optionnel (sync instant si tu mets l'ID du serveur)
DB_PATH = "league.sqlite"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant (Railway > Variables)")

FORMATS = ["Genesys", "Compétitif", "Chill"]

def now() -> int:
    return int(time.time())

# =========================
# DB helpers
# =========================
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

# =========================
# Bot
# =========================
intents = discord.Intents.none()
intents.guilds = True

class LeagueBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db: Optional[aiosqlite.Connection] = None

    async def setup_hook(self):
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row

        # Leagues are separated by format
        await db_exec(self.db, """
            CREATE TABLE IF NOT EXISTS leagues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT,
                name TEXT,
                format TEXT,
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

        # Matches belong to a league (=> format), but we store format too for easy export/debug
        await db_exec(self.db, """
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id INTEGER,
                format TEXT,
                p1 TEXT,            -- winner if win, else player A on draw
                p2 TEXT,            -- loser if win, else player B on draw
                result TEXT,        -- 'win' or 'draw'
                created_at INTEGER,
                p1_deck TEXT,
                p2_deck TEXT
            );
        """)

        # Deck directory separated by format
        await db_exec(self.db, """
            CREATE TABLE IF NOT EXISTS decks (
                guild_id TEXT,
                format TEXT,
                name TEXT,
                PRIMARY KEY (guild_id, format, name)
            );
        """)

        # Pending matches for confirmation
        await db_exec(self.db, """
            CREATE TABLE IF NOT EXISTS pending_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id INTEGER,
                guild_id TEXT,
                format TEXT,
                reporter_id TEXT,
                opponent_id TEXT,
                result TEXT,         -- 'win' or 'draw'
                winner_id TEXT,      -- if win
                loser_id TEXT,       -- if win
                p1_deck TEXT,
                p2_deck TEXT,
                created_at INTEGER
            );
        """)

        # Sync commands
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

bot = LeagueBot()

# =========================
# Format helpers
# =========================
def normalize_format(fmt: str) -> str:
    fmt = (fmt or "").strip()
    # accept case-insensitive matches
    for f in FORMATS:
        if fmt.lower() == f.lower():
            return f
    return fmt

async def format_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower().strip()
    return [
        app_commands.Choice(name=f, value=f)
        for f in FORMATS
        if cur in f.lower()
    ][:25]

# =========================
# League helpers
# =========================
async def get_open_league(guild_id: int, fmt: str):
    fmt = normalize_format(fmt)
    return await db_one(
        bot.db,
        "SELECT * FROM leagues WHERE guild_id=? AND format=? AND status='open' ORDER BY id DESC LIMIT 1",
        (str(guild_id), fmt),
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
        await interaction.response.send_message("❌ Tu n'es pas inscrit à ce tournoi. Fais `/joinleague`.", ephemeral=True)
        return False
    if not opp_ok:
        await interaction.response.send_message("❌ Ton adversaire n'est pas inscrit à ce tournoi.", ephemeral=True)
        return False
    return True

# =========================
# Deck helpers
# =========================
async def upsert_deck(guild_id: int, fmt: str, deck_name: str):
    fmt = normalize_format(fmt)
    deck_name = deck_name.strip()
    if deck_name:
        await db_exec(
            bot.db,
            "INSERT OR IGNORE INTO decks (guild_id, format, name) VALUES (?,?,?)",
            (str(guild_id), fmt, deck_name),
        )

def validate_decks(*names: str) -> tuple[bool, str]:
    cleaned = [n.strip() for n in names]
    if any(not n for n in cleaned):
        return False, "❌ Tu dois renseigner tous les decks (pas vide)."
    if any(len(n) > 50 for n in cleaned):
        return False, "❌ Nom de deck trop long (max 50 caractères)."
    return True, ""

async def deck_autocomplete(interaction: discord.Interaction, current: str):
    """
    On filtre par format sélectionné si possible : interaction.namespace.format
    """
    cur = (current or "").strip()
    fmt = None
    try:
        fmt = getattr(interaction.namespace, "format", None)
    except Exception:
        fmt = None
    fmt = normalize_format(fmt) if fmt else None

    if fmt and fmt in FORMATS:
        rows = await db_all(
            bot.db,
            "SELECT name FROM decks WHERE guild_id=? AND format=? AND name LIKE ? ORDER BY name LIMIT 25",
            (str(interaction.guild_id), fmt, f"%{cur}%"),
        )
    else:
        # fallback: sans format (propose global)
        rows = await db_all(
            bot.db,
            "SELECT name FROM decks WHERE guild_id=? AND name LIKE ? ORDER BY name LIMIT 25",
            (str(interaction.guild_id), f"%{cur}%"),
        )

    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

# =========================
# Scoring
# =========================
async def apply_win(league_id: int, winner_id: str, loser_id: str):
    await ensure_standing(league_id, winner_id)
    await ensure_standing(league_id, loser_id)

    await db_exec(
        bot.db,
        "UPDATE standings SET wins=wins+1, points=points+3 WHERE league_id=? AND user_id=?",
        (league_id, winner_id),
    )
    await db_exec(
        bot.db,
        "UPDATE standings SET losses=losses+1 WHERE league_id=? AND user_id=?",
        (league_id, loser_id),
    )

async def apply_draw(league_id: int, p1_id: str, p2_id: str):
    for uid in (p1_id, p2_id):
        await ensure_standing(league_id, uid)
        await db_exec(
            bot.db,
            "UPDATE standings SET draws=draws+1, points=points+1 WHERE league_id=? AND user_id=?",
            (league_id, uid),
        )

# =========================
# Confirmation view (reportmatch)
# =========================
class ConfirmMatchView(discord.ui.View):
    def __init__(self, pending_id: int, opponent_id: int):
        super().__init__(timeout=24 * 60 * 60)
        self.pending_id = pending_id
        self.opponent_id = opponent_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Seul l’adversaire peut confirmer/refuser.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirmer", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await db_one(bot.db, "SELECT * FROM pending_matches WHERE id=?", (self.pending_id,))
        if not p:
            self.disable_all_items()
            return await interaction.response.edit_message(content="ℹ️ Ce match n’est plus en attente.", view=self)

        league = await db_one(bot.db, "SELECT * FROM leagues WHERE id=?", (p["league_id"],))
        if not league or league["status"] != "open":
            await db_exec(bot.db, "DELETE FROM pending_matches WHERE id=?", (self.pending_id,))
            self.disable_all_items()
            return await interaction.response.edit_message(content="❌ Tournoi fermé : confirmation impossible.", view=self)

        reporter_id = int(p["reporter_id"])
        opponent_id = int(p["opponent_id"])
        if not await is_player(p["league_id"], str(reporter_id)) or not await is_player(p["league_id"], str(opponent_id)):
            await db_exec(bot.db, "DELETE FROM pending_matches WHERE id=?", (self.pending_id,))
            self.disable_all_items()
            return await interaction.response.edit_message(content="❌ Un joueur n’est plus inscrit : match annulé.", view=self)

        fmt = normalize_format(p["format"])
        await upsert_deck(int(p["guild_id"]), fmt, p["p1_deck"] or "")
        await upsert_deck(int(p["guild_id"]), fmt, p["p2_deck"] or "")

        if p["result"] == "win":
            winner_id = str(p["winner_id"])
            loser_id = str(p["loser_id"])
            await apply_win(p["league_id"], winner_id, loser_id)

            await db_exec(
                bot.db,
                "INSERT INTO matches (league_id, format, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?,?)",
                (p["league_id"], fmt, winner_id, loser_id, "win", p["created_at"], p["p1_deck"], p["p2_deck"]),
            )
        else:
            await apply_draw(p["league_id"], str(p["reporter_id"]), str(p["opponent_id"]))
            await db_exec(
                bot.db,
                "INSERT INTO matches (league_id, format, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?,?)",
                (p["league_id"], fmt, str(p["reporter_id"]), str(p["opponent_id"]), "draw", p["created_at"], p["p1_deck"], p["p2_deck"]),
            )

        await db_exec(bot.db, "DELETE FROM pending_matches WHERE id=?", (self.pending_id,))
        self.disable_all_items()
        await interaction.response.edit_message(content="✅ Match confirmé et enregistré !", view=self)

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger)
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_exec(bot.db, "DELETE FROM pending_matches WHERE id=?", (self.pending_id,))
        self.disable_all_items()
        await interaction.response.edit_message(content="❌ Match refusé.", view=self)

# =========================
# Autocomplete for reportmatch
# =========================
async def _auto_resultat(interaction: discord.Interaction, current: str):
    choices = ["win", "draw"]
    cur = (current or "").lower()
    return [app_commands.Choice(name=c, value=c) for c in choices if cur in c][:25]

async def _auto_victoire_de(interaction: discord.Interaction, current: str):
    choices = ["moi", "adversaire"]
    cur = (current or "").lower()
    return [app_commands.Choice(name=c, value=c) for c in choices if cur in c][:25]

# =========================
# Commands: League management
# =========================
@bot.tree.command(name="league_create", description="Créer et ouvrir un tournoi (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(format=format_autocomplete)
async def league_create(interaction: discord.Interaction, name: str, format: str):
    fmt = normalize_format(format)
    if fmt not in FORMATS:
        return await interaction.response.send_message("❌ Format invalide (Genesys / Compétitif / Chill).", ephemeral=True)

    # Option: only one open league per format, close previous automatically
    prev = await get_open_league(interaction.guild_id, fmt)
    if prev:
        await db_exec(bot.db, "UPDATE leagues SET status='closed' WHERE id=?", (prev["id"],))

    await db_exec(
        bot.db,
        "INSERT INTO leagues (guild_id, name, format, status) VALUES (?,?,?,?)",
        (str(interaction.guild_id), name, fmt, "open"),
    )
    await interaction.response.send_message(f"✅ Tournoi **{name}** ouvert — format **{fmt}**")

@bot.tree.command(name="league_close", description="Fermer le tournoi d'un format (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(format=format_autocomplete)
async def league_close(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    await db_exec(bot.db, "UPDATE leagues SET status='closed' WHERE id=?", (league["id"],))
    await interaction.response.send_message(f"🔒 Tournoi **{league['name']}** fermé — format **{fmt}**")

@bot.tree.command(name="league_status", description="Infos du tournoi (par format)")
@app_commands.autocomplete(format=format_autocomplete)
async def league_status(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    players_count = await db_one(bot.db, "SELECT COUNT(*) AS c FROM players WHERE league_id=?", (league["id"],))
    matches_count = await db_one(bot.db, "SELECT COUNT(*) AS c FROM matches WHERE league_id=?", (league["id"],))
    decks_count = await db_one(
        bot.db, "SELECT COUNT(*) AS c FROM decks WHERE guild_id=? AND format=?",
        (str(interaction.guild_id), fmt),
    )
    last_match = await db_one(
        bot.db,
        "SELECT created_at, result, p1, p2, p1_deck, p2_deck FROM matches WHERE league_id=? ORDER BY id DESC LIMIT 1",
        (league["id"],),
    )

    msg = [
        f"🏁 **Tournoi ouvert** : **{league['name']}**",
        f"🎮 Format: **{fmt}**",
        f"👥 Inscrits: **{players_count['c']}**",
        f"🧾 Matchs: **{matches_count['c']}**",
        f"📁 Decks (répertoire): **{decks_count['c']}**",
    ]
    if last_match:
        t = f"<t:{last_match['created_at']}:R>" if last_match["created_at"] else ""
        if last_match["result"] == "win":
            msg.append(f"⏱️ Dernier match: 🏆 <@{last_match['p1']}> vs <@{last_match['p2']}> — {t}")
        else:
            msg.append(f"⏱️ Dernier match: 🤝 <@{last_match['p1']}> vs <@{last_match['p2']}> — {t}")

    await interaction.response.send_message("\n".join(msg))

@bot.tree.command(name="joinleague", description="S'inscrire au tournoi (par format)")
@app_commands.autocomplete(format=format_autocomplete)
async def joinleague(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    await db_exec(
        bot.db,
        "INSERT OR IGNORE INTO players (league_id, user_id) VALUES (?,?)",
        (league["id"], str(interaction.user.id)),
    )
    await interaction.response.send_message(f"✅ Inscription validée — format **{fmt}**")

@bot.tree.command(name="leaveleague", description="Quitter le tournoi (stats conservées)")
@app_commands.autocomplete(format=format_autocomplete)
async def leaveleague(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    await db_exec(
        bot.db,
        "DELETE FROM players WHERE league_id=? AND user_id=?",
        (league["id"], str(interaction.user.id)),
    )
    await interaction.response.send_message(f"🚪 Tu as quitté le tournoi **{fmt}**. Tes stats restent en base.")

# =========================
# Commands: Deck directory (per format)
# =========================
@bot.tree.command(name="deck_add", description="Ajouter un deck au répertoire (admin, par format)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(format=format_autocomplete)
async def deck_add(interaction: discord.Interaction, format: str, name: str):
    fmt = normalize_format(format)
    if fmt not in FORMATS:
        return await interaction.response.send_message("❌ Format invalide.", ephemeral=True)

    name = name.strip()
    if not name:
        return await interaction.response.send_message("❌ Nom invalide.", ephemeral=True)
    if len(name) > 50:
        return await interaction.response.send_message("❌ Nom trop long (max 50 caractères).", ephemeral=True)

    await upsert_deck(interaction.guild_id, fmt, name)
    await interaction.response.send_message(f"✅ Deck ajouté au répertoire **{fmt}** : **{name}**")

@bot.tree.command(name="deck_remove", description="Supprimer un deck (admin, par format)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(format=format_autocomplete, name=deck_autocomplete)
async def deck_remove(interaction: discord.Interaction, format: str, name: str):
    fmt = normalize_format(format)
    if fmt not in FORMATS:
        return await interaction.response.send_message("❌ Format invalide.", ephemeral=True)

    name = name.strip()
    if not name:
        return await interaction.response.send_message("❌ Nom invalide.", ephemeral=True)

    await db_exec(
        bot.db,
        "DELETE FROM decks WHERE guild_id=? AND format=? AND name=?",
        (str(interaction.guild_id), fmt, name),
    )
    await interaction.response.send_message(f"🗑️ Deck supprimé du répertoire **{fmt}** : **{name}**")

@bot.tree.command(name="deck_list", description="Afficher le répertoire des decks (par format)")
@app_commands.autocomplete(format=format_autocomplete)
async def deck_list(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    if fmt not in FORMATS:
        return await interaction.response.send_message("❌ Format invalide.", ephemeral=True)

    rows = await db_all(
        bot.db,
        "SELECT name FROM decks WHERE guild_id=? AND format=? ORDER BY name",
        (str(interaction.guild_id), fmt),
    )
    if not rows:
        return await interaction.response.send_message(f"ℹ️ Aucun deck dans le répertoire **{fmt}** pour l’instant.")

    names = [r["name"] for r in rows]
    text = "\n".join(f"• {n}" for n in names)

    if len(text) <= 1800:
        return await interaction.response.send_message(f"📁 **Répertoire des decks — {fmt}**\n{text}")

    await interaction.response.send_message(f"📁 **Répertoire des decks — {fmt}** (suite en messages)")
    chunk = ""
    for n in names:
        line = f"• {n}\n"
        if len(chunk) + len(line) > 1800:
            await interaction.followup.send(chunk)
            chunk = ""
        chunk += line
    if chunk:
        await interaction.followup.send(chunk)

# =========================
# Commands: Matches (direct) - per format
# =========================
@bot.tree.command(name="winversus", description="Déclarer une victoire (par format, decks obligatoires)")
@app_commands.autocomplete(format=format_autocomplete, deck_gagnant=deck_autocomplete, deck_adverse=deck_autocomplete)
async def winversus(
    interaction: discord.Interaction,
    format: str,
    adversaire: discord.User,
    deck_gagnant: str,
    deck_adverse: str,
):
    fmt = normalize_format(format)
    if fmt not in FORMATS:
        return await interaction.response.send_message("❌ Format invalide.", ephemeral=True)
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Impossible contre toi-même.", ephemeral=True)

    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    ok = await require_both_registered(interaction, league["id"], adversaire)
    if not ok:
        return

    valid, err = validate_decks(deck_gagnant, deck_adverse)
    if not valid:
        return await interaction.response.send_message(err, ephemeral=True)

    deck_gagnant = deck_gagnant.strip()
    deck_adverse = deck_adverse.strip()

    # auto-add to deck directory for this format
    await upsert_deck(interaction.guild_id, fmt, deck_gagnant)
    await upsert_deck(interaction.guild_id, fmt, deck_adverse)

    winner_id = str(interaction.user.id)
    loser_id = str(adversaire.id)

    await apply_win(league["id"], winner_id, loser_id)

    await db_exec(
        bot.db,
        "INSERT INTO matches (league_id, format, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?,?)",
        (league["id"], fmt, winner_id, loser_id, "win", now(), deck_gagnant, deck_adverse),
    )

    await interaction.response.send_message(
        f"🏆 **{fmt}** — <@{interaction.user.id}> gagne contre <@{adversaire.id}> (+3 pts)\n"
        f"🃏 Decks: **{deck_gagnant}** vs **{deck_adverse}**"
    )

@bot.tree.command(name="drawversus", description="Déclarer une égalité (par format, decks obligatoires)")
@app_commands.autocomplete(format=format_autocomplete, deck_p1=deck_autocomplete, deck_p2=deck_autocomplete)
async def drawversus(
    interaction: discord.Interaction,
    format: str,
    adversaire: discord.User,
    deck_p1: str,
    deck_p2: str,
):
    fmt = normalize_format(format)
    if fmt not in FORMATS:
        return await interaction.response.send_message("❌ Format invalide.", ephemeral=True)
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Impossible contre toi-même.", ephemeral=True)

    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    ok = await require_both_registered(interaction, league["id"], adversaire)
    if not ok:
        return

    valid, err = validate_decks(deck_p1, deck_p2)
    if not valid:
        return await interaction.response.send_message(err, ephemeral=True)

    deck_p1 = deck_p1.strip()
    deck_p2 = deck_p2.strip()

    await upsert_deck(interaction.guild_id, fmt, deck_p1)
    await upsert_deck(interaction.guild_id, fmt, deck_p2)

    p1_id = str(interaction.user.id)
    p2_id = str(adversaire.id)

    await apply_draw(league["id"], p1_id, p2_id)

    await db_exec(
        bot.db,
        "INSERT INTO matches (league_id, format, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?,?)",
        (league["id"], fmt, p1_id, p2_id, "draw", now(), deck_p1, deck_p2),
    )

    await interaction.response.send_message(
        f"🤝 **{fmt}** — Égalité entre <@{interaction.user.id}> et <@{adversaire.id}> (+1 pt chacun)\n"
        f"🃏 Decks: **{deck_p1}** vs **{deck_p2}**"
    )

# =========================
# Commands: Confirmation flow (optional) - per format
# =========================
@bot.tree.command(name="reportmatch", description="Déclarer un match à confirmer (par format)")
@app_commands.autocomplete(
    format=format_autocomplete,
    deck_moi=deck_autocomplete,
    deck_adverse=deck_autocomplete,
    resultat=_auto_resultat,
    victoire_de=_auto_victoire_de,
)
async def reportmatch(
    interaction: discord.Interaction,
    format: str,
    adversaire: discord.User,
    resultat: str,          # 'win' or 'draw'
    deck_moi: str,
    deck_adverse: str,
    victoire_de: str = "moi"  # 'moi'/'adversaire' if win
):
    fmt = normalize_format(format)
    if fmt not in FORMATS:
        return await interaction.response.send_message("❌ Format invalide.", ephemeral=True)
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Impossible contre toi-même.", ephemeral=True)

    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    ok = await require_both_registered(interaction, league["id"], adversaire)
    if not ok:
        return

    resultat = (resultat or "").strip().lower()
    if resultat not in ("win", "draw"):
        return await interaction.response.send_message("❌ `resultat` doit être `win` ou `draw`.", ephemeral=True)

    valid, err = validate_decks(deck_moi, deck_adverse)
    if not valid:
        return await interaction.response.send_message(err, ephemeral=True)

    deck_moi = deck_moi.strip()
    deck_adverse = deck_adverse.strip()

    reporter_id = interaction.user.id
    opponent_id = adversaire.id

    winner_id = None
    loser_id = None
    p1_deck = None
    p2_deck = None

    if resultat == "win":
        victoire_de = (victoire_de or "moi").strip().lower()
        if victoire_de not in ("moi", "adversaire"):
            return await interaction.response.send_message("❌ `victoire_de` doit être `moi` ou `adversaire`.", ephemeral=True)

        if victoire_de == "moi":
            winner_id = reporter_id
            loser_id = opponent_id
            p1_deck = deck_moi
            p2_deck = deck_adverse
        else:
            winner_id = opponent_id
            loser_id = reporter_id
            p1_deck = deck_adverse
            p2_deck = deck_moi
    else:
        p1_deck = deck_moi
        p2_deck = deck_adverse

    await db_exec(
        bot.db,
        """
        INSERT INTO pending_matches
        (league_id, guild_id, format, reporter_id, opponent_id, result, winner_id, loser_id, p1_deck, p2_deck, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            league["id"],
            str(interaction.guild_id),
            fmt,
            str(reporter_id),
            str(opponent_id),
            resultat,
            str(winner_id) if winner_id is not None else None,
            str(loser_id) if loser_id is not None else None,
            p1_deck,
            p2_deck,
            now(),
        ),
    )

    pending = await db_one(bot.db, "SELECT id FROM pending_matches ORDER BY id DESC LIMIT 1")
    pending_id = int(pending["id"])

    view = ConfirmMatchView(pending_id=pending_id, opponent_id=opponent_id)

    if resultat == "win":
        msg = (
            f"📨 **{fmt}** — <@{reporter_id}> a reporté un match.\n"
            f"Résultat: 🏆 **Victoire de <@{winner_id}>**\n"
            f"Decks: **{p1_deck}** vs **{p2_deck}**\n\n"
            f"<@{opponent_id}>, confirme ou refuse :"
        )
    else:
        msg = (
            f"📨 **{fmt}** — <@{reporter_id}> a reporté un match.\n"
            f"Résultat: 🤝 **Égalité**\n"
            f"Decks: **{p1_deck}** vs **{p2_deck}**\n\n"
            f"<@{opponent_id}>, confirme ou refuse :"
        )

    await interaction.response.send_message(f"✅ Demande envoyée pour confirmation — format **{fmt}**.")
    await interaction.followup.send(msg, view=view)

# =========================
# Commands: Leaderboard / History / Stats (per format)
# =========================
@bot.tree.command(name="league_leaderboard", description="Classement (par format)")
@app_commands.autocomplete(format=format_autocomplete)
async def league_leaderboard(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    rows = await db_all(
        bot.db,
        "SELECT * FROM standings WHERE league_id=? ORDER BY points DESC, wins DESC, draws DESC LIMIT 50",
        (league["id"],),
    )
    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun match enregistré.")

    text = "\n".join(
        f"{i+1}. <@{r['user_id']}> — {r['points']} pts ({r['wins']}/{r['draws']}/{r['losses']})"
        for i, r in enumerate(rows)
    )
    await interaction.response.send_message(f"📊 **Classement — {fmt}**\n{text}")

@bot.tree.command(name="league_history", description="Derniers matchs (par format)")
@app_commands.autocomplete(format=format_autocomplete)
async def league_history(interaction: discord.Interaction, format: str, nombre: app_commands.Range[int, 1, 25] = 10):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    rows = await db_all(
        bot.db,
        """
        SELECT id, p1, p2, result, created_at, p1_deck, p2_deck
        FROM matches
        WHERE league_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (league["id"], int(nombre)),
    )
    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun match enregistré.")

    lines = []
    for m in rows:
        outcome = "🏆 Victoire" if m["result"] == "win" else "🤝 Égalité"
        t = f"<t:{m['created_at']}:R>" if m["created_at"] else ""
        lines.append(
            f"• #{m['id']} — {outcome} — {t}\n"
            f"  <@{m['p1']}> (**{m['p1_deck'] or '?'}**) vs <@{m['p2']}> (**{m['p2_deck'] or '?'}**)"
        )

    await interaction.response.send_message(f"🧾 **Derniers matchs — {fmt}**\n" + "\n".join(lines))

@bot.tree.command(name="league_undo_last", description="Annuler le dernier match (admin, par format)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(format=format_autocomplete)
async def league_undo_last(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    m = await db_one(
        bot.db,
        "SELECT * FROM matches WHERE league_id=? ORDER BY id DESC LIMIT 1",
        (league["id"],),
    )
    if not m:
        return await interaction.response.send_message("ℹ️ Aucun match à annuler.", ephemeral=True)

    if m["result"] == "win":
        await db_exec(
            bot.db,
            "UPDATE standings SET wins=wins-1, points=points-3 WHERE league_id=? AND user_id=?",
            (league["id"], m["p1"]),
        )
        await db_exec(
            bot.db,
            "UPDATE standings SET losses=losses-1 WHERE league_id=? AND user_id=?",
            (league["id"], m["p2"]),
        )
    else:
        for uid in (m["p1"], m["p2"]):
            await db_exec(
                bot.db,
                "UPDATE standings SET draws=draws-1, points=points-1 WHERE league_id=? AND user_id=?",
                (league["id"], uid),
            )

    await db_exec(bot.db, "DELETE FROM matches WHERE id=?", (m["id"],))
    await interaction.response.send_message(f"↩️ Dernier match annulé — **{fmt}**")

@bot.tree.command(name="admin_reset_league", description="Reset tournoi (admin, par format)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(format=format_autocomplete)
async def admin_reset_league(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    await db_exec(bot.db, "DELETE FROM matches WHERE league_id=?", (league["id"],))
    await db_exec(bot.db, "DELETE FROM standings WHERE league_id=?", (league["id"],))
    await db_exec(bot.db, "DELETE FROM players WHERE league_id=?", (league["id"],))
    await db_exec(bot.db, "DELETE FROM pending_matches WHERE league_id=?", (league["id"],))

    await interaction.response.send_message(
        f"🧹 Reset effectué — **{fmt}** : matchs, standings, inscrits supprimés. (Decks du répertoire conservés)"
    )

# =========================
# Stats: my_stats / h2h (per format)
# =========================
@bot.tree.command(name="my_stats", description="Tes stats (par format)")
@app_commands.autocomplete(format=format_autocomplete)
async def my_stats(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    uid = str(interaction.user.id)
    row = await db_one(bot.db, "SELECT * FROM standings WHERE league_id=? AND user_id=?", (league["id"], uid))
    if not row:
        return await interaction.response.send_message("ℹ️ Pas encore de stats pour toi (aucun match).")

    total = int(row["wins"]) + int(row["draws"]) + int(row["losses"])
    winrate = (int(row["wins"]) / total * 100) if total > 0 else 0.0

    decks_rows = await db_all(
        bot.db,
        """
        SELECT deck, COUNT(*) AS games
        FROM (
            SELECT p1_deck AS deck FROM matches WHERE league_id=? AND p1=? AND p1_deck IS NOT NULL AND p1_deck != ''
            UNION ALL
            SELECT p2_deck AS deck FROM matches WHERE league_id=? AND p2=? AND p2_deck IS NOT NULL AND p2_deck != ''
        )
        GROUP BY deck
        ORDER BY games DESC, deck ASC
        LIMIT 5
        """,
        (league["id"], uid, league["id"], uid),
    )
    top_decks = "\n".join([f"• **{r['deck']}** — {r['games']} match(s)" for r in decks_rows]) if decks_rows else "• (aucun deck enregistré)"

    await interaction.response.send_message(
        f"👤 **Tes stats — {fmt}**\n"
        f"• Points: **{row['points']}**\n"
        f"• Bilan: **{row['wins']}**W / **{row['draws']}**D / **{row['losses']}**L (Total: {total})\n"
        f"• Winrate: **{winrate:.1f}%**\n\n"
        f"🃏 **Tes decks les plus joués — {fmt}**\n{top_decks}"
    )

@bot.tree.command(name="h2h", description="Face-à-face contre un joueur (par format)")
@app_commands.autocomplete(format=format_autocomplete)
async def h2h(interaction: discord.Interaction, format: str, adversaire: discord.User):
    fmt = normalize_format(format)
    if adversaire.bot:
        return await interaction.response.send_message("❌ Pas de H2H contre un bot.", ephemeral=True)
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Pas de H2H contre toi-même.", ephemeral=True)

    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    me = str(interaction.user.id)
    opp = str(adversaire.id)

    rows = await db_all(
        bot.db,
        """
        SELECT * FROM matches
        WHERE league_id=?
          AND ((p1=? AND p2=?) OR (p1=? AND p2=?))
        ORDER BY id DESC
        """,
        (league["id"], me, opp, opp, me),
    )
    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun match entre vous deux.")

    w = d = l = 0
    for m in rows:
        if m["result"] == "draw":
            d += 1
        else:
            if m["p1"] == me:
                w += 1
            elif m["p2"] == me:
                l += 1

    last = rows[:5]
    last_lines = []
    for m in last:
        t = f"<t:{m['created_at']}:R>" if m["created_at"] else ""
        if m["result"] == "draw":
            last_lines.append(
                f"• 🤝 {t} — <@{m['p1']}> (**{m['p1_deck'] or '?'}**) vs <@{m['p2']}> (**{m['p2_deck'] or '?'}**)"
            )
        else:
            last_lines.append(
                f"• 🏆 {t} — gagnant: <@{m['p1']}> — decks: **{m['p1_deck'] or '?'}** vs **{m['p2_deck'] or '?'}**"
            )

    await interaction.response.send_message(
        f"🤜🤛 **H2H — {fmt}** <@{interaction.user.id}> vs <@{adversaire.id}>\n"
        f"• Bilan: **{w}W / {d}D / {l}L** (Total: {len(rows)})\n\n"
        "🕘 **Derniers matchs**\n" + "\n".join(last_lines)
    )

# =========================
# Deck stats / matchups (per format)
# =========================
@bot.tree.command(name="deck_stats", description="Stats d'un deck (par format)")
@app_commands.autocomplete(format=format_autocomplete, deck=deck_autocomplete)
async def deck_stats(interaction: discord.Interaction, format: str, deck: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    deck = deck.strip()
    if not deck:
        return await interaction.response.send_message("❌ Deck invalide.", ephemeral=True)

    stats = await db_one(
        bot.db,
        """
        SELECT
          SUM(CASE WHEN result='win' AND p1_deck=? THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN result='win' AND p2_deck=? THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN result='draw' AND (p1_deck=? OR p2_deck=?) THEN 1 ELSE 0 END) AS draws,
          COUNT(*) AS games
        FROM matches
        WHERE league_id=?
          AND (p1_deck=? OR p2_deck=?)
        """,
        (deck, deck, deck, deck, league["id"], deck, deck),
    )

    games = int(stats["games"] or 0)
    if games == 0:
        return await interaction.response.send_message("ℹ️ Aucun match trouvé pour ce deck.")

    wins = int(stats["wins"] or 0)
    losses = int(stats["losses"] or 0)
    draws = int(stats["draws"] or 0)
    winrate = (wins / games * 100) if games else 0.0

    matchup_rows = await db_all(
        bot.db,
        """
        SELECT opp_deck,
               SUM(w) AS wins,
               SUM(l) AS losses,
               SUM(d) AS draws,
               SUM(w + l + d) AS games
        FROM (
            SELECT p2_deck AS opp_deck,
                   CASE WHEN result='win' THEN 1 ELSE 0 END AS w,
                   0 AS l,
                   CASE WHEN result='draw' THEN 1 ELSE 0 END AS d
            FROM matches
            WHERE league_id=? AND p1_deck=? AND p2_deck IS NOT NULL AND p2_deck != ''

            UNION ALL

            SELECT p1_deck AS opp_deck,
                   0 AS w,
                   CASE WHEN result='win' THEN 1 ELSE 0 END AS l,
                   CASE WHEN result='draw' THEN 1 ELSE 0 END AS d
            FROM matches
            WHERE league_id=? AND p2_deck=? AND p1_deck IS NOT NULL AND p1_deck != ''
        )
        GROUP BY opp_deck
        HAVING games >= 2
        ORDER BY (CAST(wins AS REAL) / games) DESC, games DESC, opp_deck ASC
        LIMIT 5
        """,
        (league["id"], deck, league["id"], deck),
    )

    matchup_text = (
        "\n".join(f"• vs **{r['opp_deck']}** — {r['wins']}W/{r['draws']}D/{r['losses']}L (sur {r['games']})" for r in matchup_rows)
        if matchup_rows else
        "• (pas assez de données pour des matchups)"
    )

    await interaction.response.send_message(
        f"🃏 **Stats deck — {fmt}** : **{deck}**\n"
        f"• Matchs: **{games}**\n"
        f"• Bilan: **{wins}W / {draws}D / {losses}L**\n"
        f"• Winrate: **{winrate:.1f}%**\n\n"
        "🎯 **Meilleurs matchups** (min 2 matchs)\n"
        f"{matchup_text}"
    )

@bot.tree.command(name="deck_matchups", description="Matchup entre 2 decks (par format)")
@app_commands.autocomplete(format=format_autocomplete, deck_a=deck_autocomplete, deck_b=deck_autocomplete)
async def deck_matchups(interaction: discord.Interaction, format: str, deck_a: str, deck_b: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    deck_a = deck_a.strip()
    deck_b = deck_b.strip()
    valid, err = validate_decks(deck_a, deck_b)
    if not valid:
        return await interaction.response.send_message(err, ephemeral=True)

    rows = await db_one(
        bot.db,
        """
        SELECT
          SUM(CASE WHEN result='win' AND p1_deck=? AND p2_deck=? THEN 1 ELSE 0 END) AS a_wins,
          SUM(CASE WHEN result='win' AND p1_deck=? AND p2_deck=? THEN 1 ELSE 0 END) AS b_wins,
          SUM(CASE WHEN result='draw' AND ((p1_deck=? AND p2_deck=?) OR (p1_deck=? AND p2_deck=?)) THEN 1 ELSE 0 END) AS draws,
          COUNT(*) AS games
        FROM matches
        WHERE league_id=?
          AND (
                (p1_deck=? AND p2_deck=?)
             OR (p1_deck=? AND p2_deck=?)
          )
        """,
        (
            deck_a, deck_b,
            deck_b, deck_a,
            deck_a, deck_b, deck_b, deck_a,
            league["id"],
            deck_a, deck_b, deck_b, deck_a,
        ),
    )

    games = int(rows["games"] or 0)
    if games == 0:
        return await interaction.response.send_message("ℹ️ Aucun match entre ces deux decks.")

    a_wins = int(rows["a_wins"] or 0)
    b_wins = int(rows["b_wins"] or 0)
    draws = int(rows["draws"] or 0)

    a_winrate = (a_wins / games * 100) if games else 0.0
    b_winrate = (b_wins / games * 100) if games else 0.0

    await interaction.response.send_message(
        f"🆚 **Matchup — {fmt}**\n"
        f"**{deck_a}** vs **{deck_b}**\n"
        f"• Matchs: **{games}**\n"
        f"• {deck_a}: **{a_wins}W** ({a_winrate:.1f}%)\n"
        f"• {deck_b}: **{b_wins}W** ({b_winrate:.1f}%)\n"
        f"• Nuls: **{draws}**"
    )

# =========================
# Exports CSV (per format)
# =========================
def _csv_file(filename: str, rows: list[dict]) -> discord.File:
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    else:
        output.write("empty\n")
    data = output.getvalue().encode("utf-8")
    return discord.File(fp=io.BytesIO(data), filename=filename)

@bot.tree.command(name="export_matches", description="Exporter les matchs CSV (admin, par format)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(format=format_autocomplete)
async def export_matches(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    rows = await db_all(
        bot.db,
        """
        SELECT id, created_at, result, p1, p2, p1_deck, p2_deck, format
        FROM matches
        WHERE league_id=?
        ORDER BY id ASC
        """,
        (league["id"],),
    )
    file = _csv_file(f"matches_{fmt}.csv", [dict(r) for r in rows])
    await interaction.response.send_message(f"📤 Export des matchs — **{fmt}** :", file=file)

@bot.tree.command(name="export_standings", description="Exporter le classement CSV (admin, par format)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(format=format_autocomplete)
async def export_standings(interaction: discord.Interaction, format: str):
    fmt = normalize_format(format)
    league = await get_open_league(interaction.guild_id, fmt)
    if not league:
        return await interaction.response.send_message("❌ Aucun tournoi ouvert pour ce format.", ephemeral=True)

    rows = await db_all(
        bot.db,
        """
        SELECT user_id, points, wins, draws, losses
        FROM standings
        WHERE league_id=?
        ORDER BY points DESC, wins DESC, draws DESC
        """,
        (league["id"],),
    )
    file = _csv_file(f"standings_{fmt}.csv", [dict(r) for r in rows])
    await interaction.response.send_message(f"📤 Export du classement — **{fmt}** :", file=file)
@bot.tree.command(name="league_list_open", description="Lister les tournois ouverts (tous formats)")
async def league_list_open(interaction: discord.Interaction):
    rows = await db_all(
        bot.db,
        """
        SELECT id, name, format
        FROM leagues
        WHERE guild_id=? AND status='open'
        ORDER BY format ASC, id DESC
        """,
        (str(interaction.guild_id),)
    )

    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun tournoi ouvert pour l’instant.")

    lines = []
    for r in rows:
        league_id = r["id"]
        fmt = r["format"] or "?"
        name = r["name"] or "(sans nom)"

        players_count = await db_one(bot.db, "SELECT COUNT(*) AS c FROM players WHERE league_id=?", (league_id,))
        matches_count = await db_one(bot.db, "SELECT COUNT(*) AS c FROM matches WHERE league_id=?", (league_id,))
        last_match = await db_one(
            bot.db,
            "SELECT created_at, result, p1, p2 FROM matches WHERE league_id=? ORDER BY id DESC LIMIT 1",
            (league_id,)
        )

        info = f"👥 {players_count['c']} | 🧾 {matches_count['c']}"
        if last_match and last_match["created_at"]:
            info += f" | ⏱️ <t:{last_match['created_at']}:R>"

        lines.append(f"🎮 **{fmt}** — **{name}**\n{info}")

    await interaction.response.send_message("🏁 **Tournois ouverts**\n\n" + "\n\n".join(lines))
    @bot.tree.command(name="admin_clear_guild_commands", description="Supprimer toutes les slash du serveur (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_clear_guild_commands(interaction: discord.Interaction):
    guild = discord.Object(id=interaction.guild_id)
    bot.tree.clear_commands(guild=guild)   # supprime les commandes "guild"
    await bot.tree.sync(guild=guild)       # applique la suppression
    await interaction.response.send_message("✅ Commandes *serveur* supprimées. Redeploy ensuite pour resync propre.")
    @bot.tree.command(name="admin_clear_guild_commands", description="Supprimer toutes les slash du serveur (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_clear_guild_commands(interaction: discord.Interaction):
    guild = discord.Object(id=interaction.guild_id)
    bot.tree.clear_commands(guild=guild)   # supprime les commandes "guild"
    await bot.tree.sync(guild=guild)       # applique la suppression
    await interaction.response.send_message("✅ Commandes *serveur* supprimées. Redeploy ensuite pour resync propre.")

@bot.tree.command(name="admin_clear_guild_commands", description="Supprimer toutes les slash du serveur (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_clear_guild_commands(interaction: discord.Interaction):
    guild = discord.Object(id=interaction.guild_id)
    bot.tree.clear_commands(guild=guild)   # supprime les commandes "guild"
    await bot.tree.sync(guild=guild)       # applique la suppression
    await interaction.response.send_message("✅ Commandes *serveur* supprimées. Redeploy ensuite pour resync propre.")

# =========================
# Run
# =========================
bot.run(TOKEN)
