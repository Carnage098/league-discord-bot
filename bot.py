import os
import time
import io
import csv
import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

# =========================
# Config
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optionnel (sync plus rapide si tu mets l'ID du serveur)
DB_PATH = "league.sqlite"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant (Railway > Variables)")

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
        self.db: aiosqlite.Connection | None = None

    async def setup_hook(self):
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row

        # Core tables
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
                p1 TEXT,             -- gagnant si result='win' (sinon joueur A si draw)
                p2 TEXT,             -- perdant si result='win' (sinon joueur B si draw)
                result TEXT,         -- 'win' ou 'draw'
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

        # Deck directory (per guild)
        await db_exec(self.db, """
            CREATE TABLE IF NOT EXISTS decks (
                guild_id TEXT,
                name TEXT,
                PRIMARY KEY (guild_id, name)
            );
        """)

        # Pending matches (for confirmation flow)
        await db_exec(self.db, """
            CREATE TABLE IF NOT EXISTS pending_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id INTEGER,
                guild_id TEXT,
                reporter_id TEXT,
                opponent_id TEXT,
                result TEXT,         -- 'win' ou 'draw'
                winner_id TEXT,      -- si result='win'
                loser_id TEXT,       -- si result='win'
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
# League helpers
# =========================
async def get_open_league(guild_id: int):
    return await db_one(
        bot.db,
        "SELECT * FROM leagues WHERE guild_id=? AND status='open' ORDER BY id DESC LIMIT 1",
        (str(guild_id),),
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

def validate_decks(*names: str) -> tuple[bool, str]:
    cleaned = [n.strip() for n in names]
    if any(not n for n in cleaned):
        return False, "❌ Tu dois renseigner tous les decks (pas vide)."
    if any(len(n) > 50 for n in cleaned):
        return False, "❌ Nom de deck trop long (max 50 caractères)."
    return True, ""

# =========================
# Autocomplete decks
# =========================
async def deck_autocomplete(interaction: discord.Interaction, current: str):
    current = (current or "").strip()
    rows = await db_all(
        bot.db,
        "SELECT name FROM decks WHERE guild_id=? AND name LIKE ? ORDER BY name LIMIT 25",
        (str(interaction.guild_id), f"%{current}%"),
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
# Confirmation view (report_match)
# =========================
class ConfirmMatchView(discord.ui.View):
    def __init__(self, pending_id: int, opponent_id: int):
        super().__init__(timeout=24 * 60 * 60)  # 24h
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

        # Safety: league still open?
        league = await db_one(bot.db, "SELECT * FROM leagues WHERE id=?", (p["league_id"],))
        if not league or league["status"] != "open":
            await db_exec(bot.db, "DELETE FROM pending_matches WHERE id=?", (self.pending_id,))
            self.disable_all_items()
            return await interaction.response.edit_message(content="❌ Ligue fermée : confirmation impossible.", view=self)

        # Must still be registered
        reporter_id = int(p["reporter_id"])
        opponent_id = int(p["opponent_id"])
        if not await is_player(p["league_id"], str(reporter_id)) or not await is_player(p["league_id"], str(opponent_id)):
            await db_exec(bot.db, "DELETE FROM pending_matches WHERE id=?", (self.pending_id,))
            self.disable_all_items()
            return await interaction.response.edit_message(
                content="❌ Un des joueurs n’est plus inscrit : match annulé.",
                view=self
            )

        # Auto-add decks to directory
        await upsert_deck(int(p["guild_id"]), p["p1_deck"] or "")
        await upsert_deck(int(p["guild_id"]), p["p2_deck"] or "")

        # Apply result + write match
        if p["result"] == "win":
            winner_id = str(p["winner_id"])
            loser_id = str(p["loser_id"])
            await apply_win(p["league_id"], winner_id, loser_id)

            # Store match with p1 = winner, p2 = loser
            await db_exec(
                bot.db,
                "INSERT INTO matches (league_id, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?)",
                (p["league_id"], winner_id, loser_id, "win", p["created_at"], p["p1_deck"], p["p2_deck"]),
            )
        else:
            # draw: keep p1=reporter, p2=opponent (as submitted)
            await apply_draw(p["league_id"], str(p["reporter_id"]), str(p["opponent_id"]))
            await db_exec(
                bot.db,
                "INSERT INTO matches (league_id, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?)",
                (p["league_id"], str(p["reporter_id"]), str(p["opponent_id"]), "draw", p["created_at"], p["p1_deck"], p["p2_deck"]),
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
# Slash commands: League
# =========================
@bot.tree.command(name="league_create", description="Créer et ouvrir une ligue (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def league_create(interaction: discord.Interaction, name: str):
    await db_exec(
        bot.db,
        "INSERT INTO leagues (guild_id, name, status) VALUES (?,?,?)",
        (str(interaction.guild_id), name, "open"),
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

@bot.tree.command(name="league_status", description="Infos sur la ligue ouverte")
async def league_status(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    players_count = await db_one(bot.db, "SELECT COUNT(*) AS c FROM players WHERE league_id=?", (league["id"],))
    matches_count = await db_one(bot.db, "SELECT COUNT(*) AS c FROM matches WHERE league_id=?", (league["id"],))
    last_match = await db_one(
        bot.db,
        "SELECT created_at, result, p1, p2, p1_deck, p2_deck FROM matches WHERE league_id=? ORDER BY id DESC LIMIT 1",
        (league["id"],),
    )
    decks_count = await db_one(bot.db, "SELECT COUNT(*) AS c FROM decks WHERE guild_id=?", (str(interaction.guild_id),))

    msg = [
        f"🏁 **Ligue ouverte** : **{league['name']}**",
        f"👥 Inscrits: **{players_count['c']}**",
        f"🧾 Matchs: **{matches_count['c']}**",
        f"📁 Decks dans le répertoire: **{decks_count['c']}**",
    ]
    if last_match:
        t = f"<t:{last_match['created_at']}:R>" if last_match["created_at"] else ""
        if last_match["result"] == "win":
            msg.append(f"⏱️ Dernier match: 🏆 <@{last_match['p1']}> vs <@{last_match['p2']}> — {t}")
        else:
            msg.append(f"⏱️ Dernier match: 🤝 <@{last_match['p1']}> vs <@{last_match['p2']}> — {t}")

    await interaction.response.send_message("\n".join(msg))

@bot.tree.command(name="joinleague", description="S'inscrire à la ligue")
async def joinleague(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    await db_exec(
        bot.db,
        "INSERT OR IGNORE INTO players (league_id, user_id) VALUES (?,?)",
        (league["id"], str(interaction.user.id)),
    )
    await interaction.response.send_message("✅ Inscription validée")

@bot.tree.command(name="leaveleague", description="Quitter la ligue (on garde tes stats)")
async def leaveleague(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    await db_exec(
        bot.db,
        "DELETE FROM players WHERE league_id=? AND user_id=?",
        (league["id"], str(interaction.user.id)),
    )
    await interaction.response.send_message("🚪 Tu as quitté la ligue. Tes stats sont conservées (tu ne peux plus déclarer de match).")

@bot.tree.command(name="league_leaderboard", description="Afficher le classement")
async def league_leaderboard(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    rows = await db_all(
        bot.db,
        "SELECT * FROM standings WHERE league_id=? ORDER BY points DESC, wins DESC, draws DESC LIMIT 50",
        (league["id"],),
    )
    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun match enregistré")

    text = "\n".join(
        f"{i+1}. <@{r['user_id']}> — {r['points']} pts ({r['wins']}/{r['draws']}/{r['losses']})"
        for i, r in enumerate(rows)
    )
    await interaction.response.send_message(f"📊 **Classement**\n{text}")

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
        (league["id"], int(nombre)),
    )
    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun match enregistré")

    lines = []
    for m in rows:
        outcome = "🏆 Victoire" if m["result"] == "win" else "🤝 Égalité"
        t = f"<t:{m['created_at']}:R>" if m["created_at"] else ""
        p1 = f"<@{m['p1']}>"
        p2 = f"<@{m['p2']}>"
        d1 = m["p1_deck"] or "?"
        d2 = m["p2_deck"] or "?"
        lines.append(f"• #{m['id']} — {outcome} — {t}\n  {p1} (**{d1}**) vs {p2} (**{d2}**)")

    await interaction.response.send_message("🧾 **Derniers matchs**\n" + "\n".join(lines))

@bot.tree.command(name="league_undo_last", description="Annuler le dernier match enregistré (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def league_undo_last(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    m = await db_one(
        bot.db,
        "SELECT * FROM matches WHERE league_id=? ORDER BY id DESC LIMIT 1",
        (league["id"],),
    )
    if not m:
        return await interaction.response.send_message("ℹ️ Aucun match à annuler", ephemeral=True)

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
    elif m["result"] == "draw":
        for uid in (m["p1"], m["p2"]):
            await db_exec(
                bot.db,
                "UPDATE standings SET draws=draws-1, points=points-1 WHERE league_id=? AND user_id=?",
                (league["id"], uid),
            )

    await db_exec(bot.db, "DELETE FROM matches WHERE id=?", (m["id"],))
    await interaction.response.send_message("↩️ Dernier match annulé.")

@bot.tree.command(name="admin_reset_league", description="Reset standings + matchs + inscrits (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_reset_league(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    # Reset only current league data (keeps deck directory)
    await db_exec(bot.db, "DELETE FROM matches WHERE league_id=?", (league["id"],))
    await db_exec(bot.db, "DELETE FROM standings WHERE league_id=?", (league["id"],))
    await db_exec(bot.db, "DELETE FROM players WHERE league_id=?", (league["id"],))
    await db_exec(bot.db, "DELETE FROM pending_matches WHERE league_id=?", (league["id"],))

    await interaction.response.send_message(
        "🧹 Reset effectué : matchs, standings et inscrits supprimés pour la ligue ouverte. (Répertoire decks conservé)"
    )

# =========================
# Slash commands: Deck directory
# =========================
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
        (str(interaction.guild_id),),
    )
    if not rows:
        return await interaction.response.send_message("ℹ️ Aucun deck dans le répertoire pour l’instant.")

    names = [r["name"] for r in rows]
    text = "\n".join(f"• {n}" for n in names)

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

# =========================
# Slash commands: Matches (direct)
# =========================
@bot.tree.command(name="winversus", description="Déclarer une victoire (decks obligatoires)")
@app_commands.autocomplete(deck_gagnant=deck_autocomplete, deck_adverse=deck_autocomplete)
async def winversus(
    interaction: discord.Interaction,
    adversaire: discord.User,
    deck_gagnant: str,
    deck_adverse: str,
):
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Impossible contre toi-même", ephemeral=True)

    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    ok = await require_both_registered(interaction, league["id"], adversaire)
    if not ok:
        return

    valid, err = validate_decks(deck_gagnant, deck_adverse)
    if not valid:
        return await interaction.response.send_message(err, ephemeral=True)

    deck_gagnant = deck_gagnant.strip()
    deck_adverse = deck_adverse.strip()

    # Auto-add to deck directory
    await upsert_deck(interaction.guild_id, deck_gagnant)
    await upsert_deck(interaction.guild_id, deck_adverse)

    winner_id = str(interaction.user.id)
    loser_id = str(adversaire.id)

    await apply_win(league["id"], winner_id, loser_id)

    await db_exec(
        bot.db,
        "INSERT INTO matches (league_id, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?)",
        (league["id"], winner_id, loser_id, "win", now(), deck_gagnant, deck_adverse),
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
    deck_p2: str,
):
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Impossible contre toi-même", ephemeral=True)

    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    ok = await require_both_registered(interaction, league["id"], adversaire)
    if not ok:
        return

    valid, err = validate_decks(deck_p1, deck_p2)
    if not valid:
        return await interaction.response.send_message(err, ephemeral=True)

    deck_p1 = deck_p1.strip()
    deck_p2 = deck_p2.strip()

    await upsert_deck(interaction.guild_id, deck_p1)
    await upsert_deck(interaction.guild_id, deck_p2)

    p1_id = str(interaction.user.id)
    p2_id = str(adversaire.id)

    await apply_draw(league["id"], p1_id, p2_id)

    await db_exec(
        bot.db,
        "INSERT INTO matches (league_id, p1, p2, result, created_at, p1_deck, p2_deck) VALUES (?,?,?,?,?,?,?)",
        (league["id"], p1_id, p2_id, "draw", now(), deck_p1, deck_p2),
    )

    await interaction.response.send_message(
        f"🤝 Égalité entre <@{interaction.user.id}> et <@{adversaire.id}> (+1 pt chacun)\n"
        f"🃏 Decks: **{deck_p1}** vs **{deck_p2}**"
    )

# =========================
# Slash commands: Confirmation flow (optional)
# =========================
class ResultType(app_commands.Transform):
    pass

@bot.tree.command(name="report_match", description="Déclarer un match à confirmer par l'adversaire (optionnel)")
@app_commands.autocomplete(deck_moi=deck_autocomplete, deck_adverse=deck_autocomplete)
async def report_match(
    interaction: discord.Interaction,
    adversaire: discord.User,
    resultat: app_commands.Choice[str],  # 'win' or 'draw'
    deck_moi: str,
    deck_adverse: str,
    victoire_de: app_commands.Choice[str] | None = None,  # 'moi'/'adversaire' si resultat='win'
):
    """
    Note: Discord Choice needs to be provided via autocomplete-like selection.
    We'll populate choices using app_commands.choices below.
    """
    await interaction.response.send_message("❌ Cette commande n'a pas été initialisée correctement.", ephemeral=True)

@report_match.autocomplete("resultat")
async def _auto_resultat(interaction: discord.Interaction, current: str):
    choices = [
        app_commands.Choice(name="🏆 Victoire", value="win"),
        app_commands.Choice(name="🤝 Égalité", value="draw"),
    ]
    cur = (current or "").lower()
    return [c for c in choices if cur in c.value or cur in c.name.lower()][:25]

@report_match.autocomplete("victoire_de")
async def _auto_victoire_de(interaction: discord.Interaction, current: str):
    choices = [
        app_commands.Choice(name="Moi", value="moi"),
        app_commands.Choice(name="Adversaire", value="adversaire"),
    ]
    cur = (current or "").lower()
    return [c for c in choices if cur in c.value or cur in c.name.lower()][:25]

# Re-define properly (Discord doesn't let us easily declare Choice params with dynamic choices in signature cleanly)
# We'll register a second command name to avoid confusion in some environments.

@bot.tree.command(name="reportmatch", description="Déclarer un match à confirmer (win/draw)")
@app_commands.autocomplete(deck_moi=deck_autocomplete, deck_adverse=deck_autocomplete, resultat=_auto_resultat, victoire_de=_auto_victoire_de)
async def reportmatch(
    interaction: discord.Interaction,
    adversaire: discord.User,
    resultat: str,          # 'win' ou 'draw'
    deck_moi: str,
    deck_adverse: str,
    victoire_de: str = "moi"  # 'moi' ou 'adversaire' (utilisé seulement si resultat='win')
):
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Impossible contre toi-même", ephemeral=True)

    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

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

    # For win, decide winner/loser
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
        # draw: store as reporter/opponent order with their decks
        p1_deck = deck_moi
        p2_deck = deck_adverse

    # Insert pending
    await db_exec(
        bot.db,
        """
        INSERT INTO pending_matches
        (league_id, guild_id, reporter_id, opponent_id, result, winner_id, loser_id, p1_deck, p2_deck, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            league["id"],
            str(interaction.guild_id),
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

    # Send confirmation to opponent
    view = ConfirmMatchView(pending_id=pending_id, opponent_id=opponent_id)

    if resultat == "win":
        msg = (
            f"📨 <@{reporter_id}> a reporté un match.\n"
            f"Résultat: 🏆 **Victoire de {'<@'+str(winner_id)+'>'}**\n"
            f"Decks: **{p1_deck}** vs **{p2_deck}**\n\n"
            f"<@{opponent_id}>, confirme ou refuse :"
        )
    else:
        msg = (
            f"📨 <@{reporter_id}> a reporté un match.\n"
            f"Résultat: 🤝 **Égalité**\n"
            f"Decks: **{p1_deck}** vs **{p2_deck}**\n\n"
            f"<@{opponent_id}>, confirme ou refuse :"
        )

    await interaction.response.send_message("✅ Demande envoyée à l’adversaire pour confirmation.")
    await interaction.followup.send(msg, view=view)

# =========================
# Slash commands: Stats (players)
# =========================
@bot.tree.command(name="my_stats", description="Afficher tes stats + tes decks les plus joués")
async def my_stats(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    uid = str(interaction.user.id)
    row = await db_one(bot.db, "SELECT * FROM standings WHERE league_id=? AND user_id=?", (league["id"], uid))
    if not row:
        return await interaction.response.send_message("ℹ️ Pas encore de stats pour toi (aucun match).")

    total = int(row["wins"]) + int(row["draws"]) + int(row["losses"])
    winrate = (int(row["wins"]) / total * 100) if total > 0 else 0.0

    # Top decks played by this user (as p1 or p2)
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
        "👤 **Tes stats**\n"
        f"• Points: **{row['points']}**\n"
        f"• Bilan: **{row['wins']}**W / **{row['draws']}**D / **{row['losses']}**L (Total: {total})\n"
        f"• Winrate: **{winrate:.1f}%**\n\n"
        "🃏 **Tes decks les plus joués**\n"
        f"{top_decks}"
    )

@bot.tree.command(name="h2h", description="Stats face-à-face contre un joueur")
async def h2h(interaction: discord.Interaction, adversaire: discord.User):
    if adversaire.bot:
        return await interaction.response.send_message("❌ Pas de H2H contre un bot.", ephemeral=True)
    if adversaire.id == interaction.user.id:
        return await interaction.response.send_message("❌ Pas de H2H contre toi-même.", ephemeral=True)

    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

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
            # p1 is winner
            if m["p1"] == me:
                w += 1
            elif m["p2"] == me:
                l += 1

    last = rows[:5]
    last_lines = []
    for m in last:
        t = f"<t:{m['created_at']}:R>" if m["created_at"] else ""
        if m["result"] == "draw":
            last_lines.append(f"• 🤝 {t} — <@{m['p1']}> (**{m['p1_deck'] or '?'}**) vs <@{m['p2']}> (**{m['p2_deck'] or '?'}**)")
        else:
            last_lines.append(f"• 🏆 {t} — gagnant: <@{m['p1']}> — decks: **{m['p1_deck'] or '?'}** vs **{m['p2_deck'] or '?'}**")

    await interaction.response.send_message(
        f"🤜🤛 **H2H** <@{interaction.user.id}> vs <@{adversaire.id}>\n"
        f"• Bilan: **{w}W / {d}D / {l}L** (Total: {len(rows)})\n\n"
        "🕘 **Derniers matchs**\n" + "\n".join(last_lines)
    )

# =========================
# Slash commands: Deck stats
# =========================
@bot.tree.command(name="deck_stats", description="Stats d'un deck (matchs, winrate, matchups)")
@app_commands.autocomplete(deck=deck_autocomplete)
async def deck_stats(interaction: discord.Interaction, deck: str):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    deck = deck.strip()
    if not deck:
        return await interaction.response.send_message("❌ Deck invalide.", ephemeral=True)

    # games, wins, draws, losses for the deck
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
        return await interaction.response.send_message("ℹ️ Aucun match trouvé pour ce deck (dans la ligue ouverte).")

    wins = int(stats["wins"] or 0)
    losses = int(stats["losses"] or 0)
    draws = int(stats["draws"] or 0)
    winrate = (wins / games * 100) if games > 0 else 0.0

    # Matchups summary: opponent deck vs this deck (wins/losses/draws)
    matchup_rows = await db_all(
        bot.db,
        """
        SELECT opp_deck,
               SUM(w) AS wins,
               SUM(l) AS losses,
               SUM(d) AS draws,
               SUM(w + l + d) AS games
        FROM (
            -- deck is p1_deck
            SELECT p2_deck AS opp_deck,
                   CASE WHEN result='win' THEN 1 ELSE 0 END AS w,
                   0 AS l,
                   CASE WHEN result='draw' THEN 1 ELSE 0 END AS d
            FROM matches
            WHERE league_id=? AND p1_deck=? AND p2_deck IS NOT NULL AND p2_deck != ''

            UNION ALL

            -- deck is p2_deck
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

    if matchup_rows:
        matchup_text = "\n".join(
            f"• vs **{r['opp_deck']}** — {r['wins']}W/{r['draws']}D/{r['losses']}L (sur {r['games']})"
            for r in matchup_rows
        )
    else:
        matchup_text = "• (pas assez de données pour des matchups)"

    await interaction.response.send_message(
        f"🃏 **Stats deck: {deck}**\n"
        f"• Matchs: **{games}**\n"
        f"• Bilan: **{wins}W / {draws}D / {losses}L**\n"
        f"• Winrate: **{winrate:.1f}%**\n\n"
        "🎯 **Meilleurs matchups** (min 2 matchs)\n"
        f"{matchup_text}"
    )

@bot.tree.command(name="deck_matchups", description="Résultats entre 2 decks")
@app_commands.autocomplete(deck_a=deck_autocomplete, deck_b=deck_autocomplete)
async def deck_matchups(interaction: discord.Interaction, deck_a: str, deck_b: str):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    deck_a = deck_a.strip()
    deck_b = deck_b.strip()
    valid, err = validate_decks(deck_a, deck_b)
    if not valid:
        return await interaction.response.send_message(err, ephemeral=True)

    # Count games where A vs B (either side)
    rows = await db_one(
        bot.db,
        """
        SELECT
          SUM(CASE
                WHEN result='win' AND p1_deck=? AND p2_deck=? THEN 1
                WHEN result='win' AND p1_deck=? AND p2_deck=? THEN 0
                ELSE 0
              END) AS a_wins_direct,
          SUM(CASE
                WHEN result='win' AND p1_deck=? AND p2_deck=? THEN 1
                ELSE 0
              END) AS b_wins_direct,
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
            deck_a, deck_b,  # a_wins_direct: A is p1
            deck_b, deck_a,  # a_wins_direct: if B is p1 then A didn't win there
            deck_b, deck_a,  # b_wins_direct: B is p1
            deck_a, deck_b, deck_b, deck_a,  # draws
            league["id"],
            deck_a, deck_b, deck_b, deck_a,
        ),
    )

    games = int(rows["games"] or 0)
    if games == 0:
        return await interaction.response.send_message("ℹ️ Aucun match entre ces deux decks (dans la ligue ouverte).")

    a_wins = int(rows["a_wins_direct"] or 0)
    b_wins = int(rows["b_wins_direct"] or 0)
    draws = int(rows["draws"] or 0)

    a_winrate = (a_wins / games * 100) if games else 0.0
    b_winrate = (b_wins / games * 100) if games else 0.0

    await interaction.response.send_message(
        f"🆚 **Matchup**\n"
        f"**{deck_a}** vs **{deck_b}**\n"
        f"• Matchs: **{games}**\n"
        f"• {deck_a}: **{a_wins}W** ({a_winrate:.1f}%)\n"
        f"• {deck_b}: **{b_wins}W** ({b_winrate:.1f}%)\n"
        f"• Nuls: **{draws}**"
    )

# =========================
# Slash commands: Exports CSV
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

@bot.tree.command(name="export_matches", description="Exporter les matchs en CSV (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def export_matches(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

    rows = await db_all(
        bot.db,
        """
        SELECT id, created_at, result, p1, p2, p1_deck, p2_deck
        FROM matches
        WHERE league_id=?
        ORDER BY id ASC
        """,
        (league["id"],),
    )
    dict_rows = [dict(r) for r in rows]
    file = _csv_file("matches.csv", dict_rows)

    await interaction.response.send_message("📤 Export des matchs :", file=file)

@bot.tree.command(name="export_standings", description="Exporter le classement en CSV (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def export_standings(interaction: discord.Interaction):
    league = await get_open_league(interaction.guild_id)
    if not league:
        return await interaction.response.send_message("❌ Aucune ligue ouverte", ephemeral=True)

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
    dict_rows = [dict(r) for r in rows]
    file = _csv_file("standings.csv", dict_rows)

    await interaction.response.send_message("📤 Export du classement :", file=file)

# =========================
# Run
# =========================
bot.run(TOKEN)
