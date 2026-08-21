import os
import re
import io
import time
import asyncio
import logging
import datetime
import threading
import requests
import discord
from collections import deque, defaultdict
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from flask import Flask, jsonify
from groq import Groq

load_dotenv()
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# ── LOGGING ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("3drbxmt-bot")

# Tracks connection state for the /health endpoint
bot_status = {
    "ready": False,
    "last_ready_at": None,
    "last_disconnect_at": None,
    "reconnect_count": 0,
    "latency_ms": None,
}


def get_web_maintenance_state():
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/app_state",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            },
            params={"key": "eq.maintenance_mode", "select": "value"},
            timeout=5
        )
        data = r.json()
        if data and len(data) > 0:
            return data[0].get("value", False)
        return False
    except Exception:
        return None  # None = error/unknown


def set_web_maintenance_state(active):
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/app_state",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            },
            params={"key": "eq.maintenance_mode"},
            json={"value": active},
            timeout=5
        )
        return True
    except Exception:
        return False

# ── FLASK KEEP-ALIVE SERVER ──────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

@app.route("/health")
def health():
    """Real connection status, for uptime pingers to check instead of just '/'."""
    status_code = 200 if bot_status["ready"] else 503
    return jsonify(bot_status), status_code

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    # Disable Flask's default request logging spam in the console
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)
    app.run(host="0.0.0.0", port=port)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

VERIFIED_ROLE_NAME = "Member"  # Ganti sesuai nama role yang mau dikasih setelah verify


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Tombol tidak expire

    @discord.ui.button(label="✅ Verify", style=discord.ButtonStyle.success, custom_id="verify_button")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        if role is None:
            await interaction.response.send_message(
                f"⚠ Role '{VERIFIED_ROLE_NAME}' not found. Contact an admin.",
                ephemeral=True
            )
            return

        if role in member.roles:
            await interaction.response.send_message(
                "✅ You are already verified!",
                ephemeral=True
            )
            return

        try:
            await member.add_roles(role)
            await interaction.response.send_message(
                "🎉 Verification successful! Welcome to the 3DRBXMT server.",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠ The bot doesn't have permission to assign this role. Contact an admin.",
                ephemeral=True
            )

WELCOME_CHANNEL_NAME = "welcome"
PARTNERSHIP_CHANNEL_NAME = "partnership"
partnership_submissions = {}
MODERATOR_ROLE_NAME = "Moderator"
moderator_submissions = {}  # temp storage: submission_id -> data
ALLOWED_YOUTUBE_CHANNEL = "youtube-upload"
YOUTUBE_PATTERNS = ["youtube.com", "youtu.be"]

DISCORD_INVITE_PATTERN = re.compile(
    r"(discord\.gg/|discord(?:app)?\.com/invite/)\S+",
    re.IGNORECASE
)

def has_invite_permission(member: discord.Member) -> bool:
    """Mods, admins, and the Dev are allowed to post invite links."""
    if member.id == DEV_USER_ID:
        return True
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    if any(r.name in PROTECTED_ROLES for r in member.roles):
        return True
    return False


# ── ANTI-RAID / AUTOMOD ──────────────────────────────────────
# Alerts are posted to a text channel named MOD_LOG_CHANNEL_NAME, if it exists.
MOD_LOG_CHANNEL_NAME = "mod-log"

# Mass-join (raid) detection
RAID_JOIN_THRESHOLD = 6          # this many joins...
RAID_JOIN_WINDOW_SECONDS = 15    # ...within this many seconds triggers auto-lockdown

# Message spam automod
# Mass-mention / link-spam automod (targets raid-style @everyone / mass-ping / link-flood
# behavior, not just typing fast — someone explaining something across several messages
# with normal content is never flagged)
SPAM_MENTION_SINGLE_THRESHOLD = 5    # mentions in a single message
SPAM_MENTION_WINDOW_SECONDS = 6
SPAM_MENTION_WINDOW_THRESHOLD = 8    # mentions+links summed across the window
SPAM_TIMEOUT_MINUTES = 10

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

_join_timestamps = defaultdict(deque)       # guild_id -> deque[float]
_message_timestamps = defaultdict(deque)    # (guild_id, user_id) -> deque[(float, int)] (time, mention+link count)
_raid_locked_channels = defaultdict(set)    # guild_id -> set[channel_id] locked by auto/manual raid response
_raid_mode_active = defaultdict(bool)       # guild_id -> bool
_known_webhooks = defaultdict(set)          # guild_id -> set[webhook_id]


# ── AI CHAT (Groq / Llama 4 Scout) ───────────────────────────
AI_CHAT_CHANNEL_NAME = "ai-chat"
GROQ_MODEL = "openai/gpt-oss-20b"  # Llama models were retired from Groq in June 2026; this is Groq's recommended open-weight replacement
AI_SYSTEM_PROMPT = (
    "You are a friendly, helpful assistant chatting in a Discord server. "
    "Keep replies warm, clear, and reasonably concise (a few sentences unless more detail is truly needed). "
    "Avoid Discord markdown headers; plain conversational text is fine. "
    "Never write @everyone, @here, or any user/role mention in your replies, even if asked to — "
    "if someone asks you to ping everyone or a role, politely decline and explain you can't do that."
)
AI_MAX_HISTORY_CHARS = 800  # trims the user's message if it's excessively long

_ai_chat_enabled = defaultdict(lambda: True)  # guild_id -> bool, default ON

_groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


def _groq_chat_sync(user_message: str) -> str:
    """Blocking call to Groq's API — run this off the event loop."""
    completion = _groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_message[:AI_MAX_HISTORY_CHARS]}
        ],
        max_tokens=1500,
        temperature=0.7
    )
    return completion.choices[0].message.content


async def generate_ai_reply(user_message: str) -> str:
    if _groq_client is None:
        return "⚠ AI chat isn't configured yet — a `GROQ_API_KEY` needs to be set."
    try:
        return await asyncio.to_thread(_groq_chat_sync, user_message)
    except Exception as e:
        logger.error(f"Groq chat error: {e}")
        return "⚠ Sorry, I couldn't get a response right now — try again in a moment."


CODE_BLOCK_PATTERN = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
LANG_TO_EXT = {
    "python": "py", "py": "py", "javascript": "js", "js": "js",
    "typescript": "ts", "ts": "ts", "lua": "lua", "html": "html",
    "css": "css", "java": "java", "c": "c", "cpp": "cpp", "c++": "cpp",
    "json": "json", "bash": "sh", "sh": "sh", "shell": "sh",
    "sql": "sql", "go": "go", "rust": "rs", "yaml": "yaml", "yml": "yaml"
}

# Prevents the AI model from being tricked/exploited into pinging @everyone, @here,
# specific roles, or specific users — the model's raw text is untrusted input and
# must never be allowed to trigger real notification pings.
NO_PING = discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=False)


async def send_ai_reply(message: discord.Message, reply_text: str):
    """Sends an AI reply, extracting fenced code blocks into file attachments
    (Discord has no collapsible code panel, so a downloadable file is the
    closest equivalent) and splitting long plain text to stay under the
    2000-character message limit. All sends here use NO_PING so the AI's
    output can never trigger a real @everyone/@here/role/user ping."""
    blocks = CODE_BLOCK_PATTERN.findall(reply_text)

    if blocks:
        remaining_text = CODE_BLOCK_PATTERN.sub("", reply_text).strip()
        files = []
        for i, (lang, code) in enumerate(blocks, start=1):
            ext = LANG_TO_EXT.get(lang.lower().strip(), "txt")
            filename = f"code_{i}.{ext}" if len(blocks) > 1 else f"code.{ext}"
            files.append(discord.File(io.BytesIO(code.strip().encode("utf-8")), filename=filename))

        caption = remaining_text if remaining_text else "Here's the code:"
        if len(caption) > 2000:
            caption = caption[:1997] + "..."
        await message.reply(caption, files=files, mention_author=False, allowed_mentions=NO_PING)
        return

    if len(reply_text) <= 2000:
        await message.reply(reply_text, mention_author=False, allowed_mentions=NO_PING)
        return

    chunks = [reply_text[i:i + 1990] for i in range(0, len(reply_text), 1990)]
    first = True
    for chunk in chunks:
        if first:
            await message.reply(chunk, mention_author=False, allowed_mentions=NO_PING)
            first = False
        else:
            await message.channel.send(chunk, allowed_mentions=NO_PING)


def _fetch_pollinations_image_sync(prompt: str) -> bytes:
    """Blocking call to Pollinations' free, no-key image API — run off the event loop."""
    from urllib.parse import quote
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}"
    r = requests.get(url, params={"width": 1024, "height": 1024, "nologo": "true"}, timeout=60)
    r.raise_for_status()
    return r.content


async def generate_image(prompt: str):
    """Returns image bytes, or None on failure."""
    try:
        return await asyncio.to_thread(_fetch_pollinations_image_sync, prompt)
    except Exception as e:
        logger.error(f"Pollinations image error: {e}")
        return None



def get_mod_log_channel(guild: discord.Guild):
    return discord.utils.get(guild.text_channels, name=MOD_LOG_CHANNEL_NAME)


async def alert_mods(guild: discord.Guild, embed: discord.Embed):
    channel = get_mod_log_channel(guild)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


def is_privileged_member(member: discord.Member) -> bool:
    """Members who should be exempt from automod (mods/admins/dev/protected roles)."""
    if member.id == DEV_USER_ID:
        return True
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
        return True
    if any(r.name in PROTECTED_ROLES for r in member.roles):
        return True
    return False


async def lock_guild(guild: discord.Guild) -> int:
    """Denies send_messages for @everyone in every text channel. Returns count locked.
    Channels that were already restricted before lockdown are left untouched and not
    tracked, so /raidunlock won't reopen something that was meant to stay locked."""
    locked = 0
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            if overwrite.send_messages is not False:
                overwrite.send_messages = False
                await channel.set_permissions(guild.default_role, overwrite=overwrite)
                _raid_locked_channels[guild.id].add(channel.id)
                locked += 1
        except discord.Forbidden:
            continue
    _raid_mode_active[guild.id] = True
    return locked


async def unlock_guild(guild: discord.Guild) -> int:
    """Reverses lock_guild() only for channels it actually locked. Returns count unlocked."""
    unlocked = 0
    for channel_id in list(_raid_locked_channels[guild.id]):
        channel = guild.get_channel(channel_id)
        if channel is None:
            continue
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = None
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
            unlocked += 1
        except discord.Forbidden:
            continue
    _raid_locked_channels[guild.id].clear()
    _raid_mode_active[guild.id] = False
    return unlocked


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # ── Mass-mention / link-spam automod ──
    if message.guild is not None and not is_privileged_member(message.author):
        mention_count = len(message.mentions) + len(message.role_mentions)
        if message.mention_everyone:
            mention_count += 5  # an unauthorized @everyone/@here ping is a strong signal on its own
        link_count = len(URL_PATTERN.findall(message.content))
        this_message_total = mention_count + link_count

        now = time.time()
        key = (message.guild.id, message.author.id)
        dq = _message_timestamps[key]
        dq.append((now, this_message_total))
        while dq and now - dq[0][0] > SPAM_MENTION_WINDOW_SECONDS:
            dq.popleft()
        window_total = sum(count for _, count in dq)

        if mention_count >= SPAM_MENTION_SINGLE_THRESHOLD or window_total >= SPAM_MENTION_WINDOW_THRESHOLD:
            dq.clear()
            member = message.author
            try:
                await member.timeout(
                    datetime.timedelta(minutes=SPAM_TIMEOUT_MINUTES),
                    reason="Automod: mass mentions/links"
                )
            except discord.Forbidden:
                pass
            try:
                warning = await message.channel.send(
                    f"🔇 {member.mention} was timed out for {SPAM_TIMEOUT_MINUTES} minute(s) — mass mentions/links detected."
                )
                await warning.delete(delay=8)
            except discord.Forbidden:
                pass
            await alert_mods(
                message.guild,
                discord.Embed(
                    title="🔇 Automod: Mass Mention/Link Timeout",
                    description=(
                        f"{member.mention} (`{member.id}`) triggered automod in {message.channel.mention} "
                        f"(mentions: {mention_count}, links: {link_count}, "
                        f"{SPAM_MENTION_WINDOW_SECONDS}s window total: {window_total})."
                    ),
                    color=0xFFA500
                )
            )
            return

    # ── AI chat (mention trigger, restricted to #ai-chat) ──
    if (
        message.guild is not None
        and bot.user in message.mentions
        and _ai_chat_enabled[message.guild.id]
    ):
        if message.channel.name != AI_CHAT_CHANNEL_NAME:
            target_channel = discord.utils.get(message.guild.text_channels, name=AI_CHAT_CHANNEL_NAME)
            try:
                notice = await message.reply(
                    f"💬 AI chat only works in "
                    f"{f'<#{target_channel.id}>' if target_channel else f'#{AI_CHAT_CHANNEL_NAME}'} — head over there to chat with me!",
                    mention_author=False
                )
                await notice.delete(delay=8)
            except discord.Forbidden:
                pass
            return

        clean_content = message.content
        for mention in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
            clean_content = clean_content.replace(mention, "")
        clean_content = clean_content.strip()

        if clean_content:
            async with message.channel.typing():
                reply = await generate_ai_reply(clean_content)
            await send_ai_reply(message, reply)
            return

    if DISCORD_INVITE_PATTERN.search(message.content) and not has_invite_permission(message.author):
        try:
            await message.delete()
            warning = await message.channel.send(
                f"{message.author.mention} \u26a0 Sharing Discord invite links requires permission "
                f"from a Moderator or the Dev."
            )
            await warning.delete(delay=8)
        except discord.Forbidden:
            pass
        return

    if message.channel.name != ALLOWED_YOUTUBE_CHANNEL:
        content_lower = message.content.lower()
        if any(pattern in content_lower for pattern in YOUTUBE_PATTERNS):
            try:
                await message.delete()
                target_channel = discord.utils.get(message.guild.text_channels, name=ALLOWED_YOUTUBE_CHANNEL)
                warning = await message.channel.send(
                    f"{message.author.mention} \u26a0 YouTube links are only allowed in "
                    f"{f'<#{target_channel.id}>' if target_channel else ALLOWED_YOUTUBE_CHANNEL}."
                )
                await warning.delete(delay=8)
            except discord.Forbidden:
                pass
            return

    await bot.process_commands(message)


@bot.event
async def on_member_join(member):
    # ── Mass-join (raid) detection ──
    now = time.time()
    dq = _join_timestamps[member.guild.id]
    dq.append(now)
    while dq and now - dq[0] > RAID_JOIN_WINDOW_SECONDS:
        dq.popleft()

    if len(dq) >= RAID_JOIN_THRESHOLD and not _raid_mode_active[member.guild.id]:
        locked = await lock_guild(member.guild)
        embed = discord.Embed(
            title="🚨 Possible Raid Detected",
            description=(
                f"{len(dq)} members joined within {RAID_JOIN_WINDOW_SECONDS}s.\n"
                f"Auto-locked {locked} channel(s). Run `/raidunlock` once it's safe to reopen."
            ),
            color=0xFF0000
        )
        embed.add_field(name="Most recent joiner", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(
            name="Account created",
            value=discord.utils.format_dt(member.created_at, style="R"),
            inline=False
        )
        await alert_mods(member.guild, embed)

    channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL_NAME)
    if channel is None:
        return

    verify_channel = discord.utils.get(member.guild.text_channels, name="verify")
    verify_mention = f"<#{verify_channel.id}>" if verify_channel else "#verify"

    embed = discord.Embed(
        title="🎉 Welcome to 3RBX-MGT",
        description=(
            f"Hey {member.mention}!\n\n"
            f"Welcome to **3D ROBLOX MODEL MOBILE GLOBAL TOOLS** Official.\n\n"
            f"Don't forget to read the **RULES** in {verify_mention} channel and get verified to unlock full access!"
        ),
        color=0x00D4FF
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Member #{member.guild.member_count} · 3DRBXMT")

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


@bot.event
async def on_ready():
    bot_status["ready"] = True
    bot_status["last_ready_at"] = time.time()
    bot_status["latency_ms"] = round(bot.latency * 1000, 1)
    logger.info(f"Bot login sebagai {bot.user} (latency: {bot_status['latency_ms']}ms)")
    bot.add_view(VerifyView())  # Register persistent view supaya tombol tetap jalan setelah restart
    bot.add_view(RoleSelectView())
    bot.add_view(PartnershipRequestView())
    bot.add_view(ModeratorSignupRequestView())
    bot.add_view(ContentCreatorRequestView())  # Register persistent view untuk role select menu
    try:
        synced = await bot.tree.sync()
        logger.info(f"{len(synced)} slash command(s) synced")
    except Exception as e:
        logger.error(f"Gagal sync commands: {e}")

    # Snapshot existing webhooks so we only alert on newly created ones later
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                whs = await channel.webhooks()
                _known_webhooks[guild.id].update(wh.id for wh in whs)
            except discord.Forbidden:
                continue


@bot.event
async def on_webhooks_update(channel):
    """Alerts mods when a new webhook appears — a common raid/spam persistence trick."""
    guild = channel.guild
    try:
        current_webhooks = await channel.webhooks()
    except discord.Forbidden:
        return

    current_ids = {wh.id for wh in current_webhooks}
    known_ids = _known_webhooks[guild.id]
    new_ids = current_ids - known_ids

    for wh in current_webhooks:
        if wh.id in new_ids:
            creator = str(wh.user) if wh.user else "Unknown (bot may lack Manage Webhooks to see creator)"
            await alert_mods(
                guild,
                discord.Embed(
                    title="⚠ New Webhook Created",
                    description=(
                        f"A webhook named **{wh.name}** was created in {channel.mention}.\n"
                        f"Created by: {creator}\n\n"
                        f"If you don't recognize this, delete it — webhooks are a common way "
                        f"raiders keep posting even after being banned."
                    ),
                    color=0xFFA500
                )
            )

    _known_webhooks[guild.id] = current_ids


@bot.event
async def on_disconnect():
    bot_status["ready"] = False
    bot_status["last_disconnect_at"] = time.time()
    logger.warning("Bot disconnected from Discord gateway")


@bot.event
async def on_resumed():
    bot_status["ready"] = True
    bot_status["reconnect_count"] += 1
    logger.info(f"Session resumed (reconnect #{bot_status['reconnect_count']})")


@bot.tree.command(name="setup_verify", description="Send a verification message with a button (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔒 Member Verification",
        description=(
            "Welcome to **3DRBXMT Community**!\n\n"
            "Click the button below to verify and get full access to the server."
        ),
        color=0x00D4FF
    )
    embed.set_footer(text="3DRBXMT · Roblox 3D Model Tools")

    await interaction.channel.send(embed=embed, view=VerifyView())
    await interaction.response.send_message("✓ Verification message sent successfully!", ephemeral=True)


@setup_verify.error
async def setup_verify_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⚠ You don't have permission to run this command.",
            ephemeral=True
        )


ROLE_OPTIONS = [
    ("Prisma 3D", "🎨"),
    ("Nomad Sculpt", "🗿"),
    ("3D Modeler", "📦"),
    ("2D Artist", "🖌️"),
    ("Indonesian", "🇮🇩"),
    ("English", "🇬🇧"),
    ("Other Languages", "🌐"),
    ("YouTube Ping", "🔔"),
]


class RoleSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=name, emoji=emoji, value=name)
            for name, emoji in ROLE_OPTIONS
        ]
        super().__init__(
            placeholder="Choose your roles (you can select more than one)...",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id="role_select_menu"
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        selected = set(self.values)
        all_role_names = {name for name, _ in ROLE_OPTIONS}

        added, removed, missing = [], [], []

        for role_name in all_role_names:
            role = discord.utils.get(guild.roles, name=role_name)
            if role is None:
                missing.append(role_name)
                continue
            has_role = role in member.roles
            wants_role = role_name in selected

            if wants_role and not has_role:
                await member.add_roles(role)
                added.append(role_name)
            elif not wants_role and has_role:
                await member.remove_roles(role)
                removed.append(role_name)

        msg_parts = []
        if added:
            msg_parts.append(f"✅ Added: {', '.join(added)}")
        if removed:
            msg_parts.append(f"➖ Removed: {', '.join(removed)}")
        if missing:
            msg_parts.append(f"⚠ Role(s) not found on server: {', '.join(missing)}")
        if not msg_parts:
            msg_parts.append("No changes made.")

        await interaction.response.send_message("\n".join(msg_parts), ephemeral=True)


class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RoleSelect())


@bot.tree.command(name="setup_roles", description="Send the role selection menu (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_roles(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🏷️ Choose Your Roles",
        description=(
            "Select the roles that match your software, skill, or language.\n"
            "You can select more than one at once from the dropdown below.\n\n"
            "**Software/Skill:** Prisma 3D, Nomad Sculpt, 3D Modeler, 2D Artist\n"
            "**Language:** Indonesian, English, Other Languages\n\n"
            "⚠ The **Content Creator** and **Moderator** roles are not available here — contact the Dev/Admin directly if you'd like those roles."
        ),
        color=0x00D4FF
    )
    embed.set_footer(text="3DRBXMT · Roblox 3D Model Tools")

    await interaction.channel.send(embed=embed, view=RoleSelectView())
    await interaction.response.send_message("✓ Role menu sent successfully!", ephemeral=True)


@setup_roles.error
async def setup_roles_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⚠ You don't have permission to run this command.",
            ephemeral=True
        )


@bot.tree.command(name="checkproto", description="Check if Protogen Security is online and working")
async def checkproto(interaction: discord.Interaction):
    await interaction.response.send_message(
        "proto security is here, just watching the community and spying on the DEV =w="
    )


ANNOUNCEMENT_CHANNEL_NAME = "announcement"


@bot.tree.command(name="announce", description="Send an announcement to the announcement channel (admin only)")
@app_commands.describe(title="Title of the announcement", message="The announcement content")
@app_commands.checks.has_permissions(administrator=True)
async def announce(interaction: discord.Interaction, title: str, message: str):
    channel = discord.utils.get(interaction.guild.text_channels, name=ANNOUNCEMENT_CHANNEL_NAME)
    if channel is None:
        await interaction.response.send_message(
            f"⚠ Channel #{ANNOUNCEMENT_CHANNEL_NAME} not found.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"📢 {title}",
        description=message,
        color=0x00D4FF
    )
    embed.set_footer(text="3DRBX-MGT · Official Announcement")

    try:
        await channel.send(content="@here", embed=embed)
        await interaction.response.send_message(
            f"✓ Announcement sent to #{ANNOUNCEMENT_CHANNEL_NAME}!",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠ The bot doesn't have permission to send messages in that channel.",
            ephemeral=True
        )


@announce.error
async def announce_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⚠ You don't have permission to run this command.",
            ephemeral=True
        )


class StatusToggleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="🔧 Set to Maintenance", style=discord.ButtonStyle.danger)
    async def set_maintenance(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠ Only admins can change the website status.",
                ephemeral=True
            )
            return
        await interaction.response.defer()
        success = set_web_maintenance_state(True)
        if success:
            await interaction.edit_original_response(
                content="🔧 Website status set to **MAINTENANCE**.",
                embed=None, view=None
            )
        else:
            await interaction.edit_original_response(
                content="✗ Failed to update status.", embed=None, view=None
            )

    @discord.ui.button(label="✅ Set to Live", style=discord.ButtonStyle.success)
    async def set_live(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠ Only admins can change the website status.",
                ephemeral=True
            )
            return
        await interaction.response.defer()
        success = set_web_maintenance_state(False)
        if success:
            await interaction.edit_original_response(
                content="✅ Website status set to **LIVE**.",
                embed=None, view=None
            )
        else:
            await interaction.edit_original_response(
                content="✗ Failed to update status.", embed=None, view=None
            )


@bot.tree.command(name="statusweb", description="Check 3DRBXMT website status (maintenance or live)")
async def statusweb(interaction: discord.Interaction):
    state = get_web_maintenance_state()

    if state is None:
        await interaction.response.send_message("⚠ Unable to fetch website status right now.", ephemeral=True)
        return

    status_text = "🔧 **MAINTENANCE**" if state else "✅ **LIVE**"
    embed = discord.Embed(
        title="🌐 3DRBXMT Website Status",
        description=f"Current status: {status_text}",
        color=0xFF3355 if state else 0x00D4FF
    )
    embed.set_footer(text="getrbx3d.qzz.io")

    await interaction.response.send_message(embed=embed, view=StatusToggleView())


import uuid

class PartnershipModal(discord.ui.Modal, title="Partnership Request"):
    owner = discord.ui.TextInput(
        label="Community Owner (name/@mention)",
        placeholder="e.g. @JohnDoe or JohnDoe#1234",
        required=True,
        max_length=100
    )
    invite_link = discord.ui.TextInput(
        label="Discord Invite Link",
        placeholder="https://discord.gg/xxxxxxx",
        required=True,
        max_length=200
    )
    details = discord.ui.TextInput(
        label="About the community (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        submission_id = str(uuid.uuid4())[:8]
        partnership_submissions[submission_id] = {
            "requester_id": interaction.user.id,
            "channel_id": interaction.channel.id,
            "owner": str(self.owner),
            "link": str(self.invite_link),
            "details": str(self.details) if self.details.value else "None provided",
        }

        dev = interaction.client.get_user(DEV_USER_ID) or await interaction.client.fetch_user(DEV_USER_ID)

        embed = discord.Embed(title="\U0001f91d New Partnership Request", color=0x00D4FF)
        embed.add_field(name="Requested by", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.add_field(name="Community Owner", value=partnership_submissions[submission_id]["owner"], inline=False)
        embed.add_field(name="Invite Link", value=partnership_submissions[submission_id]["link"], inline=False)
        embed.add_field(name="Details", value=partnership_submissions[submission_id]["details"], inline=False)
        embed.set_footer(text=f"Submission ID: {submission_id}")

        try:
            await dev.send(embed=embed, view=PartnershipDecisionView(submission_id))
            await interaction.response.send_message(
                "\u2705 Your partnership request has been sent to the Dev for review!",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "\u26a0 Couldn't reach the Dev's DMs. Please contact them directly.",
                ephemeral=True
            )


class PartnershipRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="\U0001f91d Partnership", style=discord.ButtonStyle.primary, custom_id="partnership_request_button")
    async def request_partnership(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PartnershipModal())


class PartnershipDenyReasonModal(discord.ui.Modal, title="Denial Reason"):
    reason = discord.ui.TextInput(
        label="Reason for denying (sent to requester)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )

    def __init__(self, submission_id: str, original_message: discord.Message):
        super().__init__()
        self.submission_id = submission_id
        self.original_message = original_message

    async def on_submit(self, interaction: discord.Interaction):
        data = partnership_submissions.get(self.submission_id)
        if not data:
            await interaction.response.send_message("\u26a0 This request was already handled or expired.", ephemeral=True)
            return

        reason = str(self.reason)

        await self.original_message.edit(
            content=f"\u274c **DENIED** by Dev.\nReason: {reason}",
            embed=self.original_message.embeds[0],
            view=None
        )
        await interaction.response.send_message("\u2705 Denial sent.", ephemeral=True)

        channel = interaction.client.get_channel(data["channel_id"])
        requester_mention = f"<@{data['requester_id']}>"

        if channel:
            try:
                await channel.send(
                    f"{requester_mention} Your partnership request has been **DENIED** by the Dev.\nReason: {reason}"
                )
            except discord.Forbidden:
                pass

        del partnership_submissions[self.submission_id]


class PartnershipDecisionView(discord.ui.View):
    def __init__(self, submission_id: str):
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.children[0].custom_id = f"partner_accept_{submission_id}"
        self.children[1].custom_id = f"partner_deny_{submission_id}"

    @discord.ui.button(label="ACCEPT", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = partnership_submissions.get(self.submission_id)
        if not data:
            await interaction.response.edit_message(content="\u26a0 This request was already handled or expired.", embed=None, view=None)
            return

        await interaction.response.edit_message(
            content="\u2705 **ACCEPTED** by Dev.",
            embed=interaction.message.embeds[0],
            view=None
        )

        channel = interaction.client.get_channel(data["channel_id"])
        requester_mention = f"<@{data['requester_id']}>"

        if channel:
            try:
                await channel.send(
                    f"{requester_mention} \U0001f389 Your partnership request has been **ACCEPTED** by the Dev! "
                    f"We'll be in touch about next steps."
                )
            except discord.Forbidden:
                pass

        del partnership_submissions[self.submission_id]

    @discord.ui.button(label="DENIED", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = partnership_submissions.get(self.submission_id)
        if not data:
            await interaction.response.edit_message(content="\u26a0 This request was already handled or expired.", embed=None, view=None)
            return
        await interaction.response.send_modal(PartnershipDenyReasonModal(self.submission_id, interaction.message))


@bot.tree.command(name="setup_partnership", description="Send the partnership request message (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_partnership(interaction: discord.Interaction):
    embed = discord.Embed(
        title="\U0001f91d Community Partnership",
        description=(
            "Want to partner your community with **3DRBXMT**?\n\n"
            "Click the button below and fill in the form with your community owner's "
            "name and invite link. The Dev will review it and you'll get a result here."
        ),
        color=0x00D4FF
    )
    embed.set_footer(text="3DRBXMT \u00b7 Partnerships")
    await interaction.channel.send(embed=embed, view=PartnershipRequestView())
    await interaction.response.send_message("\u2713 Partnership message sent!", ephemeral=True)

@setup_partnership.error
async def setup_partnership_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("\u26a0 You don't have permission to run this command.", ephemeral=True)


class ModeratorSignupModal(discord.ui.Modal, title="Moderator Sign Up"):
    q1 = discord.ui.TextInput(
        label="Why do you want to become a Moderator?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )
    q2 = discord.ui.TextInput(
        label="Most important 2-3 rules & why?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )
    q3 = discord.ui.TextInput(
        label="Handling repeat offenders/NSFW/toxicity?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    q4 = discord.ui.TextInput(
        label="Comfortable w/ mod tools? Punishing a friend?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    q5 = discord.ui.TextInput(
        label="Anything else staff should know? (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        submission_id = str(uuid.uuid4())[:8]
        moderator_submissions[submission_id] = {
            "requester_id": interaction.user.id,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "answers": {
                "Why become a Moderator?": str(self.q1),
                "Most important rules & why?": str(self.q2),
                "Handling repeat offenders/NSFW/toxicity?": str(self.q3),
                "Mod tools comfort / punishing a friend?": str(self.q4),
                "Anything else?": str(self.q5) if self.q5.value else "None provided",
            }
        }

        dev = interaction.client.get_user(DEV_USER_ID) or await interaction.client.fetch_user(DEV_USER_ID)

        embed = discord.Embed(title="\U0001f6e1 New Moderator Application", color=0xFFA500)
        embed.add_field(name="Applicant", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        for question, answer in moderator_submissions[submission_id]["answers"].items():
            embed.add_field(name=question, value=answer[:1024], inline=False)
        embed.set_footer(text=f"Submission ID: {submission_id}")

        try:
            await dev.send(embed=embed, view=ModeratorDecisionView(submission_id))
            await interaction.response.send_message(
                "\u2705 Your moderator application has been sent to the Dev for review!",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "\u26a0 Couldn't reach the Dev's DMs. Please contact them directly.",
                ephemeral=True
            )


class ModeratorSignupRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="\U0001f6e1 Sign Up Moderator", style=discord.ButtonStyle.primary, custom_id="moderator_signup_button")
    async def signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModeratorSignupModal())


class ModeratorDenyReasonModal(discord.ui.Modal, title="Denial Reason"):
    reason = discord.ui.TextInput(
        label="Reason for denying (sent to applicant)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )

    def __init__(self, submission_id: str, original_message: discord.Message):
        super().__init__()
        self.submission_id = submission_id
        self.original_message = original_message

    async def on_submit(self, interaction: discord.Interaction):
        data = moderator_submissions.get(self.submission_id)
        if not data:
            await interaction.response.send_message("\u26a0 This application was already handled or expired.", ephemeral=True)
            return

        reason = str(self.reason)

        await self.original_message.edit(
            content=f"\u274c **DENIED** by Dev.\nReason: {reason}",
            embed=self.original_message.embeds[0],
            view=None
        )
        await interaction.response.send_message("\u2705 Denial sent.", ephemeral=True)

        channel = interaction.client.get_channel(data["channel_id"])
        requester_mention = f"<@{data['requester_id']}>"

        if channel:
            try:
                await channel.send(
                    f"{requester_mention} Your moderator application has been **DENIED** by the Dev.\nReason: {reason}"
                )
            except discord.Forbidden:
                pass

        del moderator_submissions[self.submission_id]


class ModeratorDecisionView(discord.ui.View):
    def __init__(self, submission_id: str):
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.children[0].custom_id = f"mod_promote_{submission_id}"
        self.children[1].custom_id = f"mod_deny_{submission_id}"

    @discord.ui.button(label="PROMOTE", style=discord.ButtonStyle.success)
    async def promote(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        data = moderator_submissions.get(self.submission_id)
        if not data:
            await interaction.edit_original_response(content="\u26a0 This application was already handled or expired.", embed=None, view=None)
            return

        guild = interaction.client.get_guild(data["guild_id"])
        member = guild.get_member(data["requester_id"]) if guild else None
        channel = interaction.client.get_channel(data["channel_id"])
        requester_mention = f"<@{data['requester_id']}>"

        role_granted = False
        if member and guild:
            role = discord.utils.get(guild.roles, name=MODERATOR_ROLE_NAME)
            if role:
                try:
                    await member.add_roles(role, reason="Promoted via moderator application")
                    role_granted = True
                except discord.Forbidden:
                    pass

        result_text = "\u2705 **PROMOTED**" if role_granted else "\u2705 **PROMOTED** (\u26a0 role assignment failed \u2014 check bot permissions/role hierarchy)"
        await interaction.edit_original_response(content=f"{result_text} by Dev.", embed=interaction.message.embeds[0], view=None)

        if channel:
            try:
                await channel.send(
                    f"{requester_mention} \U0001f389 Congratulations! You've been **PROMOTED** to Moderator!"
                )
            except discord.Forbidden:
                pass

        del moderator_submissions[self.submission_id]

    @discord.ui.button(label="DENIED", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = moderator_submissions.get(self.submission_id)
        if not data:
            await interaction.response.edit_message(content="\u26a0 This application was already handled or expired.", embed=None, view=None)
            return
        await interaction.response.send_modal(ModeratorDenyReasonModal(self.submission_id, interaction.message))


@bot.tree.command(name="setup_moderator_signup", description="Send the moderator sign-up message (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_moderator_signup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="\U0001f6e1 Become a Moderator",
        description=(
            "Interested in helping moderate **3DRBXMT**?\n\n"
            "Click the button below and answer a few questions. The Dev will review "
            "your application and you'll get a result here."
        ),
        color=0xFFA500
    )
    embed.set_footer(text="3DRBXMT \u00b7 Staff Applications")
    await interaction.channel.send(embed=embed, view=ModeratorSignupRequestView())
    await interaction.response.send_message("\u2713 Moderator sign-up message sent!", ephemeral=True)

@setup_moderator_signup.error
async def setup_moderator_signup_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("\u26a0 You don't have permission to run this command.", ephemeral=True)


CONTENT_CREATOR_ROLE_NAME = "Content Creator"
content_creator_submissions = {}  # temp storage: submission_id -> data


class ContentCreatorModal(discord.ui.Modal, title="Content Creator Sign Up"):
    channel_name = discord.ui.TextInput(
        label="Name of the channel",
        required=True,
        max_length=100
    )
    channel_link = discord.ui.TextInput(
        label="Link Channel YouTube",
        placeholder="https://youtube.com/@yourchannel",
        required=True,
        max_length=200
    )
    about = discord.ui.TextInput(
        label="About This YouTube Channel (Optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        submission_id = str(uuid.uuid4())[:8]
        content_creator_submissions[submission_id] = {
            "requester_id": interaction.user.id,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "channel_name": str(self.channel_name),
            "channel_link": str(self.channel_link),
            "about": str(self.about) if self.about.value else "None provided",
        }

        dev = interaction.client.get_user(DEV_USER_ID) or await interaction.client.fetch_user(DEV_USER_ID)

        embed = discord.Embed(title="\U0001f3ac New Content Creator Application", color=0xFF0000)
        embed.add_field(name="Applicant", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.add_field(name="Channel Name", value=content_creator_submissions[submission_id]["channel_name"], inline=False)
        embed.add_field(name="Channel Link", value=content_creator_submissions[submission_id]["channel_link"], inline=False)
        embed.add_field(name="About", value=content_creator_submissions[submission_id]["about"], inline=False)
        embed.set_footer(text=f"Submission ID: {submission_id}")

        try:
            await dev.send(embed=embed, view=ContentCreatorDecisionView(submission_id))
            await interaction.followup.send(
                "\u2705 Your Content Creator application has been sent to the Dev for review!",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "\u26a0 Couldn't reach the Dev's DMs. Please contact them directly.",
                ephemeral=True
            )


class ContentCreatorRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="\U0001f3ac Content Creator", style=discord.ButtonStyle.primary, custom_id="content_creator_signup_button")
    async def signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ContentCreatorModal())


class ContentCreatorDenyReasonModal(discord.ui.Modal, title="Denial Reason"):
    reason = discord.ui.TextInput(
        label="Reason for denying (sent to applicant)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )

    def __init__(self, submission_id: str, original_message: discord.Message):
        super().__init__()
        self.submission_id = submission_id
        self.original_message = original_message

    async def on_submit(self, interaction: discord.Interaction):
        data = content_creator_submissions.get(self.submission_id)
        if not data:
            await interaction.response.send_message("\u26a0 This application was already handled or expired.", ephemeral=True)
            return

        reason = str(self.reason)

        await self.original_message.edit(
            content=f"\u274c **DENIED** by Dev.\nReason: {reason}",
            embed=self.original_message.embeds[0],
            view=None
        )
        await interaction.response.send_message("\u2705 Denial sent.", ephemeral=True)

        channel = interaction.client.get_channel(data["channel_id"])
        requester_mention = f"<@{data['requester_id']}>"

        if channel:
            try:
                await channel.send(
                    f"{requester_mention} Your Content Creator application has been **DENIED** by the Dev.\nReason: {reason}"
                )
            except discord.Forbidden:
                pass

        del content_creator_submissions[self.submission_id]


class ContentCreatorDecisionView(discord.ui.View):
    def __init__(self, submission_id: str):
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.children[0].custom_id = f"creator_promote_{submission_id}"
        self.children[1].custom_id = f"creator_deny_{submission_id}"

    @discord.ui.button(label="PROMOTE", style=discord.ButtonStyle.success)
    async def promote(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        data = content_creator_submissions.get(self.submission_id)
        if not data:
            await interaction.edit_original_response(content="\u26a0 This application was already handled or expired.", embed=None, view=None)
            return

        guild = interaction.client.get_guild(data["guild_id"])
        member = guild.get_member(data["requester_id"]) if guild else None
        channel = interaction.client.get_channel(data["channel_id"])
        requester_mention = f"<@{data['requester_id']}>"

        role_granted = False
        if member and guild:
            role = discord.utils.get(guild.roles, name=CONTENT_CREATOR_ROLE_NAME)
            if role:
                try:
                    await member.add_roles(role, reason="Promoted via Content Creator application")
                    role_granted = True
                except discord.Forbidden:
                    pass

        result_text = "\u2705 **PROMOTED**" if role_granted else "\u2705 **PROMOTED** (\u26a0 role assignment failed \u2014 check bot permissions/role hierarchy)"
        await interaction.edit_original_response(content=f"{result_text} by Dev.", embed=interaction.message.embeds[0], view=None)

        if channel:
            try:
                await channel.send(
                    f"{requester_mention} \U0001f389 Congratulations! You've been **PROMOTED** to Content Creator!"
                )
            except discord.Forbidden:
                pass

        del content_creator_submissions[self.submission_id]

    @discord.ui.button(label="DENIED", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = content_creator_submissions.get(self.submission_id)
        if not data:
            await interaction.response.edit_message(content="\u26a0 This application was already handled or expired.", embed=None, view=None)
            return
        await interaction.response.send_modal(ContentCreatorDenyReasonModal(self.submission_id, interaction.message))


@bot.tree.command(name="setup_content_creator", description="Send the Content Creator sign-up message (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_content_creator(interaction: discord.Interaction):
    embed = discord.Embed(
        title="\U0001f3ac Become a Content Creator",
        description=(
            "Create YouTube content related to **3DRBXMT**?\n\n"
            "Click the button below and tell us about your channel. The Dev will review "
            "your application and you'll get a result here."
        ),
        color=0xFF0000
    )
    embed.set_footer(text="3DRBXMT \u00b7 Content Creator Applications")
    await interaction.channel.send(embed=embed, view=ContentCreatorRequestView())
    await interaction.response.send_message("\u2713 Content Creator sign-up message sent!", ephemeral=True)

@setup_content_creator.error
async def setup_content_creator_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("\u26a0 You don't have permission to run this command.", ephemeral=True)


# ── MODERATION COMMANDS ───────────────────────────────────

@bot.tree.command(name="kick", description="Kick a member from the server (mod only)")
@app_commands.describe(member="Member to kick", reason="Reason for kicking")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.id == DEV_USER_ID or any(r.name in PROTECTED_ROLES for r in member.roles):
        await interaction.response.send_message("⛔ This member is protected and cannot be kicked.", ephemeral=True)
        return
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 {member.mention} has been kicked. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("⚠ I don't have permission to kick this member.", ephemeral=True)


@kick.error
async def kick_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="ban", description="Ban a member from the server (mod only)")
@app_commands.describe(member="Member to ban", reason="Reason for banning")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.id == DEV_USER_ID or any(r.name in PROTECTED_ROLES for r in member.roles):
        await interaction.response.send_message("⛔ This member is protected and cannot be banned.", ephemeral=True)
        return
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"🔨 {member.mention} has been banned. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("⚠ I don't have permission to ban this member.", ephemeral=True)


@ban.error
async def ban_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="timeout", description="Timeout (mute) a member for a duration (mod only)")
@app_commands.describe(member="Member to timeout", minutes="Duration in minutes", reason="Reason for timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
    if member.id == DEV_USER_ID or any(r.name in PROTECTED_ROLES for r in member.roles):
        await interaction.response.send_message("⛔ This member is protected and cannot be timed out.", ephemeral=True)
        return
    import datetime
    try:
        duration = datetime.timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        await interaction.response.send_message(f"🔇 {member.mention} has been timed out for {minutes} minute(s). Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("⚠ I don't have permission to timeout this member.", ephemeral=True)


@timeout.error
async def timeout_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="clear", description="Delete a number of recent messages (mod only)")
@app_commands.describe(amount="Number of messages to delete (max 100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        await interaction.response.send_message("⚠ Amount must be between 1 and 100.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s).", ephemeral=True)


@clear.error
async def clear_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


def get_warnings(user_id):
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/warnings",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            },
            params={"user_id": f"eq.{user_id}", "select": "id,reason", "order": "id.asc"},
            timeout=5
        )
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def add_warning(user_id, reason):
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/warnings",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            },
            json={"user_id": str(user_id), "reason": reason},
            timeout=5
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


def remove_warning(warning_id):
    try:
        r = requests.delete(
            f"{SUPABASE_URL}/rest/v1/warnings",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            },
            params={"id": f"eq.{warning_id}"},
            timeout=5
        )
        return r.status_code in (200, 204)
    except Exception:
        return False


@bot.tree.command(name="warn", description="Warn a member (mod only)")
@app_commands.describe(member="Member to warn", reason="Reason for warning")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    await interaction.response.defer()
    success = add_warning(member.id, reason)
    if not success:
        await interaction.followup.send("✗ Failed to save warning.", ephemeral=True)
        return
    count = len(get_warnings(member.id))
    await interaction.followup.send(
        f"⚠ {member.mention} has been warned. Reason: {reason}\nTotal warnings: {count}"
    )


@warn.error
async def warn_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="warnings", description="Check a member's warning history (mod only)")
@app_commands.describe(member="Member to check")
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    entries = get_warnings(member.id)
    if not entries:
        await interaction.followup.send(f"{member.mention} has no warnings.", ephemeral=True)
        return
    formatted = "\n".join(f"{i+1}. (ID:{e['id']}) {e['reason']}" for i, e in enumerate(entries))
    await interaction.followup.send(
        f"⚠ Warning history for {member.mention} ({len(entries)} total):\n{formatted}",
        ephemeral=True
    )


@warnings_cmd.error
async def warnings_cmd_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


def get_dev_info(user_id):
    """Fetches a saved /addinfo profile for a user. Returns dict or None."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/dev_info",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            },
            params={"user_id": f"eq.{user_id}", "select": "name,age,language,about"},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            return data[0] if data else None
        return None
    except Exception:
        return None


def upsert_dev_info(user_id, name, age, language, about):
    """Creates or overwrites a user's /addinfo profile. Requires a unique constraint
    on user_id in the dev_info table for the upsert to merge correctly."""
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/dev_info",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            },
            params={"on_conflict": "user_id"},
            json={
                "user_id": str(user_id),
                "name": name,
                "age": age,
                "language": language,
                "about": about
            },
            timeout=5
        )
        if r.status_code not in (200, 201):
            logger.error(f"upsert_dev_info failed: status={r.status_code} body={r.text}")
        return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"upsert_dev_info exception: {e}")
        return False


class AddInfoModal(discord.ui.Modal, title="Set Your Dev Info"):
    name = discord.ui.TextInput(label="Name", placeholder="What should we call you?", max_length=100)
    age = discord.ui.TextInput(label="Age", placeholder="e.g. 21", max_length=10)
    language = discord.ui.TextInput(label="Language", placeholder="e.g. Python, Lua, JavaScript", max_length=100)
    about = discord.ui.TextInput(
        label="About",
        style=discord.TextStyle.paragraph,
        placeholder="Tell us a bit about yourself...",
        max_length=500,
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        success = upsert_dev_info(
            interaction.user.id,
            self.name.value,
            self.age.value,
            self.language.value,
            self.about.value or "—"
        )
        if success:
            await interaction.response.send_message(
                "✅ Your info has been saved! Check it anytime with `/devinfo`.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠ Something went wrong saving your info. Try again in a moment.",
                ephemeral=True
            )


@bot.tree.command(name="addinfo", description="Fill in your dev info (name, age, language, about)")
async def addinfo(interaction: discord.Interaction):
    await interaction.response.send_modal(AddInfoModal())


@bot.tree.command(name="devinfo", description="View a member's dev info")
@app_commands.describe(member="Whose info to view (defaults to you)")
async def devinfo(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    info = get_dev_info(target.id)

    if info is None:
        if target.id == interaction.user.id:
            await interaction.response.send_message(
                "You haven't set your info yet — use `/addinfo` to fill it in!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{target.mention} hasn't set their info yet.",
                ephemeral=True
            )
        return

    embed = discord.Embed(title=f"📇 {target.display_name}'s Dev Info", color=0x00D4FF)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Name", value=info.get("name") or "—", inline=True)
    embed.add_field(name="Age", value=info.get("age") or "—", inline=True)
    embed.add_field(name="Language", value=info.get("language") or "—", inline=True)
    embed.add_field(name="About", value=info.get("about") or "—", inline=False)
    await interaction.response.send_message(embed=embed)


# ── SERVER STATS & BOT INFO ────────────────────────────────

@bot.tree.command(name="serverstats", description="Show server statistics")
async def serverstats(interaction: discord.Interaction):
    guild = interaction.guild
    total_members = guild.member_count
    online_members = sum(1 for m in guild.members if m.status != discord.Status.offline)
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    role_count = len(guild.roles)

    embed = discord.Embed(title=f"📊 {guild.name} Stats", color=0x00D4FF)
    embed.add_field(name="Total Members", value=str(total_members), inline=True)
    embed.add_field(name="Online Members", value=str(online_members), inline=True)
    embed.add_field(name="Text Channels", value=str(text_channels), inline=True)
    embed.add_field(name="Voice Channels", value=str(voice_channels), inline=True)
    embed.add_field(name="Roles", value=str(role_count), inline=True)
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="botinfo", description="Show bot information (ping, uptime, etc)")
async def botinfo(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    embed = discord.Embed(title="🤖 Protogen Security Info", color=0x00D4FF)
    embed.add_field(name="Ping", value=f"{latency_ms}ms", inline=True)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.set_footer(text="3DRBX-MGT · Protogen Security")

    await interaction.response.send_message(embed=embed)


PROTECTED_ROLES = ["Dev", "Moderator", "BOT", "Protogen Security"]


@bot.tree.command(name="revokerole", description="Revoke a role from a member as punishment (mod only)")
@app_commands.describe(member="Member to revoke role from", role="Role to revoke", reason="Reason for revoking")
@app_commands.checks.has_permissions(manage_roles=True)
async def revokerole(interaction: discord.Interaction, member: discord.Member, role: discord.Role, reason: str = "No reason provided"):
    if role.name in PROTECTED_ROLES:
        await interaction.response.send_message(
            f"⛔ The role **{role.name}** is protected and cannot be revoked through this command.",
            ephemeral=True
        )
        return

    if role not in member.roles:
        await interaction.response.send_message(
            f"⚠ {member.mention} doesn't have the role **{role.name}**.",
            ephemeral=True
        )
        return

    try:
        await member.remove_roles(role, reason=reason)
        await interaction.response.send_message(
            f"🚫 Role **{role.name}** has been revoked from {member.mention}.\nReason: {reason}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠ I don't have permission to remove this role (check role hierarchy).",
            ephemeral=True
        )


@revokerole.error
async def revokerole_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="myinfo", description="Check your own info (or another member's) including warnings")
@app_commands.describe(member="Member to check (leave empty to check yourself)")
async def myinfo(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user

    roles = [r.name for r in target.roles if r.name != "@everyone"]
    roles_text = ", ".join(roles) if roles else "No roles yet"

    entries = get_warnings(target.id)
    warning_count = len(entries)

    embed = discord.Embed(
        title=f"📋 Info for {target.display_name}",
        color=0x00D4FF
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Username", value=str(target), inline=True)
    embed.add_field(name="Joined Server", value=target.joined_at.strftime("%B %d, %Y") if target.joined_at else "Unknown", inline=True)
    embed.add_field(name="Account Created", value=target.created_at.strftime("%B %d, %Y"), inline=True)
    embed.add_field(name="Roles", value=roles_text, inline=False)
    embed.add_field(name="Total Warnings", value=str(warning_count), inline=True)

    if entries:
        formatted = "\n".join(f"{i+1}. {e['reason']}" for i, e in enumerate(entries))
        embed.add_field(name="Warning History", value=formatted, inline=False)

    embed.set_footer(text="3DRBX-MGT · Member Info")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="help", description="Show all available commands and who can use them")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 3DRBX-MGT Bot Commands",
        description="List of all available commands and access level.",
        color=0x00D4FF
    )

    embed.add_field(
        name="👤 Everyone",
        value=(
            "`/checkproto` — Check if the bot is online\n"
            "`/statusweb` — Check website status (live/maintenance)\n"
            "`/myinfo [member]` — View your or another member's info & warnings\n"
            "`/addinfo` — Fill in your dev info (name, age, language, about)\n"
            "`/devinfo [member]` — View your or another member's dev info\n"
            "`/serverstats` — View server statistics\n"
            "`/botinfo` — View bot info (ping, uptime)\n"
            "`/help` — Show this command list\n"
            "💬 **Mention the bot in #ai-chat** — Chat with the AI assistant (if enabled)\n"
            "`/imagine <prompt>` — Generate an image from text (in #ai-chat)\n"
            "🤝 **Partnership** button — Submit a partnership request\n"
            "🛡 **Sign Up Moderator** button — Apply to become a Moderator"
        ),
        inline=False
    )

    embed.add_field(
        name="🛡️ Moderator Only",
        value=(
            "`/kick` — Kick a member\n"
            "`/ban` — Ban a member\n"
            "`/timeout` — Timeout a member\n"
            "`/untimeout` — Remove timeout early\n"
            "`/clear` — Bulk delete messages\n"
            "`/warn` — Warn a member\n"
            "`/warnings` — View a member's warning history\n"
            "`/removewarning` — Remove a specific warning\n"
            "`/revokerole` — Revoke a role as punishment\n"
            "`/slowmode` — Set channel slowmode delay\n"
            "`/lock` / `/unlock` — Lock or unlock a channel\n"
            "`/nickname` — Change a member's nickname"
        ),
        inline=False
    )

    embed.add_field(
        name="🚨 Raid Response (Admin Only)",
        value=(
            "`/raidlockdown` — Lock every channel server-wide\n"
            "`/raidunlock` — Reverse a server-wide lockdown\n\n"
            "Also automatic: mass-join auto-lockdown, mass-mention/link-spam auto-timeout, "
            "and new-webhook alerts — all posted to a `#mod-log` channel if one exists."
        ),
        inline=False
    )

    embed.add_field(
        name="👑 Admin/Dev Only",
        value=(
            "`/setup_verify` — Send the verification message\n"
            "`/setup_roles` — Send the self-role selection menu\n"
            "`/setup_partnership` — Send the partnership request message\n"
            "`/setup_moderator_signup` — Send the moderator sign-up message\n"
            "`/announce` — Send an announcement to #announcement\n"
            "`/statusweb` toggle buttons — Change website status\n\n"
            "**🔴 Dev Only (restricted to the Dev user or Dev role):**\n"
            "`/dm` — Send a DM to a user as the bot\n"
            "`/broadcast` — Send a message to all channels\n"
            "`/reload` — Resync slash commands\n"
            "`/shutdown` — Safely shut down the bot\n"
            "`/enable` / `/disable` — Turn AI chat on or off"
        ),
        inline=False
    )

    embed.set_footer(text="3DRBX-MGT · Protogen Security")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="untimeout", description="Remove timeout from a member early (mod only)")
@app_commands.describe(member="Member to remove timeout from")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    try:
        await member.timeout(None)
        await interaction.response.send_message(f"🔊 Timeout removed for {member.mention}.")
    except discord.Forbidden:
        await interaction.response.send_message("⚠ I don't have permission to do this.", ephemeral=True)


@untimeout.error
async def untimeout_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="slowmode", description="Set slowmode delay for this channel (mod only)")
@app_commands.describe(seconds="Delay in seconds (0 to disable, max 21600)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: int):
    if seconds < 0 or seconds > 21600:
        await interaction.response.send_message("⚠ Seconds must be between 0 and 21600.", ephemeral=True)
        return
    try:
        await interaction.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await interaction.response.send_message("✅ Slowmode disabled for this channel.")
        else:
            await interaction.response.send_message(f"🐢 Slowmode set to {seconds} second(s) for this channel.")
    except discord.Forbidden:
        await interaction.response.send_message("⚠ I don't have permission to do this.", ephemeral=True)


@slowmode.error
async def slowmode_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="lock", description="Lock this channel so only mods can send messages (mod only)")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    try:
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message("🔒 Channel locked. Only mods can send messages now.")
    except discord.Forbidden:
        await interaction.response.send_message("⚠ I don't have permission to do this.", ephemeral=True)


@lock.error
async def lock_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="unlock", description="Unlock this channel so everyone can send messages again (mod only)")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    try:
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message("🔓 Channel unlocked. Everyone can send messages again.")
    except discord.Forbidden:
        await interaction.response.send_message("⚠ I don't have permission to do this.", ephemeral=True)


@unlock.error
async def unlock_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="raidlockdown", description="EMERGENCY: lock every text channel server-wide (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def raidlockdown(interaction: discord.Interaction):
    await interaction.response.defer()
    locked = await lock_guild(interaction.guild)
    await interaction.followup.send(f"🚨 Server-wide lockdown activated. {locked} channel(s) locked.")
    await alert_mods(
        interaction.guild,
        discord.Embed(
            title="🚨 Server Lockdown Activated",
            description=f"Triggered manually by {interaction.user.mention}. {locked} channel(s) locked.",
            color=0xFF0000
        )
    )


@raidlockdown.error
async def raidlockdown_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="raidunlock", description="Reverse a server-wide raid lockdown (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def raidunlock(interaction: discord.Interaction):
    await interaction.response.defer()
    unlocked = await unlock_guild(interaction.guild)
    await interaction.followup.send(f"✅ Lockdown lifted. {unlocked} channel(s) unlocked.")


@raidunlock.error
async def raidunlock_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


def is_dev_or_dev_role(interaction: discord.Interaction) -> bool:
    if interaction.user.id == DEV_USER_ID:
        return True
    if isinstance(interaction.user, discord.Member):
        return any(r.name == "Dev" for r in interaction.user.roles)
    return False


@bot.tree.command(name="enable", description="Turn on AI chat (mention the bot to talk) (Dev only)")
async def enable_ai_chat(interaction: discord.Interaction):
    if not is_dev_or_dev_role(interaction):
        await interaction.response.send_message("⛔ This command is restricted to the Dev only.", ephemeral=True)
        return
    _ai_chat_enabled[interaction.guild.id] = True
    await interaction.response.send_message("✅ AI chat is now **enabled** — mention me to chat!")


@bot.tree.command(name="disable", description="Turn off AI chat (Dev only)")
async def disable_ai_chat(interaction: discord.Interaction):
    if not is_dev_or_dev_role(interaction):
        await interaction.response.send_message("⛔ This command is restricted to the Dev only.", ephemeral=True)
        return
    _ai_chat_enabled[interaction.guild.id] = False
    await interaction.response.send_message("🔇 AI chat is now **disabled**.")


@bot.tree.command(name="imagine", description="Generate an image from a text prompt (in #ai-chat)")
@app_commands.describe(prompt="Describe the image you want")
async def imagine(interaction: discord.Interaction, prompt: str):
    if interaction.channel.name != AI_CHAT_CHANNEL_NAME:
        target_channel = discord.utils.get(interaction.guild.text_channels, name=AI_CHAT_CHANNEL_NAME)
        await interaction.response.send_message(
            f"💬 This command only works in "
            f"{f'<#{target_channel.id}>' if target_channel else f'#{AI_CHAT_CHANNEL_NAME}'}.",
            ephemeral=True
        )
        return

    if not _ai_chat_enabled[interaction.guild.id]:
        await interaction.response.send_message("🔇 AI features are currently disabled.", ephemeral=True)
        return

    await interaction.response.defer()
    image_bytes = await generate_image(prompt)

    if image_bytes is None:
        await interaction.followup.send("⚠ Couldn't generate that image — try again in a moment.")
        return

    file = discord.File(io.BytesIO(image_bytes), filename="imagine.png")
    embed = discord.Embed(title="🎨 Generated Image", description=f"**Prompt:** {prompt}", color=0x9B59B6)
    embed.set_image(url="attachment://imagine.png")
    embed.set_footer(text=f"Requested by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, file=file, allowed_mentions=NO_PING)


@bot.tree.command(name="removewarning", description="Remove a specific warning from a member (mod only)")
@app_commands.describe(member="Member to remove warning from", warning_id="Warning ID to remove (from /warnings list)")
@app_commands.checks.has_permissions(moderate_members=True)
async def removewarning(interaction: discord.Interaction, member: discord.Member, warning_id: int):
    await interaction.response.defer()
    success = remove_warning(warning_id)
    if not success:
        await interaction.followup.send("⚠ Failed to remove warning (check the ID).", ephemeral=True)
        return
    remaining = len(get_warnings(member.id))
    await interaction.followup.send(
        f"✅ Removed warning (ID:{warning_id}) from {member.mention}.\nRemaining warnings: {remaining}"
    )


@removewarning.error
async def removewarning_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


@bot.tree.command(name="nickname", description="Change a member's nickname (mod only)")
@app_commands.describe(member="Member to rename", new_nickname="New nickname (leave empty to reset)")
@app_commands.checks.has_permissions(manage_nicknames=True)
async def nickname(interaction: discord.Interaction, member: discord.Member, new_nickname: str = None):
    if member.id == DEV_USER_ID or any(r.name in PROTECTED_ROLES for r in member.roles):
        await interaction.response.send_message("⛔ This member is protected and cannot be renamed.", ephemeral=True)
        return
    try:
        await member.edit(nick=new_nickname)
        if new_nickname:
            await interaction.response.send_message(f"✅ {member.mention}'s nickname changed to **{new_nickname}**.")
        else:
            await interaction.response.send_message(f"✅ {member.mention}'s nickname has been reset.")
    except discord.Forbidden:
        await interaction.response.send_message("⚠ I don't have permission to do this (check role hierarchy).", ephemeral=True)


@nickname.error
async def nickname_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


DEV_USER_ID = 980731561433530388


def is_dev(interaction: discord.Interaction) -> bool:
    return interaction.user.id == DEV_USER_ID


@bot.tree.command(name="dm", description="Send a DM to a user as the bot (Dev only)")
@app_commands.describe(user="User to DM", message="Message content")
async def dm(interaction: discord.Interaction, user: discord.User, message: str):
    if not is_dev(interaction):
        await interaction.response.send_message("⛔ This command is restricted to the Dev only.", ephemeral=True)
        return
    try:
        await user.send(f"📩 Message from 3DRBX-MGT Dev:\n\n{message}")
        await interaction.response.send_message(f"✓ DM sent to {user.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("⚠ Couldn't DM this user (they may have DMs disabled).", ephemeral=True)


_last_broadcast_at = {"time": 0}
BROADCAST_COOLDOWN_SECONDS = 30

@bot.tree.command(name="broadcast", description="Send a message to one or all text channels (Dev only)")
@app_commands.describe(
    message="Message to broadcast",
    target="Choose a specific channel, or leave empty to send to ALL channels",
    minutes_before_delete="Auto-delete after this many minutes (default 120 = 2 hours, 0 = never delete)"
)
async def broadcast(interaction: discord.Interaction, message: str, target: discord.TextChannel = None, minutes_before_delete: int = 120):
    if not is_dev(interaction):
        await interaction.response.send_message("⛔ This command is restricted to the Dev only.", ephemeral=True)
        return

    now = time.time()
    elapsed = now - _last_broadcast_at["time"]
    if elapsed < BROADCAST_COOLDOWN_SECONDS:
        wait = round(BROADCAST_COOLDOWN_SECONDS - elapsed, 1)
        await interaction.response.send_message(
            f"⏳ Broadcast is on cooldown, try again in {wait}s (prevents Discord rate limiting).",
            ephemeral=True
        )
        return
    _last_broadcast_at["time"] = now

    await interaction.response.defer(ephemeral=True)

    channels = [target] if target else list(interaction.guild.text_channels)
    sent_count = 0

    for channel in channels:
        try:
            sent_msg = await channel.send(message)
            sent_count += 1
            if minutes_before_delete > 0:
                await sent_msg.delete(delay=minutes_before_delete * 60)
            if not target:
                await asyncio.sleep(1)  # small delay between channels to avoid hammering the API
        except discord.Forbidden:
            continue
        except discord.HTTPException as e:
            logger.warning(f"Broadcast failed in #{channel.name}: {e}")
            continue

    scope_text = f"#{target.name}" if target else f"{sent_count} channel(s)"
    delete_text = f"auto-delete in {minutes_before_delete} min" if minutes_before_delete > 0 else "no auto-delete"
    await interaction.followup.send(f"✓ Broadcast sent to {scope_text} ({delete_text}).", ephemeral=True)


@bot.tree.command(name="reload", description="Resync slash commands without restarting the bot (Dev only)")
async def reload(interaction: discord.Interaction):
    if not is_dev(interaction):
        await interaction.response.send_message("⛔ This command is restricted to the Dev only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        synced = await bot.tree.sync()
        await interaction.followup.send(f"✓ Resynced {len(synced)} slash command(s).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"✗ Failed to resync: {e}", ephemeral=True)


@bot.tree.command(name="shutdown", description="Safely shut down the bot (Dev only)")
async def shutdown(interaction: discord.Interaction):
    if not is_dev(interaction):
        await interaction.response.send_message("⛔ This command is restricted to the Dev only.", ephemeral=True)
        return

    await interaction.response.send_message("🔴 Shutting down... See you soon!", ephemeral=True)
    await bot.close()


YOUTUBE_UPLOAD_CHANNEL = "youtube-upload"


@bot.tree.command(name="postvideo", description="Post a new YouTube video (Content Creator only)")
@app_commands.describe(title="Video title", link="YouTube video link", description="Video description (optional)")
async def postvideo(interaction: discord.Interaction, title: str, link: str, description: str = None):
    has_role = any(r.name == "Content Creator" for r in interaction.user.roles)
    if not has_role:
        await interaction.response.send_message(
            "⛔ This command is restricted to members with the **Content Creator** role.",
            ephemeral=True
        )
        return

    channel = discord.utils.get(interaction.guild.text_channels, name=YOUTUBE_UPLOAD_CHANNEL)
    if channel is None:
        await interaction.response.send_message(
            f"⚠ Channel #{YOUTUBE_UPLOAD_CHANNEL} not found.",
            ephemeral=True
        )
        return

    ping_role = discord.utils.get(interaction.guild.roles, name="YouTube Ping")
    ping_mention = ping_role.mention if ping_role else ""

    embed = discord.Embed(
        title=f"🎬 {title}",
        description=description or "No description provided.",
        color=0xFF0000,
        url=link
    )
    embed.add_field(name="Watch here", value=link, inline=False)
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="3DRBX-MGT · New Upload")

    try:
        await channel.send(content=ping_mention, embed=embed)
        await interaction.response.send_message(f"✓ Video posted to #{YOUTUBE_UPLOAD_CHANNEL}!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠ The bot doesn't have permission to post in that channel.",
            ephemeral=True
        )


@bot.tree.command(name="deletedpythonfileproto", description="Warn the channel about command spam (mod only)")
@app_commands.checks.has_permissions(moderate_members=True)
async def deletedpythonfileproto(interaction: discord.Interaction):
    await interaction.response.send_message("💀 `bot.py` has crashed due to excessive command spam...")
    msg = await interaction.original_response()

    import asyncio
    await asyncio.sleep(1.5)
    await msg.edit(content="💀 `bot.py` has crashed due to excessive command spam...\n`Traceback: MemoryError`")
    await asyncio.sleep(1.5)
    await msg.edit(content=(
        "💀 `bot.py` has crashed due to excessive command spam...\n`Traceback: MemoryError`\n\n"
        "⚠️ Protogen Security is shutting down to prevent damage..."
    ))
    await asyncio.sleep(2)
    await msg.edit(content=(
        "💀 `bot.py` has crashed due to excessive command spam...\n`Traceback: MemoryError`\n\n"
        "⚠️ Protogen Security is shutting down to prevent damage...\n\n"
        "Just kidding — but please stop spamming commands! 😅 Keep it chill, folks."
    ))


@deletedpythonfileproto.error
async def deletedpythonfileproto_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠ You don't have permission to run this command.", ephemeral=True)


def run_bot_forever():
    """
    Runs the bot with crash handling + exponential backoff.
    This keeps ONE process alive across transient errors instead of relying
    on the host to restart the whole process (which triggers a fresh
    IDENTIFY to Discord's gateway and can contribute to rate limiting).
    """
    backoff = 5  # seconds
    max_backoff = 300  # 5 minutes

    while True:
        try:
            if not TOKEN:
                logger.critical("DISCORD_BOT_TOKEN is not set. Exiting.")
                return
            bot_status["ready"] = False
            bot.run(TOKEN, log_handler=None)
            # bot.run() only returns after a clean shutdown (e.g. /shutdown command)
            logger.info("Bot shut down cleanly. Not restarting.")
            return
        except discord.LoginFailure:
            logger.critical("Invalid Discord token. Check DISCORD_BOT_TOKEN. Exiting.")
            return
        except discord.HTTPException as e:
            if e.status == 429:
                logger.error(f"Rate limited by Discord (429). Backing off for {backoff}s.")
            else:
                logger.error(f"Discord HTTP error: {e}. Retrying in {backoff}s.")
        except Exception as e:
            logger.error(f"Unhandled exception in bot.run(): {e}", exc_info=True)

        bot_status["ready"] = False
        bot_status["last_disconnect_at"] = time.time()
        logger.info(f"Reconnecting in {backoff}s...")
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)  # exponential backoff, capped at 5 min


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_forever()
