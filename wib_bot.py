import os
import asyncio
import re
import time
import json
import sqlite3
import random
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "./wib.db")
HOST_ROLE_NAME = (os.getenv("HOST_ROLE_NAME", "") or "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in environment.")
if OWNER_ID <= 0:
    raise RuntimeError("Missing/invalid OWNER_ID in environment (Mike only).")


# -----------------------------
# Utilities
# -----------------------------

def now_ms() -> int:
    return int(time.time() * 1000)

def norm_num(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not re.fullmatch(r"-?\d+", s):
        return None
    try:
        return int(s)
    except Exception:
        return None

def norm_word(w: str) -> str:
    w = (w or "").strip().upper()
    w = re.sub(r"[^A-Z0-9 ]+", "", w)
    w = re.sub(r"\s+", " ", w).strip()
    return w

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild

def has_host_role(member: discord.Member) -> bool:
    if not HOST_ROLE_NAME:
        return is_admin(member)
    return any(r.name == HOST_ROLE_NAME for r in member.roles) or is_admin(member)

def is_owner(user: discord.abc.User) -> bool:
    return user.id == OWNER_ID


# -----------------------------
# Database
# -----------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  session_seed INTEGER NOT NULL,
  is_locked INTEGER NOT NULL DEFAULT 0,
  current_box INTEGER NOT NULL DEFAULT 1,
  opened_boxes_count INTEGER NOT NULL DEFAULT 0,
  eliminations_unlocked INTEGER NOT NULL DEFAULT 0,
  lobby_msg_id INTEGER,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS participants (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  eliminated INTEGER NOT NULL DEFAULT 0,
  joined_at_ms INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id, user_id)
);

CREATE TABLE IF NOT EXISTS prizes (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  box_id INTEGER NOT NULL,
  title TEXT,
  description TEXT,
  filled_by INTEGER,
  filled_at_ms INTEGER,
  PRIMARY KEY (guild_id, channel_id, box_id)
);

CREATE TABLE IF NOT EXISTS box_ownership (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  box_id INTEGER NOT NULL,
  owner_user_id INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id, box_id)
);

CREATE TABLE IF NOT EXISTS box_secrets (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  box_id INTEGER NOT NULL,
  phrase_w1 TEXT NOT NULL,
  phrase_w2 TEXT NOT NULL,
  phrase_w3 TEXT NOT NULL,
  deck_json TEXT NOT NULL,
  revealed_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id, box_id)
);

CREATE TABLE IF NOT EXISTS trivia_rounds (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  box_id INTEGER NOT NULL,
  q_text TEXT NOT NULL,
  answer_int INTEGER NOT NULL,
  published_msg_id INTEGER,
  is_active INTEGER NOT NULL DEFAULT 0,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id, box_id)
);

CREATE TABLE IF NOT EXISTS trivia_submissions (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  box_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  value_int INTEGER NOT NULL,
  submitted_at_ms INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id, box_id, user_id)
);

CREATE TABLE IF NOT EXISTS order_rounds (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  box_id INTEGER NOT NULL,
  slot_user_id INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  items_json TEXT NOT NULL,
  correct_order_json TEXT NOT NULL,
  published_msg_id INTEGER,
  is_active INTEGER NOT NULL DEFAULT 0,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id, box_id)
);

CREATE TABLE IF NOT EXISTS slot_state (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  box_id INTEGER NOT NULL,
  slot_user_id INTEGER,
  turns_left INTEGER NOT NULL DEFAULT 0,
  pending_action TEXT,
  pending_msg_id INTEGER,
  PRIMARY KEY (guild_id, channel_id, box_id)
);

CREATE TABLE IF NOT EXISTS puzzle_attempts (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  box_id INTEGER NOT NULL,
  attempt_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  g1 TEXT NOT NULL,
  g2 TEXT NOT NULL,
  g3 TEXT NOT NULL,
  submitted_at_ms INTEGER NOT NULL,
  checked INTEGER NOT NULL DEFAULT 0,
  score_positions INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, channel_id, box_id, attempt_id)
);
"""

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    try:
        con.executescript(SCHEMA)
        try:
            con.execute("ALTER TABLE sessions ADD COLUMN lobby_msg_id INTEGER")
        except sqlite3.OperationalError:
            pass
        con.commit()
    finally:
        con.close()

init_db()


# -----------------------------
# Session-unique content generation (seeded)
# -----------------------------

NUMERIC_TEMPLATES = [
    "Box {box}: Using seed {seed}, compute: (seed % 97) + (players * {k}). Answer as an integer.",
    "Box {box}: Take seed {seed}. Compute: (seed % 100) - {k} + (players * 2). Answer as an integer.",
    "Box {box}: Let N be number of registered players ({players}). Compute: (seed % 89) + N + {k}.",
    "Box {box}: Compute: (seed % 73) + ({k} * players) - (box * 3).",
]

ORDER_TEMPLATES = [
    "Arrange these five deliveries from earliest to latest (1 to 5):",
    "Arrange these five values from smallest to largest (1 to 5):",
    "Arrange these five checkpoints from first to last (1 to 5):",
]

WORD_BANK_1 = ["ONE", "SILVER", "MIDNIGHT", "BRIGHT", "HIDDEN", "GOLDEN", "QUIET", "FIRST", "SOFT", "BLUE", "CRISP", "FAIR"]
WORD_BANK_2 = ["FINE", "STILL", "COLD", "TRUE", "SMALL", "WILD", "GREEN", "DARK", "CLEAR", "LAST", "SWEET", "SHARP"]
WORD_BANK_3 = ["AFTERNOON", "MORNING", "HORIZON", "PROMISE", "WHISPER", "GARDEN", "LANTERN", "SUNRISE", "MOONLIGHT", "COMPASS", "VICTORY", "FIRELIGHT"]

def gen_numeric_question(seed: int, box_id: int, player_count: int) -> Tuple[str, int]:
    rng = random.Random(seed * 100 + box_id * 7 + player_count)
    tpl = rng.choice(NUMERIC_TEMPLATES)
    k = rng.randint(3, 11)
    if "seed % 97" in tpl:
        ans = (seed % 97) + (player_count * k)
    elif "seed % 100" in tpl:
        ans = (seed % 100) - k + (player_count * 2)
    elif "seed % 89" in tpl:
        ans = (seed % 89) + player_count + k
    else:
        ans = (seed % 73) + (k * player_count) - (box_id * 3)
    q = tpl.format(seed=seed, box=box_id, players=player_count, k=k)
    return q, int(ans)

def gen_order_question(seed: int, box_id: int) -> Tuple[str, List[str], List[int]]:
    rng = random.Random(seed * 200 + box_id * 19)
    mode = rng.choice(ORDER_TEMPLATES)
    items: List[str] = []
    values: List[int] = []
    for i in range(5):
        v = rng.randint(10, 99)
        while v in values:
            v = rng.randint(10, 99)
        values.append(v)
        items.append(f"{chr(65+i)}: Item {i+1} ({v})")
    if "earliest" in mode or "first to last" in mode:
        sorted_pairs = sorted(list(enumerate(values)), key=lambda x: x[1])
    else:
        sorted_pairs = sorted(list(enumerate(values)), key=lambda x: x[1])
    correct_indices = [idx for idx, _ in sorted_pairs]
    prompt = f"Box {box_id}: {mode}\n" + "\n".join(items) + "\n\nSubmit with: /wib order A B C D E"
    return prompt, items, correct_indices

def gen_phrase_and_deck(seed: int, box_id: int) -> Tuple[Tuple[str, str, str], List[dict]]:
    rng = random.Random(seed * 300 + box_id * 31)
    w1 = rng.choice(WORD_BANK_1)
    w2 = rng.choice(WORD_BANK_2)
    w3 = rng.choice(WORD_BANK_3)
    phrase = (w1, w2, w3)

    deck: List[dict] = []
    deck.append({"type": "PIECE", "reveal": "W1"})
    deck.append({"type": "PIECE", "reveal": "W2"})
    deck.append({"type": "PIECE", "reveal": "W3"})

    remaining = 7
    if box_id == 1:
        for _ in range(remaining):
            t = rng.choices(["PIECE", "PASS"], weights=[7, 3])[0]
            if t == "PIECE":
                deck.append({"type": "PIECE", "reveal": rng.choice(["W1", "W2", "W3"])})
            else:
                deck.append({"type": "PASS"})
    elif 2 <= box_id <= 5:
        for _ in range(remaining):
            t = rng.choices(["PIECE", "PASS", "STEAL"], weights=[6, 2, 2])[0]
            if t == "PIECE":
                deck.append({"type": "PIECE", "reveal": rng.choice(["W1", "W2", "W3"])})
            else:
                deck.append({"type": t})
    else:
        for _ in range(remaining):
            t = rng.choices(["PIECE", "PASS", "STEAL", "DONATE", "WILDCARD"], weights=[5, 2, 2, 2, 1])[0]
            if t == "PIECE":
                deck.append({"type": "PIECE", "reveal": rng.choice(["W1", "W2", "W3"])})
            else:
                deck.append({"type": t})

    rng.shuffle(deck)
    return phrase, deck


# -----------------------------
# Discord UI Components
# -----------------------------

class JoinView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int, channel_id: int, locked: bool = False):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.join_button.disabled = locked

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="wib:join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = interaction.guild.get_member(interaction.user.id)

        con = db()
        try:
            row = con.execute(
                "SELECT is_locked FROM sessions WHERE guild_id=? AND channel_id=?",
                (self.guild_id, self.channel_id),
            ).fetchone()
            if not row:
                return await interaction.followup.send("No active lobby in this channel.", ephemeral=True)
            if int(row["is_locked"]) == 1:
                return await interaction.followup.send("Entries are locked.", ephemeral=True)

            con.execute(
                """INSERT INTO participants (guild_id, channel_id, user_id, display_name, eliminated, joined_at_ms)
                   VALUES (?, ?, ?, ?, 0, ?)
                   ON CONFLICT(guild_id, channel_id, user_id) DO UPDATE SET
                     display_name=excluded.display_name, eliminated=0""",
                (self.guild_id, self.channel_id, member.id, member.display_name, now_ms()),
            )
            con.commit()
        finally:
            con.close()

        await interaction.followup.send("You are registered for this session.", ephemeral=True)


class PreviewPublishView(discord.ui.View):
    def __init__(self, on_publish, on_regen, on_cancel):
        super().__init__(timeout=300)
        self.on_publish = on_publish
        self.on_regen = on_regen
        self.on_cancel = on_cancel

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_publish(interaction)
        self.stop()

    @discord.ui.button(label="Regenerate", style=discord.ButtonStyle.primary)
    async def regen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_regen(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_cancel(interaction)
        self.stop()


# ---- NEW: Numeric Answer button -> modal ----

class NumericAnswerModal(discord.ui.Modal, title="Submit Answer"):
    answer = discord.ui.TextInput(label="Your number", placeholder="Enter a whole number", max_length=12)

    def __init__(self, guild_id: int, channel_id: int, box_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.box_id = box_id

    async def on_submit(self, interaction: discord.Interaction):
        val = norm_num(str(self.answer))
        if val is None:
            return await interaction.response.send_message("Invalid number. Use whole numbers only.", ephemeral=True)

        con = db()
        try:
            sess = con.execute(
                "SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                (self.guild_id, self.channel_id),
            ).fetchone()
            if not sess or int(sess["is_locked"]) != 1:
                return await interaction.response.send_message("No active locked session.", ephemeral=True)

            # Must be registered & not eliminated
            row = con.execute(
                "SELECT eliminated FROM participants WHERE guild_id=? AND channel_id=? AND user_id=?",
                (self.guild_id, self.channel_id, interaction.user.id),
            ).fetchone()
            if not row or int(row["eliminated"]) != 0:
                return await interaction.response.send_message("You are not registered (or you are eliminated).", ephemeral=True)

            tr = con.execute(
                "SELECT is_active FROM trivia_rounds WHERE guild_id=? AND channel_id=? AND box_id=?",
                (self.guild_id, self.channel_id, self.box_id),
            ).fetchone()
            if not tr or int(tr["is_active"]) != 1:
                return await interaction.response.send_message("No active question right now.", ephemeral=True)

            existing = con.execute(
                "SELECT 1 FROM trivia_submissions WHERE guild_id=? AND channel_id=? AND box_id=? AND user_id=?",
                (self.guild_id, self.channel_id, self.box_id, interaction.user.id),
            ).fetchone()
            if existing:
                return await interaction.response.send_message("Your submission is already recorded.", ephemeral=True)

            con.execute(
                """INSERT INTO trivia_submissions (guild_id, channel_id, box_id, user_id, value_int, submitted_at_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (self.guild_id, self.channel_id, self.box_id, interaction.user.id, int(val), now_ms()),
            )
            con.commit()
        finally:
            con.close()

        await interaction.response.send_message("Submitted.", ephemeral=True)


class NumericAnswerView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int, box_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.box_id = box_id

    @discord.ui.button(label="Answer", style=discord.ButtonStyle.primary, custom_id="wib:numeric_answer")
    async def answer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NumericAnswerModal(self.guild_id, self.channel_id, self.box_id))


class PuzzleModal(discord.ui.Modal, title="Submit Puzzle"):
    w1 = discord.ui.TextInput(label="Word 1", placeholder="FIRST WORD", max_length=32)
    w2 = discord.ui.TextInput(label="Word 2", placeholder="SECOND WORD", max_length=32)
    w3 = discord.ui.TextInput(label="Word 3", placeholder="THIRD WORD", max_length=32)

    def __init__(self, guild_id: int, channel_id: int, box_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.box_id = box_id

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        g1, g2, g3 = norm_word(str(self.w1)), norm_word(str(self.w2)), norm_word(str(self.w3))

        con = db()
        try:
            row = con.execute(
                "SELECT COALESCE(MAX(attempt_id), 0) AS mx FROM puzzle_attempts WHERE guild_id=? AND channel_id=? AND box_id=?",
                (self.guild_id, self.channel_id, self.box_id),
            ).fetchone()
            attempt_id = int(row["mx"]) + 1

            con.execute(
                """INSERT INTO puzzle_attempts (guild_id, channel_id, box_id, attempt_id, user_id, g1, g2, g3, submitted_at_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.guild_id, self.channel_id, self.box_id, attempt_id, user.id, g1, g2, g3, now_ms()),
            )
            con.commit()
        finally:
            con.close()

        channel = interaction.client.get_channel(self.channel_id)
        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title=f"Puzzle Attempt #{attempt_id}",
                description=f"Player: {user.mention}\nGuess: **{g1} {g2} {g3}**\nStatus: Pending host check"
            )
            await channel.send(embed=embed)

        await interaction.response.send_message("Submitted.", ephemeral=True)


class CardPickView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int, box_id: int):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.box_id = box_id
        self._build_buttons()

    def _build_buttons(self):
        con = db()
        try:
            row = con.execute(
                "SELECT deck_json, revealed_json FROM box_secrets WHERE guild_id=? AND channel_id=? AND box_id=?",
                (self.guild_id, self.channel_id, self.box_id),
            ).fetchone()
            if not row:
                return
            deck = json.loads(row["deck_json"])
            revealed = set(json.loads(row["revealed_json"]))
        finally:
            con.close()

        self.clear_items()
        for idx in range(len(deck)):
            disabled = (idx in revealed)
            self.add_item(CardButton(idx=idx, disabled=disabled))

    async def refresh_and_edit(self, message: discord.Message):
        self._build_buttons()
        await message.edit(view=self)


class CardButton(discord.ui.Button):
    def __init__(self, idx: int, disabled: bool):
        super().__init__(
            label=f"Card {idx+1}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"wib:card:{idx}",
            disabled=disabled,
        )
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        con = db()
        try:
            sess = con.execute("SELECT current_box FROM sessions WHERE guild_id=? AND channel_id=?",
                               (guild_id, channel_id)).fetchone()
            if not sess:
                return await interaction.followup.send("No session.", ephemeral=True)
            box_id = int(sess["current_box"])

            slot = con.execute(
                "SELECT slot_user_id, turns_left, pending_action FROM slot_state WHERE guild_id=? AND channel_id=? AND box_id=?",
                (guild_id, channel_id, box_id),
            ).fetchone()
            if not slot or slot["slot_user_id"] is None:
                return await interaction.followup.send("No active slot holder.", ephemeral=True)
            if int(slot["slot_user_id"]) != user_id:
                return await interaction.followup.send("Only the current slot holder can reveal cards.", ephemeral=True)
            if int(slot["turns_left"]) <= 0:
                return await interaction.followup.send("No turns remaining.", ephemeral=True)
            if slot["pending_action"]:
                return await interaction.followup.send("A special action is pending. Wait for the host.", ephemeral=True)

            row = con.execute(
                "SELECT deck_json, revealed_json, phrase_w1, phrase_w2, phrase_w3 FROM box_secrets WHERE guild_id=? AND channel_id=? AND box_id=?",
                (guild_id, channel_id, box_id),
            ).fetchone()
            if not row:
                return await interaction.followup.send("Box deck not found.", ephemeral=True)

            deck = json.loads(row["deck_json"])
            revealed = set(json.loads(row["revealed_json"]))
            if self.idx in revealed:
                return await interaction.followup.send("That card is already revealed.", ephemeral=True)

            card = deck[self.idx]

            revealed.add(self.idx)
            turns_left = int(slot["turns_left"]) - 1
            con.execute(
                "UPDATE box_secrets SET revealed_json=? WHERE guild_id=? AND channel_id=? AND box_id=?",
                (json.dumps(sorted(list(revealed))), guild_id, channel_id, box_id),
            )
            con.execute(
                "UPDATE slot_state SET turns_left=? WHERE guild_id=? AND channel_id=? AND box_id=?",
                (turns_left, guild_id, channel_id, box_id),
            )

            pending_action = None
            channel = interaction.client.get_channel(channel_id)

            if card["type"] == "PIECE":
                w1, w2, w3 = row["phrase_w1"], row["phrase_w2"], row["phrase_w3"]
                reveal = card.get("reveal")
                word = w1 if reveal == "W1" else (w2 if reveal == "W2" else w3)
                if isinstance(channel, discord.TextChannel):
                    await channel.send(embed=discord.Embed(
                        title=f"Box {box_id} — Puzzle Piece Revealed",
                        description=f"Revealed word: **{word}**\nTurns left: **{turns_left}**"
                    ))
            elif card["type"] in ("PASS", "STEAL", "DONATE"):
                pending_action = card["type"]
                if isinstance(channel, discord.TextChannel):
                    await channel.send(embed=discord.Embed(
                        title=f"Special Card Revealed: {pending_action}",
                        description="Host must trigger the selection UI."
                    ))
            elif card["type"] == "WILDCARD":
                effects = ["PASS", "STEAL", "BONUS_TURN"]
                if box_id == 6:
                    effects.append("DONATE")
                chosen = random.choice(effects)
                if chosen == "BONUS_TURN":
                    turns_left = min(5, turns_left + 1)
                    con.execute(
                        "UPDATE slot_state SET turns_left=? WHERE guild_id=? AND channel_id=? AND box_id=?",
                        (turns_left, guild_id, channel_id, box_id),
                    )
                    if isinstance(channel, discord.TextChannel):
                        await channel.send(embed=discord.Embed(
                            title="Wildcard Resolved",
                            description=f"Effect: **BONUS TURN**\nTurns left: **{turns_left}**"
                        ))
                else:
                    pending_action = chosen
                    if isinstance(channel, discord.TextChannel):
                        await channel.send(embed=discord.Embed(
                            title=f"Wildcard Resolved",
                            description=f"Effect: **{chosen}**\nHost must trigger the selection UI."
                        ))

            if pending_action:
                con.execute(
                    "UPDATE slot_state SET pending_action=? WHERE guild_id=? AND channel_id=? AND box_id=?",
                    (pending_action, guild_id, channel_id, box_id),
                )

            con.commit()
        finally:
            con.close()

        try:
            if isinstance(interaction.message, discord.Message) and isinstance(interaction.view, CardPickView):
                await interaction.view.refresh_and_edit(interaction.message)
        except Exception:
            pass

        await interaction.followup.send("Done.", ephemeral=True)


class SubmitPuzzleView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int, box_id: int):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.box_id = box_id

    @discord.ui.button(label="Submit Puzzle", style=discord.ButtonStyle.primary)
    async def submit_puzzle(self, interaction: discord.Interaction, button: discord.ui.Button):
        con = db()
        try:
            slot = con.execute(
                "SELECT slot_user_id FROM slot_state WHERE guild_id=? AND channel_id=? AND box_id=?",
                (self.guild_id, self.channel_id, self.box_id),
            ).fetchone()
            if not slot or slot["slot_user_id"] is None or int(slot["slot_user_id"]) != interaction.user.id:
                return await interaction.response.send_message("Only the current slot holder can submit.", ephemeral=True)
        finally:
            con.close()

        await interaction.response.send_modal(PuzzleModal(self.guild_id, self.channel_id, self.box_id))


# -----------------------------
# Bot / Commands
# -----------------------------

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
class WIB(app_commands.Group):
    def __init__(self):
        super().__init__(name="wib", description="What's in the Box controls")

wib = WIB()
bot.tree.add_command(wib)


def ensure_session(con: sqlite3.Connection, guild_id: int, channel_id: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?", (guild_id, channel_id)).fetchone()
    if row:
        return row
    seed = random.randint(100000, 999999)
    con.execute(
        """INSERT INTO sessions (guild_id, channel_id, session_seed, is_locked, current_box, opened_boxes_count, eliminations_unlocked, created_at_ms)
           VALUES (?, ?, ?, 0, 1, 0, 0, ?)""",
        (guild_id, channel_id, seed, now_ms()),
    )
    con.commit()
    return con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?", (guild_id, channel_id)).fetchone()

def get_participant_count(con: sqlite3.Connection, guild_id: int, channel_id: int) -> int:
    row = con.execute(
        "SELECT COUNT(*) AS c FROM participants WHERE guild_id=? AND channel_id=? AND eliminated=0",
        (guild_id, channel_id),
    ).fetchone()
    return int(row["c"])

def is_registered(con: sqlite3.Connection, guild_id: int, channel_id: int, user_id: int) -> bool:
    row = con.execute(
        "SELECT eliminated FROM participants WHERE guild_id=? AND channel_id=? AND user_id=?",
        (guild_id, channel_id, user_id),
    ).fetchone()
    return bool(row) and int(row["eliminated"]) == 0

def ensure_box_secret(con: sqlite3.Connection, guild_id: int, channel_id: int, seed: int, box_id: int):
    row = con.execute(
        "SELECT 1 FROM box_secrets WHERE guild_id=? AND channel_id=? AND box_id=?",
        (guild_id, channel_id, box_id),
    ).fetchone()
    if row:
        return
    phrase, deck = gen_phrase_and_deck(seed, box_id)
    con.execute(
        """INSERT INTO box_secrets (guild_id, channel_id, box_id, phrase_w1, phrase_w2, phrase_w3, deck_json, revealed_json, created_at_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (guild_id, channel_id, box_id, phrase[0], phrase[1], phrase[2], json.dumps(deck), json.dumps([]), now_ms()),
    )
    con.execute(
        """INSERT OR IGNORE INTO slot_state (guild_id, channel_id, box_id, slot_user_id, turns_left, pending_action, pending_msg_id)
           VALUES (?, ?, ?, NULL, 0, NULL, NULL)""",
        (guild_id, channel_id, box_id),
    )
    con.commit()

def compute_trivia_winner(con: sqlite3.Connection, guild_id: int, channel_id: int, box_id: int, correct: int) -> Optional[int]:
    subs = con.execute(
        """SELECT user_id, value_int, submitted_at_ms
           FROM trivia_submissions
           WHERE guild_id=? AND channel_id=? AND box_id=?""",
        (guild_id, channel_id, box_id),
    ).fetchall()
    if not subs:
        return None

    exacts = [r for r in subs if int(r["value_int"]) == correct]
    if exacts:
        exacts.sort(key=lambda r: int(r["submitted_at_ms"]))
        return int(exacts[0]["user_id"])

    scored = []
    for r in subs:
        diff = abs(int(r["value_int"]) - correct)
        scored.append((diff, int(r["submitted_at_ms"]), int(r["user_id"])))
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[0][2]

def compute_trivia_outcome(
    con: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    box_id: int,
    correct: int,
) -> Optional[Tuple[int, int, bool]]:
    subs = con.execute(
        """SELECT user_id, value_int, submitted_at_ms
           FROM trivia_submissions
           WHERE guild_id=? AND channel_id=? AND box_id=?""",
        (guild_id, channel_id, box_id),
    ).fetchall()
    if not subs:
        return None

    exacts = [r for r in subs if int(r["value_int"]) == correct]
    if exacts:
        exacts.sort(key=lambda r: int(r["submitted_at_ms"]))
        top = exacts[0]
        return int(top["user_id"]), int(top["value_int"]), True

    scored = []
    for r in subs:
        diff = abs(int(r["value_int"]) - correct)
        scored.append((diff, int(r["submitted_at_ms"]), r))
    scored.sort(key=lambda t: (t[0], t[1]))
    top = scored[0][2]
    return int(top["user_id"]), int(top["value_int"]), False


def compute_puzzle_position_score(correct_words: Tuple[str, str, str], guess_words: Tuple[str, str, str]) -> int:
    return sum(1 for i in range(3) if correct_words[i] == guess_words[i])

def next_closest_puzzle_attempt(con: sqlite3.Connection, guild_id: int, channel_id: int, box_id: int, correct_words: Tuple[str,str,str], exclude_attempt_id: int) -> Optional[sqlite3.Row]:
    attempts = con.execute(
        """SELECT * FROM puzzle_attempts
           WHERE guild_id=? AND channel_id=? AND box_id=? AND checked=0 AND attempt_id<>?""",
        (guild_id, channel_id, box_id, exclude_attempt_id),
    ).fetchall()
    if not attempts:
        return None
    scored = []
    for a in attempts:
        g = (a["g1"], a["g2"], a["g3"])
        score = compute_puzzle_position_score(correct_words, g)
        scored.append((score, int(a["submitted_at_ms"]), a))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][2]

async def post_boxes_leaderboard(channel: discord.TextChannel, guild_id: int, channel_id: int):
    con = db()
    try:
        rows = con.execute(
            "SELECT box_id, owner_user_id FROM box_ownership WHERE guild_id=? AND channel_id=? ORDER BY box_id ASC",
            (guild_id, channel_id),
        ).fetchall()
        if not rows:
            await channel.send(embed=discord.Embed(title="Boxes Owned", description="No boxes have been opened yet."))
            return
        owners: Dict[int, List[int]] = {}
        for r in rows:
            owners.setdefault(int(r["owner_user_id"]), []).append(int(r["box_id"]))
        lines = []
        for uid, boxes in sorted(owners.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            member = channel.guild.get_member(uid)
            name = member.mention if member else f"<@{uid}>"
            boxes_str = ", ".join(f"Box {b}" for b in boxes)
            lines.append(f"{name} — **{len(boxes)}** ({boxes_str})")
        emb = discord.Embed(title="Boxes Owned Leaderboard", description="\n".join(lines))
        await channel.send(embed=emb)
    finally:
        con.close()


@wib.command(name="lobby", description="Open the session lobby (one-time) with Join button.")
async def lobby(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)

    con = db()
    try:
        sess = ensure_session(con, interaction.guild_id, interaction.channel_id)
        if int(sess["is_locked"]) == 1:
            return await interaction.response.send_message("Session is already locked.", ephemeral=True)
    finally:
        con.close()

    view = JoinView(bot, interaction.guild_id, interaction.channel_id)
    emb = discord.Embed(title="Session Registration", description="Click **Join** to register for this session.\nHost will lock entries when ready.")
    await interaction.response.send_message(embed=emb, view=view)
    msg = await interaction.original_response()

    con = db()
    try:
        con.execute(
            "UPDATE sessions SET lobby_msg_id=? WHERE guild_id=? AND channel_id=?",
            (msg.id, interaction.guild_id, interaction.channel_id),
        )
        con.commit()
    finally:
        con.close()

@wib.command(name="lock", description="Lock session entries (one-time).")
async def lock(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)
        
    lobby_msg_id = None
    con = db()
    try:
        sess = ensure_session(con, interaction.guild_id, interaction.channel_id)
        if int(sess["is_locked"]) == 1:
            return await interaction.response.send_message("Entries already locked.", ephemeral=True)
        con.execute(
            "UPDATE sessions SET is_locked=1 WHERE guild_id=? AND channel_id=?",
            (interaction.guild_id, interaction.channel_id),
        )
        con.commit()
        seed = int(sess["session_seed"])
        pcount = get_participant_count(con, interaction.guild_id, interaction.channel_id)
        lobby_msg_id = sess["lobby_msg_id"]
        ensure_box_secret(con, interaction.guild_id, interaction.channel_id, seed, 1)
    finally:
        con.close()
        
    if lobby_msg_id:
        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(int(lobby_msg_id))
                await msg.edit(view=JoinView(bot, interaction.guild_id, interaction.channel_id, locked=True))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    await interaction.response.send_message(f"Entries locked. Registered players: **{pcount}**.\nSession seed locked.")


@bot.tree.command(name="wib_q", description="Preview a question for the current box.")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def wib_q(interaction: discord.Interaction):
    # --- this is the SAME body as your existing /wib q_numeric ---
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)

    con = db()
    try:
        sess = ensure_session(con, interaction.guild_id, interaction.channel_id)
        if int(sess["is_locked"]) != 1:
            return await interaction.response.send_message("Lock entries first.", ephemeral=True)
        box_id = int(sess["current_box"])
        pcount = get_participant_count(con, interaction.guild_id, interaction.channel_id)
        seed = int(sess["session_seed"])
    finally:
        con.close()

    async def do_preview(ix: discord.Interaction, salt: int = 0, edit_response: bool = False):
        q, ans = gen_numeric_question(seed + salt, box_id, pcount)
        emb = discord.Embed(title=f"Question Preview (Box {box_id})", description=q)
        emb.add_field(name="Answer (host only)", value=str(ans), inline=False)

        async def on_publish(pix: discord.Interaction):
            if pix.user.id != interaction.user.id:
                return await pix.response.send_message("Only the host who generated this preview can publish it.", ephemeral=True)

            con2 = db()
            try:
                con2.execute(
                    """INSERT INTO trivia_rounds (guild_id, channel_id, box_id, q_text, answer_int, is_active, created_at_ms)
                       VALUES (?, ?, ?, ?, ?, 1, ?)
                       ON CONFLICT(guild_id, channel_id, box_id) DO UPDATE SET
                         q_text=excluded.q_text, answer_int=excluded.answer_int, is_active=1, created_at_ms=excluded.created_at_ms""",
                    (pix.guild_id, pix.channel_id, box_id, q, ans, now_ms()),
                )
                con2.execute(
                    "DELETE FROM trivia_submissions WHERE guild_id=? AND channel_id=? AND box_id=?",
                    (pix.guild_id, pix.channel_id, box_id),
                )
                con2.commit()
            finally:
                con2.close()

            msg = await pix.channel.send(
                embed=discord.Embed(
                    title=f"Box {box_id} — Question",
                    description=f"{q}\n\nClick **Answer** to submit.\n(Registered players only. First submission counts.)"
                ),
                view=NumericAnswerView(pix.guild_id, pix.channel_id, box_id)
            )

            con3 = db()
            try:
                con3.execute(
                    "UPDATE trivia_rounds SET published_msg_id=? WHERE guild_id=? AND channel_id=? AND box_id=?",
                    (msg.id, pix.guild_id, pix.channel_id, box_id),
                )
                con3.commit()
            finally:
                con3.close()

            await pix.response.send_message("Published.", ephemeral=True)

        async def on_regen(rix: discord.Interaction):
            if rix.user.id != interaction.user.id:
                return await rix.response.send_message("Only the host who generated this preview can regenerate it.", ephemeral=True)
            await do_preview(rix, salt=random.randint(1, 99999), edit_response=True)

        async def on_cancel(cix: discord.Interaction):
            if cix.user.id != interaction.user.id:
                return await cix.response.send_message("Only the host who generated this preview can cancel it.", ephemeral=True)
            await cix.response.send_message("Cancelled.", ephemeral=True)

        view = PreviewPublishView(on_publish, on_regen, on_cancel)
        if edit_response or ix.response.is_done():
            await ix.edit_original_response(embed=emb, view=view)
        else:
            await ix.response.send_message(embed=emb, view=view, ephemeral=True)
            
    await do_preview(interaction, salt=random.randint(0, 9999))

# Keep /wib num as fallback (button flow is primary)
@wib.command(name="num", description="(Fallback) Submit your answer for the active question.")
@app_commands.describe(value="Your answer")
async def num(interaction: discord.Interaction, value: int):
    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess or int(sess["is_locked"]) != 1:
            return await interaction.response.send_message("No active locked session in this channel.", ephemeral=True)
        if not is_registered(con, interaction.guild_id, interaction.channel_id, interaction.user.id):
            return await interaction.response.send_message("You are not registered (or you are eliminated).", ephemeral=True)

        box_id = int(sess["current_box"])
        tr = con.execute(
            "SELECT is_active FROM trivia_rounds WHERE guild_id=? AND channel_id=? AND box_id=?",
            (interaction.guild_id, interaction.channel_id, box_id),
        ).fetchone()
        if not tr or int(tr["is_active"]) != 1:
            return await interaction.response.send_message("No active question right now.", ephemeral=True)

        existing = con.execute(
            "SELECT 1 FROM trivia_submissions WHERE guild_id=? AND channel_id=? AND box_id=? AND user_id=?",
            (interaction.guild_id, interaction.channel_id, box_id, interaction.user.id),
        ).fetchone()
        if existing:
            return await interaction.response.send_message("Your submission is already recorded.", ephemeral=True)

        con.execute(
            """INSERT INTO trivia_submissions (guild_id, channel_id, box_id, user_id, value_int, submitted_at_ms)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (interaction.guild_id, interaction.channel_id, box_id, interaction.user.id, int(value), now_ms()),
        )
        con.commit()
    finally:
        con.close()

    await interaction.response.send_message("Submitted.", ephemeral=True)


@wib.command(name="reveal", description="Host: reveal winner and assign slot.")
async def reveal(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)

    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess or int(sess["is_locked"]) != 1:
            return await interaction.response.send_message("No active locked session.", ephemeral=True)
        box_id = int(sess["current_box"])
        tr = con.execute(
            "SELECT answer_int, is_active FROM trivia_rounds WHERE guild_id=? AND channel_id=? AND box_id=?",
            (interaction.guild_id, interaction.channel_id, box_id),
        ).fetchone()
        if not tr or int(tr["is_active"]) != 1:
            return await interaction.response.send_message("No active question.", ephemeral=True)

        correct = int(tr["answer_int"])
        outcome = compute_trivia_outcome(con, interaction.guild_id, interaction.channel_id, box_id, correct)
        winner_id = outcome[0] if outcome else None
        
        con.execute(
            "UPDATE trivia_rounds SET is_active=0 WHERE guild_id=? AND channel_id=? AND box_id=?",
            (interaction.guild_id, interaction.channel_id, box_id),
        )
        con.execute(
            """INSERT INTO slot_state (guild_id, channel_id, box_id, slot_user_id, turns_left, pending_action, pending_msg_id)
               VALUES (?, ?, ?, ?, 0, NULL, NULL)
               ON CONFLICT(guild_id, channel_id, box_id) DO UPDATE SET slot_user_id=excluded.slot_user_id, turns_left=0, pending_action=NULL, pending_msg_id=NULL""",
            (interaction.guild_id, interaction.channel_id, box_id, winner_id),
        )
        con.commit()
    finally:
        con.close()

    if not winner_id:
        return await interaction.response.send_message("No submissions. No slot assigned.", ephemeral=False)

    winner = interaction.guild.get_member(winner_id)
    winner_mention = winner.mention if winner else f"<@{winner_id}>"
    correct_line = f"The correct answer is **{correct}**."
    await interaction.response.send_message(embed=discord.Embed(
        title=f"Box {box_id} — Answer Reveal",
        description=correct_line
    ))

    await asyncio.sleep(1.2)

    winner_value = outcome[1] if outcome else None
    was_exact = outcome[2] if outcome else False
    if was_exact:
        result_line = f"{winner_mention} got it exactly right with **{winner_value}**!"
    else:
        result_line = f"No exact answers. {winner_mention} was closest with **{winner_value}**."

    await interaction.followup.send(embed=discord.Embed(
        title=f"Box {box_id} — Winner",
        description=result_line
    ))

    await asyncio.sleep(0.8)
    await interaction.followup.send(embed=discord.Embed(
        title=f"Box {box_id} — Slot Assigned",
        description=f"Slot holder: {winner_mention}\n\nHost: generate the arrange question with **/wib q_order**."
    ))
       

@wib.command(name="q_order", description="Preview an arrange question for the slot holder.")
async def q_order(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)

    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess or int(sess["is_locked"]) != 1:
            return await interaction.response.send_message("No active locked session.", ephemeral=True)
        box_id = int(sess["current_box"])
        seed = int(sess["session_seed"])
        slot = con.execute(
            "SELECT slot_user_id FROM slot_state WHERE guild_id=? AND channel_id=? AND box_id=?",
            (interaction.guild_id, interaction.channel_id, box_id),
        ).fetchone()
        if not slot or slot["slot_user_id"] is None:
            return await interaction.response.send_message("No slot holder assigned yet.", ephemeral=True)
        slot_user_id = int(slot["slot_user_id"])
    finally:
        con.close()

    async def do_preview(ix: discord.Interaction, salt: int = 0, edit_response: bool = False):
        prompt, items, correct = gen_order_question(seed + salt, box_id)
        emb = discord.Embed(title=f"Arrange Question Preview (Box {box_id})", description=prompt)
        emb.add_field(name="Correct order (host only)", value=" ".join(chr(65+i) for i in correct), inline=False)
        emb.add_field(name="Slot holder", value=f"<@{slot_user_id}>", inline=False)

        async def on_publish(pix: discord.Interaction):
            if pix.user.id != interaction.user.id:
                return await pix.response.send_message("Only the host who generated this preview can publish it.", ephemeral=True)
            con2 = db()
            try:
                con2.execute(
                    """INSERT INTO order_rounds (guild_id, channel_id, box_id, slot_user_id, prompt, items_json, correct_order_json, is_active, created_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                       ON CONFLICT(guild_id, channel_id, box_id) DO UPDATE SET
                         slot_user_id=excluded.slot_user_id, prompt=excluded.prompt, items_json=excluded.items_json,
                         correct_order_json=excluded.correct_order_json, is_active=1, created_at_ms=excluded.created_at_ms""",
                    (pix.guild_id, pix.channel_id, box_id, slot_user_id, prompt, json.dumps(items), json.dumps(correct), now_ms()),
                )
                con2.commit()
            finally:
                con2.close()

            msg = await pix.channel.send(embed=discord.Embed(
                title=f"Box {box_id} — Arrange Question",
                description=f"{prompt}\n\nOnly the slot holder may answer using **/wib order A B C D E**."

            ))
            con3 = db()
            try:
                con3.execute(
                    "UPDATE order_rounds SET published_msg_id=? WHERE guild_id=? AND channel_id=? AND box_id=?",
                    (msg.id, pix.guild_id, pix.channel_id, box_id),
                )
                con3.commit()
            finally:
                con3.close()

            await pix.response.send_message("Published.", ephemeral=True)

        async def on_regen(rix: discord.Interaction):
            if rix.user.id != interaction.user.id:
                return await rix.response.send_message("Only the host who generated this preview can regenerate it.", ephemeral=True)
            await do_preview(rix, salt=random.randint(1, 99999), edit_response=True)
            
        async def on_cancel(cix: discord.Interaction):
            if cix.user.id != interaction.user.id:
                return await cix.response.send_message("Only the host who generated this preview can cancel it.", ephemeral=True)
            await cix.response.send_message("Cancelled.", ephemeral=True)

        view = PreviewPublishView(on_publish, on_regen, on_cancel)
        if edit_response or ix.response.is_done():
            await ix.edit_original_response(embed=emb, view=view)
        else:
            await ix.response.send_message(embed=emb, view=view, ephemeral=True)
            
    await do_preview(interaction, salt=random.randint(0, 9999))


@wib.command(name="order", description="Slot holder submits the order (A B C D E).")
@app_commands.describe(a="First", b="Second", c="Third", d="Fourth", e="Fifth")
async def order(interaction: discord.Interaction, a: str, b: str, c: str, d: str, e: str):
    letters = [a, b, c, d, e]
    letters = [x.strip().upper() for x in letters]
    if sorted(letters) != ["A", "B", "C", "D", "E"]:
        return await interaction.response.send_message("Invalid order. Use each of A B C D E exactly once.", ephemeral=True)

    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess or int(sess["is_locked"]) != 1:
            return await interaction.response.send_message("No active locked session.", ephemeral=True)
        box_id = int(sess["current_box"])
        orow = con.execute(
            "SELECT is_active, slot_user_id, correct_order_json FROM order_rounds WHERE guild_id=? AND channel_id=? AND box_id=?",
            (interaction.guild_id, interaction.channel_id, box_id),
        ).fetchone()
        if not orow or int(orow["is_active"]) != 1:
            return await interaction.response.send_message("No active arrange question.", ephemeral=True)
        slot_user_id = int(orow["slot_user_id"])
        if interaction.user.id != slot_user_id:
            return await interaction.response.send_message("Only the current slot holder may answer.", ephemeral=True)

        correct = json.loads(orow["correct_order_json"])
        submitted_indices = [ord(x) - 65 for x in letters]
        turns = sum(1 for i in range(5) if submitted_indices[i] == correct[i])

        con.execute(
            "UPDATE order_rounds SET is_active=0 WHERE guild_id=? AND channel_id=? AND box_id=?",
            (interaction.guild_id, interaction.channel_id, box_id),
        )
        con.execute(
            "UPDATE slot_state SET turns_left=?, pending_action=NULL WHERE guild_id=? AND channel_id=? AND box_id=?",
            (turns, interaction.guild_id, interaction.channel_id, box_id),
        )
        con.commit()
    finally:
        con.close()

    channel = interaction.channel
    if isinstance(channel, discord.TextChannel):
        await channel.send(embed=discord.Embed(
            title=f"Box {box_id} — Turns Awarded",
            description=f"{interaction.user.mention} earned **{turns}** turn(s).\n\nUse your turns to reveal cards."
        ))
        if turns > 0:
            await channel.send(
                embed=discord.Embed(title=f"Box {box_id} — Card Selection", description="Slot holder: pick a card to reveal."),
                view=CardPickView(interaction.guild_id, interaction.channel_id, box_id),
            )
            await channel.send(
                embed=discord.Embed(title=f"Box {box_id} — Puzzle Submission", description="Slot holder: submit when ready (host will check)."),
                view=SubmitPuzzleView(interaction.guild_id, interaction.channel_id, box_id),
            )

    await interaction.response.send_message("Recorded.", ephemeral=True)


# ---- Host UIs: PASS / STEAL / DONATE and other commands ----
# (These sections are unchanged from the prior build; kept intact for stability.)

@wib.command(name="pass_show", description="Host: show PASS selection UI (slot holder chooses who to pass to).")
async def pass_show(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)

    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess:
            return await interaction.response.send_message("No session.", ephemeral=True)
        box_id = int(sess["current_box"])
        slot = con.execute("SELECT slot_user_id, pending_action FROM slot_state WHERE guild_id=? AND channel_id=? AND box_id=?",
                           (interaction.guild_id, interaction.channel_id, box_id)).fetchone()
        if not slot or slot["pending_action"] != "PASS" or slot["slot_user_id"] is None:
            return await interaction.response.send_message("No PASS pending.", ephemeral=True)
        slot_user_id = int(slot["slot_user_id"])

        players = con.execute(
            "SELECT user_id, display_name FROM participants WHERE guild_id=? AND channel_id=? AND eliminated=0 AND user_id<>? ORDER BY display_name COLLATE NOCASE ASC",
            (interaction.guild_id, interaction.channel_id, slot_user_id),
        ).fetchall()
    finally:
        con.close()

    if not players:
        return await interaction.response.send_message("No eligible players to pass to.", ephemeral=True)

    class PassSelect(discord.ui.Select):
        def __init__(self):
            options = [discord.SelectOption(label=p["display_name"][:100], value=str(p["user_id"])) for p in players[:25]]
            super().__init__(placeholder="Select player to receive the slot", min_values=1, max_values=1, options=options)

        async def callback(self, ix: discord.Interaction):
            await ix.response.defer(ephemeral=True)
            if ix.user.id != slot_user_id:
                return await ix.followup.send("Only the slot holder can choose.", ephemeral=True)
            target_id = int(self.values[0])

            con2 = db()
            try:
                con2.execute(
                    "UPDATE slot_state SET slot_user_id=?, turns_left=0, pending_action=NULL WHERE guild_id=? AND channel_id=? AND box_id=?",
                    (target_id, ix.guild_id, ix.channel_id, box_id),
                )
                con2.commit()
            finally:
                con2.close()

            for child in self.view.children:
                child.disabled = True
            await ix.message.edit(view=self.view)

            if isinstance(ix.channel, discord.TextChannel):
                await ix.channel.send(embed=discord.Embed(title="Slot Passed", description=f"Slot moved to <@{target_id}>."))
            await ix.followup.send("Done.", ephemeral=True)

    view = discord.ui.View(timeout=300)
    view.add_item(PassSelect())
    await interaction.response.send_message(
        embed=discord.Embed(title="PASS Selection", description=f"Only <@{slot_user_id}> may select."),
        view=view
    )

@wib.command(name="steal_show", description="Host: show STEAL buttons (slot holder steals an opened box).")
async def steal_show(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)

    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess:
            return await interaction.response.send_message("No session.", ephemeral=True)
        box_id = int(sess["current_box"])
        slot = con.execute("SELECT slot_user_id, pending_action FROM slot_state WHERE guild_id=? AND channel_id=? AND box_id=?",
                           (interaction.guild_id, interaction.channel_id, box_id)).fetchone()
        if not slot or slot["pending_action"] != "STEAL" or slot["slot_user_id"] is None:
            return await interaction.response.send_message("No STEAL pending.", ephemeral=True)
        slot_user_id = int(slot["slot_user_id"])

        rows = con.execute(
            """SELECT o.box_id, o.owner_user_id
               FROM box_ownership o
               WHERE o.guild_id=? AND o.channel_id=? AND o.box_id BETWEEN 1 AND 5
               ORDER BY o.box_id ASC""",
            (interaction.guild_id, interaction.channel_id),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return await interaction.response.send_message("No eligible boxes to steal yet.", ephemeral=True)

    class StealButton(discord.ui.Button):
        def __init__(self, box_to_steal: int, owner_id: int):
            super().__init__(label=f"Steal Box {box_to_steal}", style=discord.ButtonStyle.danger)
            self.box_to_steal = box_to_steal
            self.owner_id = owner_id

        async def callback(self, ix: discord.Interaction):
            await ix.response.defer(ephemeral=True)
            if ix.user.id != slot_user_id:
                return await ix.followup.send("Only the slot holder can steal.", ephemeral=True)
            if self.owner_id == slot_user_id:
                return await ix.followup.send("You already own that box.", ephemeral=True)

            con2 = db()
            try:
                con2.execute(
                    "UPDATE box_ownership SET owner_user_id=?, updated_at_ms=? WHERE guild_id=? AND channel_id=? AND box_id=?",
                    (slot_user_id, now_ms(), ix.guild_id, ix.channel_id, self.box_to_steal),
                )
                con2.execute(
                    "UPDATE slot_state SET pending_action=NULL WHERE guild_id=? AND channel_id=? AND box_id=?",
                    (ix.guild_id, ix.channel_id, box_id),
                )
                con2.commit()
            finally:
                con2.close()

            for child in self.view.children:
                child.disabled = True
            await ix.message.edit(view=self.view)

            if isinstance(ix.channel, discord.TextChannel):
                await ix.channel.send(embed=discord.Embed(
                    title="STEAL Executed",
                    description=f"<@{slot_user_id}> stole **Box {self.box_to_steal}** from <@{self.owner_id}>."
                ))
                await post_boxes_leaderboard(ix.channel, ix.guild_id, ix.channel_id)

            await ix.followup.send("Done.", ephemeral=True)

    class StealView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            for r in rows:
                self.add_item(StealButton(int(r["box_id"]), int(r["owner_user_id"])))

    lines = [f"Box {int(r['box_id'])} — owner: <@{int(r['owner_user_id'])}>" for r in rows]
    emb = discord.Embed(title="STEAL Selection", description="\n".join(lines) + f"\n\nOnly <@{slot_user_id}> may select.")
    await interaction.response.send_message(embed=emb, view=StealView())

@wib.command(name="donate_show", description="Host: show DONATE UI (Mega only). Slot holder donates a box they own.")
async def donate_show(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)

    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess:
            return await interaction.response.send_message("No session.", ephemeral=True)
        box_id = int(sess["current_box"])
        if box_id != 6:
            return await interaction.response.send_message("DONATE is Mega-only.", ephemeral=True)

        slot = con.execute("SELECT slot_user_id, pending_action FROM slot_state WHERE guild_id=? AND channel_id=? AND box_id=?",
                           (interaction.guild_id, interaction.channel_id, box_id)).fetchone()
        if not slot or slot["pending_action"] != "DONATE" or slot["slot_user_id"] is None:
            return await interaction.response.send_message("No DONATE pending.", ephemeral=True)
        slot_user_id = int(slot["slot_user_id"])

        owned = con.execute(
            """SELECT box_id FROM box_ownership
               WHERE guild_id=? AND channel_id=? AND owner_user_id=? AND box_id BETWEEN 1 AND 5
               ORDER BY box_id ASC""",
            (interaction.guild_id, interaction.channel_id, slot_user_id),
        ).fetchall()

        recipients = con.execute(
            "SELECT user_id, display_name FROM participants WHERE guild_id=? AND channel_id=? AND eliminated=0 AND user_id<>? ORDER BY display_name COLLATE NOCASE ASC",
            (interaction.guild_id, interaction.channel_id, slot_user_id),
        ).fetchall()
    finally:
        con.close()

    if not owned:
        return await interaction.response.send_message("Slot holder owns no eligible boxes to donate.", ephemeral=True)
    if not recipients:
        return await interaction.response.send_message("No eligible recipients.", ephemeral=True)

    owned_boxes = [int(r["box_id"]) for r in owned]

    class DonateState:
        chosen_box: Optional[int] = None

    state = DonateState()

    class BoxSelect(discord.ui.Select):
        def __init__(self):
            options = [discord.SelectOption(label=f"Box {b}", value=str(b)) for b in owned_boxes]
            super().__init__(placeholder="Select a box to donate", min_values=1, max_values=1, options=options)

        async def callback(self, ix: discord.Interaction):
            await ix.response.defer(ephemeral=True)
            if ix.user.id != slot_user_id:
                return await ix.followup.send("Only the slot holder can donate.", ephemeral=True)
            state.chosen_box = int(self.values[0])
            await ix.followup.send(f"Selected Box {state.chosen_box}. Now choose a recipient.", ephemeral=True)

    class RecipientSelect(discord.ui.Select):
        def __init__(self):
            options = [discord.SelectOption(label=r["display_name"][:100], value=str(r["user_id"])) for r in recipients[:25]]
            super().__init__(placeholder="Select a recipient", min_values=1, max_values=1, options=options)

        async def callback(self, ix: discord.Interaction):
            await ix.response.defer(ephemeral=True)
            if ix.user.id != slot_user_id:
                return await ix.followup.send("Only the slot holder can donate.", ephemeral=True)
            if state.chosen_box is None:
                return await ix.followup.send("Select a box first.", ephemeral=True)
            target_id = int(self.values[0])
            chosen_box = int(state.chosen_box)

            con2 = db()
            try:
                con2.execute(
                    "UPDATE box_ownership SET owner_user_id=?, updated_at_ms=? WHERE guild_id=? AND channel_id=? AND box_id=? AND owner_user_id=?",
                    (target_id, now_ms(), ix.guild_id, ix.channel_id, chosen_box, slot_user_id),
                )
                con2.execute(
                    "UPDATE slot_state SET pending_action=NULL WHERE guild_id=? AND channel_id=? AND box_id=?",
                    (ix.guild_id, ix.channel_id, box_id),
                )
                con2.commit()
            finally:
                con2.close()

            for child in self.view.children:
                child.disabled = True
            await ix.message.edit(view=self.view)

            if isinstance(ix.channel, discord.TextChannel):
                await ix.channel.send(embed=discord.Embed(
                    title="DONATE Completed",
                    description=f"<@{slot_user_id}> donated **Box {chosen_box}** to <@{target_id}>."
                ))
                await post_boxes_leaderboard(ix.channel, ix.guild_id, ix.channel_id)

            await ix.followup.send("Done.", ephemeral=True)

    view = discord.ui.View(timeout=300)
    view.add_item(BoxSelect())
    view.add_item(RecipientSelect())
    emb = discord.Embed(title="DONATE", description=f"Only <@{slot_user_id}> may donate.\nSelect a box, then select a recipient.")
    await interaction.response.send_message(embed=emb, view=view)

@wib.command(name="check_puzzle", description="Host: check the latest unchecked puzzle attempt for current box.")
async def check_puzzle(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)

    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess:
            return await interaction.response.send_message("No session.", ephemeral=True)
        box_id = int(sess["current_box"])

        secret = con.execute(
            "SELECT phrase_w1, phrase_w2, phrase_w3 FROM box_secrets WHERE guild_id=? AND channel_id=? AND box_id=?",
            (interaction.guild_id, interaction.channel_id, box_id),
        ).fetchone()
        if not secret:
            return await interaction.response.send_message("Box secret not found.", ephemeral=True)
        correct = (secret["phrase_w1"], secret["phrase_w2"], secret["phrase_w3"])

        attempt = con.execute(
            """SELECT * FROM puzzle_attempts
               WHERE guild_id=? AND channel_id=? AND box_id=? AND checked=0
               ORDER BY attempt_id DESC LIMIT 1""",
            (interaction.guild_id, interaction.channel_id, box_id),
        ).fetchone()
        if not attempt:
            return await interaction.response.send_message("No pending puzzle attempts.", ephemeral=True)

        g = (attempt["g1"], attempt["g2"], attempt["g3"])
        score = compute_puzzle_position_score(correct, g)
        solved = (score == 3)

        con.execute(
            "UPDATE puzzle_attempts SET checked=1, score_positions=? WHERE guild_id=? AND channel_id=? AND box_id=? AND attempt_id=?",
            (score, interaction.guild_id, interaction.channel_id, box_id, int(attempt["attempt_id"])),
        )
        if solved:
            con.execute(
                "UPDATE slot_state SET turns_left=0, pending_action=NULL WHERE guild_id=? AND channel_id=? AND box_id=?",
                (interaction.guild_id, interaction.channel_id, box_id),
            )
        con.commit()
    finally:
        con.close()

    if solved:
        return await interaction.response.send_message(embed=discord.Embed(
            title=f"Box {box_id} — Puzzle Check",
            description="Result: ✅ Correct.\n\nMike may open the box with **/wib open_box**."
        ))

    con2 = db()
    try:
        nxt = next_closest_puzzle_attempt(con2, interaction.guild_id, interaction.channel_id, box_id, correct, int(attempt["attempt_id"]))
        if nxt:
            con2.execute(
                "UPDATE slot_state SET slot_user_id=?, turns_left=0, pending_action=NULL WHERE guild_id=? AND channel_id=? AND box_id=?",
                (int(nxt["user_id"]), interaction.guild_id, interaction.channel_id, box_id),
            )
            con2.commit()
    finally:
        con2.close()

    if nxt:
        await interaction.response.send_message(embed=discord.Embed(
            title=f"Box {box_id} — Puzzle Check",
            description=f"Result: ❌ Not solved.\nMatches in correct position: **{score}/3**.\n\nNext slot holder: <@{int(nxt['user_id'])}>.\nHost: generate the arrange question with **/wib q_order**."
        ))
    else:
        await interaction.response.send_message(embed=discord.Embed(
            title=f"Box {box_id} — Puzzle Check",
            description=f"Result: ❌ Not solved.\nMatches in correct position: **{score}/3**.\nNo further pending attempts."
        ))

class PrizeModal(discord.ui.Modal, title="Fill Prize"):
    title_in = discord.ui.TextInput(label="Prize Title", placeholder="Prize name", max_length=120)
    desc_in = discord.ui.TextInput(label="Prize Description", style=discord.TextStyle.paragraph, max_length=800, required=False)

    def __init__(self, guild_id: int, channel_id: int, box_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.box_id = box_id

    async def on_submit(self, interaction: discord.Interaction):
        t = str(self.title_in).strip()
        d = str(self.desc_in).strip()
        con = db()
        try:
            con.execute(
                """INSERT INTO prizes (guild_id, channel_id, box_id, title, description, filled_by, filled_at_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, channel_id, box_id) DO UPDATE SET
                     title=excluded.title, description=excluded.description, filled_by=excluded.filled_by, filled_at_ms=excluded.filled_at_ms""",
                (self.guild_id, self.channel_id, self.box_id, t, d, interaction.user.id, now_ms()),
            )
            con.commit()
        finally:
            con.close()
        await interaction.response.send_message("Prize saved.", ephemeral=True)

@wib.command(name="prize_set", description="Mike only: pre-fill prize for a box (hidden until opened).")
@app_commands.describe(box_id="Box number (1-6)")
async def prize_set(interaction: discord.Interaction, box_id: int):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner permission required.", ephemeral=True)
    if box_id < 1 or box_id > 6:
        return await interaction.response.send_message("Box must be 1-6.", ephemeral=True)
    await interaction.response.send_modal(PrizeModal(interaction.guild_id, interaction.channel_id, box_id))

@wib.command(name="open_box", description="Mike only: open the current box and reveal the prize (prompts if not pre-filled).")
async def open_box(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner permission required.", ephemeral=True)

    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess:
            return await interaction.response.send_message("No session.", ephemeral=True)
        box_id = int(sess["current_box"])

        solved = con.execute(
            """SELECT 1 FROM puzzle_attempts
               WHERE guild_id=? AND channel_id=? AND box_id=? AND checked=1 AND score_positions=3
               LIMIT 1""",
            (interaction.guild_id, interaction.channel_id, box_id),
        ).fetchone()
        if not solved:
            return await interaction.response.send_message("Box is not ready yet (puzzle not confirmed).", ephemeral=True)

        solver = con.execute(
            """SELECT user_id FROM puzzle_attempts
               WHERE guild_id=? AND channel_id=? AND box_id=? AND checked=1 AND score_positions=3
               ORDER BY attempt_id DESC LIMIT 1""",
            (interaction.guild_id, interaction.channel_id, box_id),
        ).fetchone()
        solver_id = int(solver["user_id"]) if solver else interaction.user.id

        prize = con.execute(
            "SELECT title, description FROM prizes WHERE guild_id=? AND channel_id=? AND box_id=?",
            (interaction.guild_id, interaction.channel_id, box_id),
        ).fetchone()

        if not prize or not (prize["title"] or "").strip():
            await interaction.response.send_modal(PrizeModal(interaction.guild_id, interaction.channel_id, box_id))
            return

        con.execute(
            """INSERT INTO box_ownership (guild_id, channel_id, box_id, owner_user_id, updated_at_ms)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, channel_id, box_id) DO UPDATE SET owner_user_id=excluded.owner_user_id, updated_at_ms=excluded.updated_at_ms""",
            (interaction.guild_id, interaction.channel_id, box_id, solver_id, now_ms()),
        )

        opened_boxes_count = int(sess["opened_boxes_count"]) + 1
        next_box = min(6, box_id + 1)
        eliminations_unlocked = 1 if opened_boxes_count >= 3 else int(sess["eliminations_unlocked"])

        con.execute(
            "UPDATE sessions SET opened_boxes_count=?, eliminations_unlocked=?, current_box=? WHERE guild_id=? AND channel_id=?",
            (opened_boxes_count, eliminations_unlocked, next_box, interaction.guild_id, interaction.channel_id),
        )
        con.commit()
    finally:
        con.close()

    channel = interaction.channel
    if isinstance(channel, discord.TextChannel):
        emb = discord.Embed(
            title=f"Box {box_id} Opened",
            description=f"Opened by Mike.\nOwner: <@{solver_id}>\n\n**{prize['title']}**\n{(prize['description'] or '').strip()}"
        )
        await interaction.response.send_message(embed=emb)
        await post_boxes_leaderboard(channel, interaction.guild_id, interaction.channel_id)

        if box_id < 6:
            con2 = db()
            try:
                sess2 = con2.execute("SELECT session_seed FROM sessions WHERE guild_id=? AND channel_id=?",
                                     (interaction.guild_id, interaction.channel_id)).fetchone()
                ensure_box_secret(con2, interaction.guild_id, interaction.channel_id, int(sess2["session_seed"]), box_id + 1)
            finally:
                con2.close()
        else:
            await channel.send(embed=discord.Embed(title="Session Complete", description="Mega Box opened. Session complete."))

@wib.command(name="leaderboard", description="Show boxes owned leaderboard.")
async def leaderboard(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Unsupported channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await post_boxes_leaderboard(interaction.channel, interaction.guild_id, interaction.channel_id)
    await interaction.followup.send("Posted.", ephemeral=True)

@wib.command(name="elim_eligible", description="Host: list eligible players for elimination (only after 3 boxes opened).")
async def elim_eligible(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not has_host_role(interaction.user):
        return await interaction.response.send_message("Host permission required.", ephemeral=True)

    con = db()
    try:
        sess = con.execute("SELECT eliminations_unlocked FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess or int(sess["eliminations_unlocked"]) != 1:
            return await interaction.response.send_message("Eliminations are locked until after 3 boxes are opened.", ephemeral=True)

        owned = con.execute(
            "SELECT owner_user_id, COUNT(*) AS c FROM box_ownership WHERE guild_id=? AND channel_id=? GROUP BY owner_user_id",
            (interaction.guild_id, interaction.channel_id),
        ).fetchall()
        owned_map = {int(r["owner_user_id"]): int(r["c"]) for r in owned}

        players = con.execute(
            "SELECT user_id, display_name FROM participants WHERE guild_id=? AND channel_id=? AND eliminated=0 ORDER BY display_name COLLATE NOCASE ASC",
            (interaction.guild_id, interaction.channel_id),
        ).fetchall()
        eligible = [p for p in players if owned_map.get(int(p["user_id"]), 0) == 0]
    finally:
        con.close()

    if not eligible:
        return await interaction.response.send_message("No eligible players (0 boxes owned).", ephemeral=True)

    desc = "\n".join(f"<@{int(p['user_id'])}> — {p['display_name']}" for p in eligible[:30])
    await interaction.response.send_message(embed=discord.Embed(title="Elimination Eligible (0 boxes owned)", description=desc), ephemeral=False)

@wib.command(name="status", description="Host: show current session status.")
async def status(interaction: discord.Interaction):
    con = db()
    try:
        sess = con.execute("SELECT * FROM sessions WHERE guild_id=? AND channel_id=?",
                           (interaction.guild_id, interaction.channel_id)).fetchone()
        if not sess:
            return await interaction.response.send_message("No session in this channel.", ephemeral=True)
        box_id = int(sess["current_box"])
        pcount = con.execute("SELECT COUNT(*) AS c FROM participants WHERE guild_id=? AND channel_id=? AND eliminated=0",
                             (interaction.guild_id, interaction.channel_id)).fetchone()["c"]
        slot = con.execute("SELECT slot_user_id, turns_left, pending_action FROM slot_state WHERE guild_id=? AND channel_id=? AND box_id=?",
                           (interaction.guild_id, interaction.channel_id, box_id)).fetchone()
        slot_user_id = int(slot["slot_user_id"]) if slot and slot["slot_user_id"] is not None else None
        turns = int(slot["turns_left"]) if slot else 0
        pending = slot["pending_action"] if slot else None

        emb = discord.Embed(title="Session Status")
        emb.add_field(name="Entries locked", value=str(bool(sess["is_locked"])), inline=True)
        emb.add_field(name="Current box", value=f"Box {box_id}" + (" (MEGA)" if box_id == 6 else ""), inline=True)
        emb.add_field(name="Opened boxes", value=str(int(sess["opened_boxes_count"])), inline=True)
        emb.add_field(name="Registered (active)", value=str(int(pcount)), inline=True)
        emb.add_field(name="Slot holder", value=(f"<@{slot_user_id}>" if slot_user_id else "None"), inline=True)
        emb.add_field(name="Turns left", value=str(turns), inline=True)
        emb.add_field(name="Pending action", value=str(pending or "None"), inline=True)
    finally:
        con.close()

    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

bot.run(DISCORD_TOKEN)
