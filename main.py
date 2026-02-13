import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import asyncio
import os
from dotenv import load_dotenv
import time
import logging
import re

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
PROXY_URL = os.getenv('HTTP_PROXY')

# API Endpoints
WAR_API_BASE = "https://war-service-live.foxholeservices.com/api"
SHARD_STATUS_URL = "https://war-service-live.foxholeservices.com/external/shardStatus/servers"

# Configuration
REQUIRED_ROLE_NAME = "404th"  # Change this to the role name you want!
ADMIN_USERNAME = "darksteeldragon"

# Form automation (👀 reaction → parse steam link → create thread → post normalized link)
# ⚠️ ВАЖНО: Укажите ID сервера, где БОТ УЖЕ НАХОДИТСЯ!
# Ваши серверы: 948605596045803552 или 1470823990644703363
FORMS_GUILD_ID = 1470823990644703363  # Замените на нужный сервер
FORMS_CHANNEL_ID = 527455866216120321  # Замените на ID канала на ВАШЕМ сервере
FORMS_THREAD_NAME = "Форма принята к рассмотрению"

STEAM_URL_RE = re.compile(r"https?://steamcommunity\.com/(?:id|profiles)/[^\s)]+", re.IGNORECASE)
PROCESSED_FORM_MESSAGE_IDS: set[int] = set()

# Setup Bot (intents.members ОБЯЗАТЕЛЕН для проверки ролей — включи в Developer Portal → Bot → Privileged Gateway Intents)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, proxy=PROXY_URL)

async def check_access(interaction: discord.Interaction) -> bool:
    """Checks if user is allowed to use the bot"""
    user = interaction.user
    guild_id = interaction.guild_id

    # 1. Админ (обход проверки роли)
    if user.name.lower() == ADMIN_USERNAME.lower():
        log.info(f"[ACCESS OK] Admin {user.name}")
        return True

    # 2. Проверка что бот вообще в этой гильдии
    guild = interaction.guild
    if not guild:
        log.warning("[ACCESS DENY] No guild in interaction")
        return False
    
    # Проверяем что бот состоит в этой гильдии
    if guild.id not in [g.id for g in bot.guilds]:
        log.warning(f"[ACCESS DENY] Bot is not in guild {guild.id}. Bot guilds: {[g.id for g in bot.guilds]}")
        return False

    # 3. Проверка роли
    member = user if isinstance(user, discord.Member) else None
    if member is None or not member.roles:
        try:
            member = await guild.fetch_member(user.id)
        except Exception as e:
            log.warning(f"[ACCESS DENY] Could not fetch member: {e}")
            return False

    roles = list(member.roles)
    role_names = [r.name for r in roles]
    log.info(f"[CHECK] User '{member.name}' | Guild {guild_id} | Roles: {role_names}")

    for role in roles:
        if role.name.lower() == REQUIRED_ROLE_NAME.lower():
            log.info(f"[ACCESS OK] Role match: '{role.name}'")
            return True

    log.info(f"[ACCESS DENY] No role '{REQUIRED_ROLE_NAME}' in {role_names}")
    return False

def _extract_steam_url_from_text(text: str) -> str | None:
    if not text:
        return None
    m = STEAM_URL_RE.search(text)
    return m.group(0) if m else None

def extract_steam_url_from_message(msg: discord.Message) -> str | None:
    # 1) message content
    url = _extract_steam_url_from_text(getattr(msg, "content", "") or "")
    if url:
        return url

    # 2) embeds (forms часто приходят как embed)
    for emb in getattr(msg, "embeds", []) or []:
        # description
        url = _extract_steam_url_from_text(getattr(emb, "description", "") or "")
        if url:
            return url
        # fields
        for f in getattr(emb, "fields", []) or []:
            url = _extract_steam_url_from_text(getattr(f, "value", "") or "")
            if url:
                return url
    return None

async def normalize_steam_url(url: str) -> str:
    """
    Приводит steamcommunity ссылку к формату:
      https://steamcommunity.com/profiles/<steamid64>/
    Если уже profiles — возвращает как есть.
    Если vanity /id/<name>/ — резолвит редиректом до /profiles/.
    """
    if not url:
        return url

    # Already profiles
    if "/profiles/" in url:
        # normalize: keep base profiles/<id>/
        m = re.search(r"(https?://steamcommunity\.com/profiles/\d+/?).*", url, re.IGNORECASE)
        return (m.group(1) if m else url).rstrip("/") + "/"

    # Vanity /id/...
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; foxhole-queue-bot/1.0)"}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True, proxy=PROXY_URL) as resp:
                final_url = str(resp.url)
                m = re.search(r"(https?://steamcommunity\.com/profiles/\d+/?).*", final_url, re.IGNORECASE)
                if m:
                    return m.group(1).rstrip("/") + "/"
                return final_url
    except Exception as e:
        log.warning(f"normalize_steam_url failed for {url}: {e}")
        return url

# Global Cache
map_names = []  # List of official map names
queue_cache = None
QUEUE_CACHE_TTL = 180  # 3 минуты

async def fetch_maps():
    global map_names
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WAR_API_BASE}/worldconquest/maps", proxy=PROXY_URL) as response:
                if response.status == 200:
                    map_names = await response.json()
                    log.info(f"Loaded {len(map_names)} maps.")
                else:
                    log.warning(f"Failed to load maps: {response.status}")
    except Exception as e:
        log.error(f"Error fetching maps: {e}")

async def fetch_queue_data():
    global queue_cache
    
    # Check cache
    if queue_cache:
        data, timestamp = queue_cache
        if time.time() - timestamp < QUEUE_CACHE_TTL:
            return data

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(SHARD_STATUS_URL, proxy=PROXY_URL) as response:
                if response.status == 200:
                    data = await response.json()
                    queue_cache = (data, time.time())
                    return data
                else:
                    log.warning(f"Failed to fetch queue data: {response.status}")
    except Exception as e:
        log.error(f"Error fetching queues: {e}")
    return None

def clean_map_name(name):
    """Removes 'Hex' suffix and formats name"""
    if name.endswith('Hex'):
        name = name[:-3]
    return name

def fmt_num(n: int) -> str:
    """Zero = plain, non-zero = bold (visual distinction)"""
    return f"**{n}**" if n > 0 else "0"

def fmt_cell(n: int, is_colonial: bool) -> str:
    """Без квадрата. Жирный если >10, 🔴 если >30. 🟢 колония, 🔵 вардены."""
    dot = "🟢 " if is_colonial else "🔵 "
    if n == 0:
        return dot + "0"
    num_str = str(n)
    if n > 10:
        num_str = f"**{n}**"
    if n > 30:
        return dot + "🔴 " + num_str
    return dot + num_str

def embed_color_by_queue(total: int) -> int:
    """Цвет полоски эмбеда: зелёный (спокойно) → жёлтый → красный (много в очередях)."""
    if total == 0:
        return 0x22B822   # зелёный
    if total < 50:
        return 0xE8C520   # жёлтый
    return 0xC92C2C       # красный

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user.name}, guilds: {[g.id for g in bot.guilds]}")
    await fetch_maps()
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Обработка реакций 👀 на формы для создания тредов со Steam ссылками"""
    try:
        # Проверяем что бот есть в гильдии откуда пришла реакция
        guild = bot.get_guild(payload.guild_id)
        if not guild:
            log.warning(f"[FORMS] Bot is not in guild {payload.guild_id}. Skipping reaction.")
            return
        
        # Проверка что это нужный канал и гильдия
        if payload.guild_id != FORMS_GUILD_ID or payload.channel_id != FORMS_CHANNEL_ID:
            return
            
        # Игнорируем реакции самого бота
        if payload.user_id == (bot.user.id if bot.user else None):
            return
            
        # Только реакция 👀
        if getattr(payload.emoji, "name", None) != "👀":
            return

        # Избегаем дубликатов
        if payload.message_id in PROCESSED_FORM_MESSAGE_IDS:
            return
        PROCESSED_FORM_MESSAGE_IDS.add(payload.message_id)

        # Получаем канал и сообщение
        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning("[FORMS] Channel is not a TextChannel/Thread")
            return

        msg = await channel.fetch_message(payload.message_id)
        steam_url = extract_steam_url_from_message(msg)
        if not steam_url:
            log.warning(f"[FORMS] No steam URL found in message {msg.id}")
            return

        # Нормализуем ссылку
        normalized = await normalize_steam_url(steam_url)

        # Создаём тред или используем существующий
        thread = getattr(msg, "thread", None)
        if thread is None:
            thread = await msg.create_thread(name=FORMS_THREAD_NAME, auto_archive_duration=1440)

        await thread.send(f"Steam: {normalized}")
        log.info(f"[FORMS] Posted normalized steam link for message {msg.id}: {normalized}")
    except Exception as e:
        log.error(f"[FORMS] on_raw_reaction_add error: {e}")

@bot.command(name="guildid", help="Показать ID сервера (для добавления в whitelist)")
async def guildid(ctx):
    await ctx.send(f"ID этого сервера: `{ctx.guild.id}`")

@bot.command(name="guilds", help="Показать все сервера бота (только для админа)")
async def show_guilds(ctx):
    if ctx.author.name.lower() != ADMIN_USERNAME.lower():
        await ctx.send("❌ Команда только для администратора")
        return
    
    guild_list = "\n".join([f"- **{g.name}** (ID: `{g.id}`)" for g in bot.guilds])
    await ctx.send(f"**🏰 Бот находится на серверах:**\n{guild_list}")

@bot.command(name="sync", help="Синхронизировать команды (для админа)")
async def sync(ctx):
    if ctx.author.name.lower() != ADMIN_USERNAME.lower():
        await ctx.send("❌ Команда только для администратора")
        return
        
    msg = await ctx.send("🔄 Синхронизация команд...")
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await msg.edit(content=f"✅ Успешно синхронизировано {len(synced)} команд для этого сервера! Теперь они должны появиться мгновенно.")
    except Exception as e:
        await msg.edit(content=f"❌ Ошибка синхронизации: {e}")

@bot.tree.command(name="queues", description="Показать очереди на всех картах (видно только вам)")
async def queues(interaction: discord.Interaction):
    if not await check_access(interaction):
        await interaction.response.send_message(
            "❌ У вас нет доступа к этому боту.\n"
            f"Необходима роль **{REQUIRED_ROLE_NAME}** или обратитесь к @{ADMIN_USERNAME}",
            ephemeral=True
        )
        return

    # Defer response since fetching might take > 3 seconds
    await interaction.response.defer(ephemeral=True)
    
    servers = await fetch_queue_data()
    
    if not servers:
        await interaction.followup.send("❌ Не удалось получить данные об очередях. API может быть недоступен.", ephemeral=True)
        return

    # Filter servers that have queues
    active_queues = []
    
    for server in servers:
        map_name = server.get('currentMap', 'Unknown')
        
        # Skip HomeRegion servers (they are lobby servers)
        if 'HomeRegion' in map_name:
            continue
            
        col_q = server.get('colonialQueueSize', 0)
        ward_q = server.get('wardenQueueSize', 0)
        
        if col_q > 0 or ward_q > 0:
            active_queues.append({
                'name': clean_map_name(map_name),
                'col': col_q,
                'ward': ward_q,
                'total': col_q + ward_q
            })
    
    # Sort by total queue size
    active_queues.sort(key=lambda x: x['total'], reverse=True)
    
    # Calculate total queues for summary
    total_col_queue = sum(s.get('colonialQueueSize', 0) for s in servers)
    total_ward_queue = sum(s.get('wardenQueueSize', 0) for s in servers)

    total_in_queues = total_col_queue + total_ward_queue

    if not active_queues:
        emb = discord.Embed(
            title="🚀 Очереди в реальном времени",
            description="✅ **Очередей нет!** На фронте тихо... пока что.",
            color=embed_color_by_queue(0),
        )
        await interaction.followup.send(embed=emb, ephemeral=True)
        return

    # Компактно: обрезка по короткому гексу, без лишних пробелов
    min_name_len = min(len(q["name"]) for q in active_queues)
    name_width = max(min_name_len, 6)
    body = f"**Всего** {fmt_cell(total_col_queue, True)} {fmt_cell(total_ward_queue, False)}\n\n"
    for q in active_queues:
        name_fixed = q["name"][:name_width]
        body += f"**{name_fixed}** {fmt_cell(q['col'], True)} {fmt_cell(q['ward'], False)}\n"

    emb = discord.Embed(
        title="🚀 Очереди в реальном времени",
        description=body,
        color=embed_color_by_queue(total_in_queues),
    )
    await interaction.followup.send(embed=emb, ephemeral=True)

@bot.tree.command(name="map", description="Показать очередь на конкретной карте")
@app_commands.describe(name="Название карты (на английском)")
async def map_stats(interaction: discord.Interaction, name: str):
    if not await check_access(interaction):
        await interaction.response.send_message(
            "❌ У вас нет доступа к этому боту.\n"
            f"Необходима роль **{REQUIRED_ROLE_NAME}** или обратитесь к @{ADMIN_USERNAME}",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
        
    servers = await fetch_queue_data()
    if not servers:
        await interaction.followup.send("❌ Не удалось получить данные.", ephemeral=True)
        return
        
    name_lower = name.lower().replace(" ", "")
    target_server = None
    
    for server in servers:
        server_map = server.get('currentMap', '').lower()
        if name_lower in server_map:
            target_server = server
            break
    
    if target_server:
        map_name = clean_map_name(target_server.get('currentMap', 'Unknown'))
        col_q = target_server.get('colonialQueueSize', 0)
        ward_q = target_server.get('wardenQueueSize', 0)
        col_slots = target_server.get('openColonialSlots', '?')
        ward_slots = target_server.get('openWardenSlots', '?')
        
        response = f"**📍 Статус {map_name}**\n"
        response += f"🟢 Колонисты: **{col_q}** в очереди (Слотов: {col_slots})\n"
        response += f"🔵 Вардены: **{ward_q}** в очереди (Слотов: {ward_slots})"
        
        await interaction.followup.send(response, ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Карта '{name}' не найдена или сервер оффлайн.", ephemeral=True)

@map_stats.autocomplete('name')
async def map_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if not map_names:
        # Try to fetch if empty, but don't await/block too long in autocomplete
        # Best effort: use what we have or empty list
        return []
    
    # Filter map names based on user input
    current_lower = current.lower()
    choices = [
        app_commands.Choice(name=m, value=m)
        for m in map_names
        if current_lower in m.lower()
    ]
    # Discord allows max 25 choices
    return choices[:25]

if __name__ == "__main__":
    if not TOKEN:
        print("❌ Error: DISCORD_TOKEN not found in .env file")
    else:
        bot.run(TOKEN)