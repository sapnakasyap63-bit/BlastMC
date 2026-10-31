import os
import re
import json
import time
import asyncio
import logging
from collections import deque

import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread

# -------------------- CONFIG --------------------
TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", "8080"))
PREFIX = "?"
ACTIVITY_TEXT = "BEST FOR MINECRAFT SMP"
DATA_FILE = os.getenv("DATA_FILE", "bot_data.json")
DUPLICATE_WINDOW = 5  # seconds

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("smp-bot")

# -------------------- RENDER WEB SERVICE --------------------
app = Flask(__name__)

@app.get("/")
def home():
    return "Minecraft SMP Discord Bot is online!", 200

@app.get("/health")
def health():
    return {"status": "ok"}, 200

def run_web():
    app.run(host="0.0.0.0", port=PORT)

Thread(target=run_web, daemon=True).start()

# -------------------- DISCORD BOT --------------------
intents = discord.Intents.default()
intents.message_content = True  # Enable Message Content Intent in Developer Portal
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
recent_bot_messages = {}  # (channel_id, fingerprint) -> timestamp
auto_replies = {}         # guild_id -> {keyword: rule_dict}
data_lock = asyncio.Lock()

def load_data():
    global auto_replies
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        
        loaded = {}
        for guild_id, values in raw.get("auto_replies", {}).items():
            loaded[int(guild_id)] = {}
            for k, v in values.items():
                if isinstance(v, str):
                    loaded[int(guild_id)][str(k).lower()] = {
                        "title": "🤖 SMP Auto Reply",
                        "response": v,
                        "matchmode": "contains"
                    }
                else:
                    loaded[int(guild_id)][str(k).lower()] = v
        auto_replies = loaded
    except FileNotFoundError:
        auto_replies = {}
    except Exception:
        log.exception("Could not load bot data; starting with empty auto-replies.")
        auto_replies = {}

async def save_data():
    async with data_lock:
        tmp = DATA_FILE + ".tmp"
        payload = {"auto_replies": {str(g): values for g, values in auto_replies.items()}}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)

def admin_interaction(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and interaction.user.guild_permissions.administrator)

def make_embed(title: str, description: str, color: discord.Color = discord.Color.blurple()):
    return discord.Embed(title=title, description=description, color=color)

@bot.event
async def on_ready():
    load_data()
    try:
        synced = await bot.tree.sync()
        log.info("Synced %s slash commands.", len(synced))
    except Exception:
        log.exception("Slash command sync failed.")
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(name=ACTIVITY_TEXT)
    )
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

@bot.event
async def on_message(message: discord.Message):
    if bot.user and message.author.id == bot.user.id:
        now = time.monotonic()
        fingerprint = (
            message.channel.id,
            message.content,
            tuple(embed.to_dict().__repr__() for embed in message.embeds)
        )
        previous = recent_bot_messages.get(fingerprint)
        recent_bot_messages[fingerprint] = now

        for key, stamp in list(recent_bot_messages.items()):
            if now - stamp > DUPLICATE_WINDOW:
                recent_bot_messages.pop(key, None)

        if previous is not None and now - previous <= DUPLICATE_WINDOW:
            try:
                await message.delete()
                log.info("Deleted a duplicate bot message in channel %s", message.channel.id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                log.warning("Could not delete duplicate bot message.")
        return

    if message.author.bot or message.guild is None:
        return

    # Keyword auto-responder logic (Sirf Title aur Response, no extra footer/description)
    guild_replies = auto_replies.get(message.guild.id, {})
    content = message.content.strip().lower()
    
    for keyword, rule in guild_replies.items():
        matchmode = rule.get("matchmode", "contains")
        is_match = False

        if matchmode == "exact":
            is_match = (content == keyword.lower())
        else:
            is_match = bool(re.search(r"(?<!\\w)" + re.escape(keyword) + r"(?!\\w)", content))

        if is_match:
            embed = discord.Embed(
                title=rule.get("title", ""),
                description=rule.get("response", ""),
                color=discord.Color.blurple()
            )
            try:
                await message.channel.send(embed=embed)
            except discord.HTTPException:
                log.exception("Auto-reply send failed.")
            break

    await bot.process_commands(message)

# -------------------- PREFIX COMMANDS --------------------
@bot.command(name="help")
async def help_prefix(ctx):
    embed = make_embed(
        "📘 SMP Bot Help",
        "**Prefix:** `?`\\n"
        "`?dm @user your message` — Send a DM embed via mention (admins only)\\n"
        "`/ping` — Check bot latency\\n"
        "`/embed` — Send an embed (admins only)\\n"
        "`/autoreply add` — Add an auto-reply rule\\n"
        "`/autoreply remove` — Remove a keyword\\n"
        "`/autoreply list` — List auto-replies"
    )
    await ctx.reply(embed=embed, mention_author=False)

# -------------------- SLASH COMMANDS --------------------
@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=make_embed("🏓 Pong!", f"Latency: **{round(bot.latency * 1000)} ms**"),
        ephemeral=True
    )

@bot.tree.command(name="embed", description="Send a custom embed in this channel (admins only).")
@app_commands.describe(title="Embed title", message="Embed description")
async def embed_command(interaction: discord.Interaction, title: str, message: str):
    if not admin_interaction(interaction):
        return await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)
    if len(title) > 256 or len(message) > 4000:
        return await interaction.response.send_message("Title/description is too long.", ephemeral=True)
    await interaction.response.send_message("✅ Embed sent.", ephemeral=True)
    out = make_embed(title, message)
    out.set_footer(text="BEST FOR MINECRAFT SMP")
    await interaction.channel.send(embed=out)

autoreply_group = app_commands.Group(name="autoreply", description="Manage keyword-based embed auto replies.")

@autoreply_group.command(name="add", description="Add or update an auto-reply rule.")
@app_commands.describe(
    title="Title of the embed",
    keyword="Trigger keyword or phrase",
    response="Text shown inside the embed",
    matchmode="Choose exact match or contains match"
)
@app_commands.choices(matchmode=[
    app_commands.Choice(name="Exact Match", value="exact"),
    app_commands.Choice(name="Contains Keyword", value="contains")
])
async def autoreply_add(interaction: discord.Interaction, title: str, keyword: str, response: str, matchmode: str):
    if not admin_interaction(interaction):
        return await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)
    
    clean_keyword = keyword.strip().lower()
    if not clean_keyword or len(clean_keyword) > 80:
        return await interaction.response.send_message("❌ Keyword must be between 1 and 80 characters.", ephemeral=True)
    
    auto_replies.setdefault(interaction.guild_id, {})[clean_keyword] = {
        "title": title.strip(),
        "response": response.strip(),
        "matchmode": matchmode
    }
    await save_data()
    
    await interaction.response.send_message(f"✅ Auto-reply rule added successfully for keyword: **{discord.utils.escape_markdown(clean_keyword)}** (`{matchmode}` mode)", ephemeral=True)

@autoreply_group.command(name="remove", description="Remove a keyword auto-reply.")
@app_commands.describe(keyword="Keyword to remove")
async def autoreply_remove(interaction: discord.Interaction, keyword: str):
    if not admin_interaction(interaction):
        return await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)
    keyword = keyword.strip().lower()
    replies = auto_replies.get(interaction.guild_id, {})
    if keyword not in replies:
        return await interaction.response.send_message("That keyword is not configured.", ephemeral=True)
    del replies[keyword]
    await save_data()
    await interaction.response.send_message(f"✅ Removed **{discord.utils.escape_markdown(keyword)}**.", ephemeral=True)

@autoreply_group.command(name="list", description="Show configured auto-reply keywords.")
async def autoreply_list(interaction: discord.Interaction):
    if not admin_interaction(interaction):
        return await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)
    replies = auto_replies.get(interaction.guild_id, {})
    if not replies:
        return await interaction.response.send_message("No auto-replies configured yet.", ephemeral=True)
    
    lines = []
    for k, v in list(replies.items())[:30]:
        mode = v.get("matchmode", "contains")
        lines.append(f"• **{discord.utils.escape_markdown(k)}** (`{mode}`)")
        
    await interaction.response.send_message(embed=make_embed("⚙️ Auto Replies", "\\n".join(lines)), ephemeral=True)

bot.tree.add_command(autoreply_group)

# -------------------- MENTION DM COMMAND --------------------
@bot.listen("on_message")
async def handle_dm_prefix(message: discord.Message):
    if message.author.bot or not message.content.startswith("?dm"):
        return
    if message.guild is None:
        return
    if not message.author.guild_permissions.administrator:
        await message.reply("❌ Only server administrators can use this command.", mention_author=False)
        return

    target_user = message.mentions[0] if message.mentions else None
    if not target_user:
        await message.reply("❌ Usage: `?dm @user your message here`", mention_author=False)
        return

    parts = message.content.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("❌ Kripya bhejne ke liye message bhi likhein! Usage: `?dm @user your message`", mention_author=False)
        return

    dm_text = parts[2].strip()
    if len(dm_text) > 3500:
        await message.reply("❌ Message is too long (max 3500 characters).", mention_author=False)
        return

    try:
        dm_embed = make_embed("📩 Message from the SMP Team", dm_text)
        dm_embed.set_footer(text=f"Sent by {message.author}")
        await target_user.send(embed=dm_embed)
        await message.reply(f"✅ Embed DM sent to **{discord.utils.escape_markdown(str(target_user))}**.", mention_author=False)
    except discord.Forbidden:
        await message.reply("⚠️ DM could not be sent. The user may have DMs disabled or blocked the bot.", mention_author=False)
    except discord.HTTPException:
        log.exception("DM failed.")
        await message.reply("❌ Discord could not deliver the DM. Please try again later.", mention_author=False)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return await ctx.reply("❌ You do not have permission to use this command.", mention_author=False)
    log.error("Command error: %s", error)
    try:
        await ctx.reply("❌ Command failed. Check the bot logs for details.", mention_author=False)
    except discord.HTTPException:
        pass

if not TOKEN:
    log.error("DISCORD_TOKEN environment variable is missing. Add it in Render Environment.")
else:
    bot.run(TOKEN)
