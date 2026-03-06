import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
import os
from dotenv import load_dotenv
import time
import logging
import re
import datetime

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
ADMIN_USERNAME = "darkstelldragon"

# Form automation (👀 reaction → parse steam link → create thread → post normalized link)
# ⚠️ ВАЖНО: Укажите ID сервера, где БОТ УЖЕ НАХОДИТСЯ!
# Ваши серверы: 948605596045803552 или 1470823990644703363
FORMS_GUILD_ID = 355748261958647809   # Сервер с формами
FORMS_CHANNEL_ID = 527455866216120321  # Канал с формами
FORMS_THREAD_NAME = "Форма принята к рассмотрению"
# Канал со списком гриферов (Steam-ссылки). Если Steam из формы совпадает — ставим ❌ и пишем в тред.
GRIEFER_LIST_CHANNEL_ID = 767379832308760607
# Канал для пинга пользователей с кривой анкетой
PING_CHANNEL_ID = 585908906115727392
# Файл со списком SteamID64 гриферов (один ID на строку). Источник истины.
GRIEFERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "griefers.txt")

STEAM_URL_RE = re.compile(r"https?://steamcommunity\.com/(?:id|profiles)/[^\s)]+", re.IGNORECASE)
# Ловим ЛЮБЫЕ steam-подобные ссылки (включая кривые: /home, /login, store и т.д.)
STEAM_ANY_URL_RE = re.compile(r"https?://(?:store\.)?steam(?:community|powered)\.com[^\s)]*", re.IGNORECASE)
PROCESSED_FORM_MESSAGE_IDS: set[int] = set()

# Setup Bot (intents.members ОБЯЗАТЕЛЕН для проверки ролей — включи в Developer Portal → Bot → Privileged Gateway Intents)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True          # Явно включаем реакции для on_raw_reaction_add
bot = commands.Bot(command_prefix='!', intents=intents, proxy=PROXY_URL)

VETERAN_ROLE_NAME = "ветеран"

async def check_access_veteran(interaction: discord.Interaction) -> bool:
    """Проверка: только роль 'ветеран' или администратор."""
    user = interaction.user
    if user.name.lower() == ADMIN_USERNAME.lower():
        return True
    guild = interaction.guild
    if not guild:
        return False
    member = user if isinstance(user, discord.Member) else None
    if member is None or not member.roles:
        try:
            member = await guild.fetch_member(user.id)
        except Exception:
            return False
    for role in member.roles:
        if role.name.lower() == VETERAN_ROLE_NAME.lower():
            return True
    return False

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

def _extract_all_steam_urls_from_text(text: str) -> list[str]:
    """Все подходящие steamcommunity ссылки из текста (id или profiles)."""
    if not text:
        return []
    return STEAM_URL_RE.findall(text)

def _steam_id64_from_url(url: str) -> str | None:
    """Из нормализованного URL https://steamcommunity.com/profiles/76561198.../ извлекает steamID64."""
    if not url:
        return None
    m = re.search(r"steamcommunity\.com/profiles/(\d+)", url, re.IGNORECASE)
    return m.group(1) if m else None

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

async def normalize_steam_url(url: str) -> tuple[str, bool, bool]:
    """
    Приводит steamcommunity ссылку к формату /profiles/<steamid64>/.
    Возвращает (normalized_url, valid, was_recovered):
      valid — профиль существует
      was_recovered — vanity /id/ был преобразован в /profiles/
    """
    if not url:
        return url, False, False

    # Already profiles
    if "/profiles/" in url:
        m = re.search(r"(https?://steamcommunity\.com/profiles/\d+/?).*", url, re.IGNORECASE)
        clean = (m.group(1) if m else url).rstrip("/") + "/"
        valid = await _check_steam_profile_exists(clean)
        return clean, valid, False

    # Vanity /id/...
    vanity_match = re.search(r"steamcommunity\.com/id/([^/\s?#]+)", url, re.IGNORECASE)
    if not vanity_match:
        return url, False, False

    vanity_name = vanity_match.group(1)
    xml_url = f"https://steamcommunity.com/id/{vanity_name}/?xml=1"

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; foxhole-queue-bot/1.0)"}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(xml_url, proxy=PROXY_URL) as resp:
                if resp.status != 200:
                    log.warning(f"normalize_steam_url: HTTP {resp.status} for {xml_url}")
                    return url, False, False
                text = await resp.text()
                m = re.search(r"<steamID64>(\d+)</steamID64>", text)
                if m:
                    steam_id64 = m.group(1)
                    resolved = f"https://steamcommunity.com/profiles/{steam_id64}/"
                    log.info(f"[STEAM] Resolved vanity '{vanity_name}' → {resolved}")
                    return resolved, True, True
                else:
                    log.warning(f"normalize_steam_url: no steamID64 in XML for {vanity_name}")
                    return url, False, False
    except Exception as e:
        log.warning(f"normalize_steam_url failed for {url}: {e}")
        return url, False, False

def load_griefer_ids_from_file() -> set[str]:
    """Читает griefers.txt — по одному SteamID64 на строку. Нет файла или пустой = пустое множество."""
    ids: set[str] = set()
    if not os.path.isfile(GRIEFERS_FILE):
        return ids
    try:
        with open(GRIEFERS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                sid = line.strip()
                if sid.isdigit():
                    ids.add(sid)
    except Exception as e:
        log.warning(f"[GRIEFERS] Failed to read {GRIEFERS_FILE}: {e}")
    return ids

def save_griefer_ids_to_file(ids: set[str]) -> None:
    """Записывает список SteamID64 в griefers.txt (один на строку)."""
    with open(GRIEFERS_FILE, "w", encoding="utf-8") as f:
        for sid in sorted(ids):
            f.write(sid + "\n")

async def refresh_griefer_list(status_msg=None) -> int:
    """
    Парсит канал со списком гриферов батчами, сохраняет в griefers.txt.
    status_msg — сообщение для обновления прогресса (опционально).
    """
    channel = bot.get_channel(GRIEFER_LIST_CHANNEL_ID)
    if not channel or not isinstance(channel, discord.TextChannel):
        log.warning("[GRIEFERS] Channel not found")
        return 0

    collected: set[str] = set()
    msg_count = 0
    batch_size = 50

    async for message in channel.history(limit=None):
        for text in _iter_message_texts(message):
            for url in _extract_all_steam_urls_from_text(text):
                sid = _steam_id64_from_url(url)
                if sid:
                    collected.add(sid)
                    continue
                # Vanity — резолвим, но с паузой чтобы не убить Steam/сервер
                normalized, _, _ = await normalize_steam_url(url)
                sid = _steam_id64_from_url(normalized)
                if sid:
                    collected.add(sid)
                await asyncio.sleep(0.5)

        msg_count += 1
        if msg_count % batch_size == 0:
            save_griefer_ids_to_file(collected)
            if status_msg:
                try:
                    await status_msg.edit(content=f"🔄 Обработано {msg_count} сообщений, найдено {len(collected)} ID…")
                except Exception:
                    pass
            await asyncio.sleep(1)

    save_griefer_ids_to_file(collected)
    log.info(f"[GRIEFERS] Saved {len(collected)} Steam IDs from {msg_count} messages")
    return len(collected)

async def _check_steam_profile_exists(profile_url: str) -> bool:
    """Проверяет существует ли Steam профиль (через XML)."""
    try:
        xml_url = profile_url.rstrip("/") + "/?xml=1"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; foxhole-queue-bot/1.0)"}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(xml_url, proxy=PROXY_URL) as resp:
                if resp.status != 200:
                    return False
                text = await resp.text()
                # Если есть steamID64 — профиль существует
                return bool(re.search(r"<steamID64>\d+</steamID64>", text))
    except Exception:
        return False

# Канал с репутациями рекрутов
REP_CHANNEL_ID = 431015270279282698
REP_LOOKBACK_DAYS = 365

# rep_cache: user_id -> list of (message_content, message_url, message_timestamp)
rep_cache: dict[int, list[tuple[str, str, datetime.datetime]]] = {}

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
    return dot + num_str

def embed_color_by_queue(total: int) -> int:
    """Цвет полоски эмбеда: зелёный (спокойно) → жёлтый → красный (много в очередях)."""
    if total == 0:
        return 0x22B822   # зелёный
    if total < 50:
        return 0xE8C520   # жёлтый
    return 0xC92C2C       # красный

async def _fetch_member_backoff(guild: discord.Guild, user_id: int, max_retries: int = 5) -> discord.Member | None:
    """Fetch a guild member with exponential backoff on 429."""
    delay = 1.0
    for attempt in range(max_retries):
        try:
            return await guild.fetch_member(user_id)
        except discord.HTTPException as e:
            if e.status == 429 and attempt < max_retries - 1:
                wait = delay + (e.retry_after if hasattr(e, 'retry_after') and e.retry_after else 0)
                await asyncio.sleep(wait)
                delay = min(delay * 2, 32.0)
            else:
                return None
        except Exception:
            return None
    return None


@tasks.loop(hours=24)
async def auto_refresh_reps():
    """Автоматически обновляет кэш репов каждые 24 часа."""
    global rep_cache
    source_guild = bot.get_guild(FORMS_GUILD_ID)
    if not source_guild:
        return
    channel = source_guild.get_channel(REP_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return
    cutoff = discord.utils.utcnow() - datetime.timedelta(days=REP_LOOKBACK_DAYS)
    new_cache: dict[int, list[tuple[str, str]]] = {}
    async for msg in channel.history(limit=None, after=cutoff, oldest_first=True):
        if not msg.mentions:
            continue
        msg_url = f"https://discord.com/channels/{source_guild.id}/{channel.id}/{msg.id}"
        for mentioned_user in msg.mentions:
            try:
                member = source_guild.get_member(mentioned_user.id) or await source_guild.fetch_member(mentioned_user.id)
            except Exception:
                continue
            if not _is_rep_recruit(member):
                continue
            if mentioned_user.id not in new_cache:
                new_cache[mentioned_user.id] = []
            new_cache[mentioned_user.id].append((msg.content, msg_url, msg.created_at))
    rep_cache = new_cache
    log.info(f"[REPS] Auto-refreshed: {len(rep_cache)} recruits")

@auto_refresh_reps.before_loop
async def before_auto_refresh():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user.name}, guilds: {[g.id for g in bot.guilds]}")
    await fetch_maps()
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")
    if not auto_refresh_reps.is_running():
        auto_refresh_reps.start()

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Обработка реакций 👀 на формы для создания тредов со Steam ссылками"""
    try:
        emoji_name = getattr(payload.emoji, "name", None)
        log.info(f"[REACTION] guild={payload.guild_id} channel={payload.channel_id} emoji='{emoji_name}' user={payload.user_id} msg={payload.message_id}")

        # Игнорируем реакции самого бота
        if payload.user_id == (bot.user.id if bot.user else None):
            return

        # Только реакция 👀 (Discord передаёт имя "👀" или "eyes")
        if emoji_name not in ("👀", "eyes"):
            return

        # Проверка канала (FORMS_CHANNEL_ID=0 => любой канал — для отладки)
        if FORMS_CHANNEL_ID != 0 and payload.channel_id != FORMS_CHANNEL_ID:
            return

        # Получаем канал и сообщение
        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning("[FORMS] Channel is not a TextChannel/Thread")
            return

        msg = await channel.fetch_message(payload.message_id)
        await process_form_message(msg)
    except Exception as e:
        log.error(f"[FORMS] on_raw_reaction_add error: {e}")

def _find_any_steam_url(msg: discord.Message) -> str | None:
    """Ищет ЛЮБУЮ steam-подобную ссылку в сообщении (включая кривые)."""
    for text in _iter_message_texts(msg):
        m = STEAM_ANY_URL_RE.search(text)
        if m:
            return m.group(0)
    return None

def _iter_message_texts(msg: discord.Message):
    """Перебирает все текстовые фрагменты сообщения (content + embeds)."""
    content = getattr(msg, "content", "") or ""
    if content:
        yield content
    for emb in getattr(msg, "embeds", []) or []:
        desc = getattr(emb, "description", "") or ""
        if desc:
            yield desc
        for f in getattr(emb, "fields", []) or []:
            val = getattr(f, "value", "") or ""
            if val:
                yield val

def extract_discord_username_from_message(msg: discord.Message) -> str | None:
    """Извлекает значение поля «Discord таг» / «Discord tag» из эмбедов формы."""
    for emb in getattr(msg, "embeds", []) or []:
        for f in getattr(emb, "fields", []) or []:
            name = (getattr(f, "name", "") or "").strip().lower()
            if "discord" in name:
                val = (getattr(f, "value", "") or "").strip()
                if val:
                    return val
    return None

def extract_steam_field_value(msg: discord.Message) -> str | None:
    """Извлекает сырое значение поля «Steam» из эмбедов формы (как бы ни было заполнено)."""
    for emb in getattr(msg, "embeds", []) or []:
        for f in getattr(emb, "fields", []) or []:
            name = (getattr(f, "name", "") or "").strip().lower()
            if "steam" in name:
                val = (getattr(f, "value", "") or "").strip()
                if val:
                    return val
    return None

# Роли для проверки анкет (подтверждают что это новичок, а не старый участник с похожим ником)
FORM_RECRUIT_ROLE_NAMES = {"пополнение", "мёртвая душа", "мертвая душа", "мертвые души", "мёртвые души"}

# Роль рекрута для парсинга репов
REP_RECRUIT_ROLE_NAME = "рекрут"

def _is_recruit(member: discord.Member) -> bool:
    """Есть ли у участника роль для анкет (пополнение / мёртвая душа)."""
    for r in member.roles:
        if r.name.lower() in FORM_RECRUIT_ROLE_NAMES:
            return True
    return False

def _is_rep_recruit(member: discord.Member) -> bool:
    """Есть ли у участника роль рекрута (для парсинга репов)."""
    for r in member.roles:
        if r.name.lower() == REP_RECRUIT_ROLE_NAME.lower():
            return True
    return False

async def search_discord_members(guild: discord.Guild, username: str) -> tuple[discord.Member | None, list[discord.Member]]:
    """
    Ищет на сервере участника по username/display name/nick.
    Приоритет: точное совпадение + роль рекрута > точное без роли > частичные.
    """
    if not guild or not username or len(username) < 2:
        return None, []
    clean = username.strip().lower()
    if not clean:
        return None, []
    try:
        members = await guild.query_members(query=username.strip(), limit=25)
        recruits_found = [m for m in members if _is_recruit(m)]
        log.info(f"[DISCORD] query_members('{username.strip()}'): {len(members)} total, {len(recruits_found)} recruits")

        exact_recruit: discord.Member | None = None
        exact_no_recruit: discord.Member | None = None
        partial: list[discord.Member] = []

        for m in members:
            m_name = (m.name or "").lower()
            m_global = (str(getattr(m, "global_name", "") or "")).lower()
            m_nick = (m.nick or "").lower()
            is_exact = (m_name == clean or m_global == clean or m_nick == clean)

            if is_exact:
                if _is_recruit(m):
                    exact_recruit = m
                else:
                    exact_no_recruit = m
            else:
                partial.append(m)

        # Приоритет: точное + рекрут, иначе точное без рекрута
        exact = exact_recruit or exact_no_recruit
        # Если exact без роли рекрута — помечаем как partial (возможно не тот)
        if exact and not _is_recruit(exact) and (partial or exact_recruit):
            partial.insert(0, exact)
            exact = exact_recruit

        return exact, partial
    except Exception as e:
        log.warning(f"[DISCORD] search_discord_members failed: {e}")
        return None, []

def _message_has_cross_reaction(msg: discord.Message) -> bool:
    """Есть ли на сообщении реакция ❌ — такие не трогаем."""
    for r in getattr(msg, "reactions", []) or []:
        if getattr(r.emoji, "name", None) == "❌":
            return True
    return False

async def process_form_message(msg: discord.Message):
    """
    Обработка анкеты. Если ВСЁ ок — молчим. Если есть проблема — тред + пинг.
    Steam: восстановлен vanity → пишем правильный ID; не восстановлен + Discord валиден → пинг в канал.
    Discord: точное совпадение → ок; 1 похожий → «возможно»; несколько → варианты; 0 → не найден.
    """
    if msg.id in PROCESSED_FORM_MESSAGE_IDS:
        return
    if _message_has_cross_reaction(msg):
        return
    PROCESSED_FORM_MESSAGE_IDS.add(msg.id)

    thread_msgs: list[str] = []
    steam_url = extract_steam_url_from_message(msg)
    is_griefer = False
    normalized_steam = None
    steam_ok = False
    steam_unrecoverable = False

    # --- Steam ---
    if steam_url:
        normalized_steam, valid, was_recovered = await normalize_steam_url(steam_url)
        steam_id64 = _steam_id64_from_url(normalized_steam)

        if steam_id64 and steam_id64 in load_griefer_ids_from_file():
            is_griefer = True
        elif valid and steam_id64:
            steam_ok = True
            if was_recovered:
                thread_msgs.append(
                    f"🔧 **Steam:** ссылка была в формате `/id/`, восстановлен SteamID64:\n"
                    f"{normalized_steam}"
                )
        else:
            steam_unrecoverable = True
            thread_msgs.append(
                f"⚠️ **Steam:** профиль не найден или ссылка некорректна.\n"
                f"Указано: {steam_url}"
            )
    else:
        bad_url = _find_any_steam_url(msg)
        raw_steam = extract_steam_field_value(msg)
        if bad_url:
            steam_unrecoverable = True
            thread_msgs.append(
                f"❌ **Steam:** указана неправильная ссылка!\n"
                f"Указано: {bad_url}\n"
                f"Нужна ссылка на **профиль**, например:\n"
                f"• `https://steamcommunity.com/profiles/76561198XXXXXXXXX/`\n"
                f"• `https://steamcommunity.com/id/ваш_ник/`"
            )
        elif raw_steam:
            steam_unrecoverable = True
            thread_msgs.append(
                f"❌ **Steam:** в поле указано `{raw_steam}` — это не ссылка на профиль Steam.\n"
                f"Нужна ссылка вида:\n"
                f"• `https://steamcommunity.com/profiles/76561198XXXXXXXXX/`\n"
                f"• `https://steamcommunity.com/id/ваш_ник/`"
            )

    # --- Discord ---
    discord_username = extract_discord_username_from_message(msg)
    discord_ok = False
    discord_member: discord.Member | None = None
    discord_partial: list[discord.Member] = []
    if discord_username and msg.guild:
        discord_member, discord_partial = await search_discord_members(msg.guild, discord_username)
        if discord_member:
            discord_ok = True
        else:
            recruits = [p for p in discord_partial if _is_recruit(p)]
            if len(recruits) == 1:
                thread_msgs.append(
                    f"ℹ️ **Discord:** `{discord_username}` — точного совпадения нет, "
                    f"но найден рекрут: <@{recruits[0].id}> (`{recruits[0].name}`)"
                )
            elif len(recruits) > 1:
                mentions = ", ".join(f"<@{p.id}> (`{p.name}`)" for p in recruits[:5])
                thread_msgs.append(
                    f"⚠️ **Discord:** `{discord_username}` — найдено несколько рекрутов: {mentions}"
                )

    # --- Грифер (наивысший приоритет) ---
    if is_griefer:
        thread = getattr(msg, "thread", None)
        if thread is None:
            thread = await msg.create_thread(name=FORMS_THREAD_NAME, auto_archive_duration=1440)
        await thread.send(
            "🚫🚫🚫 **ОБНАРУЖЕН В СПИСКЕ ГРИФЕРОВ, НЕ ПРИНИМАТЬ!** 🚫🚫🚫\n"
            "⚠️ Steam из анкеты совпадает с записью в списке гриферов."
        )
        try:
            await msg.add_reaction("❌")
        except Exception:
            pass
        log.warning(f"[FORMS] GRIEFER match message {msg.id}: {normalized_steam}")
        return

    # --- Всё ок — молчим ---
    if not thread_msgs:
        log.info(f"[FORMS] OK message {msg.id} — no issues, staying silent")
        return

    # --- Есть проблемы — создаём тред ---
    thread = getattr(msg, "thread", None)
    if thread is None:
        thread = await msg.create_thread(name=FORMS_THREAD_NAME, auto_archive_duration=1440)

    body = "\n\n".join(thread_msgs)
    await thread.send(body)

    # --- Пинг в канал если Steam не восстановить, но Discord валиден ---
    if steam_unrecoverable and discord_ok and discord_member:
        ping_channel = bot.get_channel(PING_CHANNEL_ID)
        if ping_channel and isinstance(ping_channel, discord.TextChannel):
            try:
                await ping_channel.send(
                    f"*Автоматическое сообщение от бота.*\n\n"
                    f"<@{discord_member.id}>, в вашей анкете указана **неправильная ссылка на Steam**.\n"
                    f"Пожалуйста, укажите правильную ссылку на свой профиль Steam.\n\n"
                    f"Где взять ссылку:\n"
                    f"1. Откройте Steam → ваш профиль\n"
                    f"2. Нажмите правой кнопкой → «Скопировать URL страницы»\n"
                    f"3. Ссылка должна выглядеть так: `https://steamcommunity.com/profiles/76561198XXXXXXXXX/`"
                )
                log.info(f"[FORMS] Pinged {discord_member.name} in ping channel about bad Steam")
            except Exception as e:
                log.warning(f"[FORMS] Failed to ping in channel: {e}")

    log.info(f"[FORMS] Issues in message {msg.id}: {len(thread_msgs)} item(s)")

@bot.event
async def on_message(message: discord.Message):
    """Автоматическая обработка новых сообщений в канале форм."""
    # Игнорируем самого бота
    if message.author == bot.user:
        await bot.process_commands(message)
        return

    # Проверяем что это канал форм
    if message.channel.id == FORMS_CHANNEL_ID and message.guild and message.guild.id == FORMS_GUILD_ID:
        log.info(f"[FORMS-MSG] New message in forms channel: {message.id} from {message.author}")
        try:
            await process_form_message(message)
        except Exception as e:
            log.error(f"[FORMS-MSG] Error processing message {message.id}: {e}")

    # ВАЖНО: без этого !sync и другие текстовые команды не будут работать
    await bot.process_commands(message)

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

@bot.command(name="refresh_griefers", help="Обновить список гриферов из канала (для админа)")
async def refresh_griefers_cmd(ctx):
    if ctx.author.name.lower() != ADMIN_USERNAME.lower():
        await ctx.send("Команда только для администратора")
        return
    status = await ctx.send("🔄 Загрузка списка гриферов из канала…")
    try:
        n = await refresh_griefer_list(status_msg=status)
        await status.edit(content=f"✅ Список гриферов обновлён: **{n}** Steam ID.")
    except Exception as e:
        await status.edit(content=f"Ошибка: {e}")

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

    QUEUES_DISCLAIMER = "⚠️ Скрины этой команды никуда не сливать — даже в чат рекрутов. Чтобы команда жила дольше и разрабы её не закрыли."

    if not active_queues:
        emb = discord.Embed(
            title="🚀 Очереди в реальном времени",
            description="✅ **Очередей нет!** На фронте тихо... пока что.",
            color=embed_color_by_queue(0),
        )
        emb.set_footer(text=QUEUES_DISCLAIMER)
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
    emb.set_footer(text=QUEUES_DISCLAIMER)
    await interaction.followup.send(embed=emb, ephemeral=True)

@bot.tree.command(name="polak", description="[В разработке] 🚧")
async def polak(interaction: discord.Interaction):
    await interaction.response.send_message("🚧 Команда в разработке", ephemeral=True)

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

@bot.tree.command(name="parse_reps", description="Спарсить репы рекрутов из канала за последний год (только ветеран/админ)")
async def parse_reps(interaction: discord.Interaction):
    if not await check_access_veteran(interaction):
        await interaction.response.send_message("❌ Нет доступа. Только ветеран или администратор.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # Канал с репами всегда на FORMS_GUILD_ID — берём оттуда независимо от сервера команды
    source_guild = bot.get_guild(FORMS_GUILD_ID)
    if not source_guild:
        await interaction.followup.send("❌ Бот не найден на сервере с каналом репов.", ephemeral=True)
        return

    try:
        channel = source_guild.get_channel(REP_CHANNEL_ID) or await source_guild.fetch_channel(REP_CHANNEL_ID)
    except Exception as e:
        await interaction.followup.send(f"❌ Не удалось получить канал репов: {e}", ephemeral=True)
        return

    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("❌ Канал репов не является текстовым.", ephemeral=True)
        return

    cutoff = discord.utils.utcnow() - datetime.timedelta(days=REP_LOOKBACK_DAYS)

    global rep_cache
    rep_cache.clear()

    msg_count = 0
    mention_count = 0
    non_recruit_ids: set[int] = set()

    status_msg = await interaction.followup.send("🔄 Загружаю участников...", ephemeral=True)
    if not source_guild.chunked:
        await source_guild.chunk()
    await status_msg.edit(content="🔄 Паршу репы...")

    async for msg in channel.history(limit=None, after=cutoff, oldest_first=True):
        msg_count += 1

        if not msg.mentions:
            continue

        msg_url = f"https://discord.com/channels/{source_guild.id}/{channel.id}/{msg.id}"

        for mentioned_user in msg.mentions:
            uid = mentioned_user.id
            # Already confirmed recruit — just add the mention
            if uid in rep_cache:
                rep_cache[uid].append((msg.content, msg_url, msg.created_at))
                mention_count += 1
                continue
            # Already confirmed non-recruit — skip
            if uid in non_recruit_ids:
                continue
            member = source_guild.get_member(uid)
            if member is None:
                # After chunk(), missing from cache = left the server → can't be a recruit
                non_recruit_ids.add(uid)
                continue

            if not _is_rep_recruit(member):
                non_recruit_ids.add(uid)
                continue

            rep_cache[uid] = [(msg.content, msg_url, msg.created_at)]
            mention_count += 1

        if msg_count % 100 == 0:
            try:
                await status_msg.edit(content=f"🔄 Обработано {msg_count} сообщений, найдено {len(rep_cache)} рекрутов...")
            except Exception:
                pass
            await asyncio.sleep(0.5)

    log.info(f"[REPS] Parsed {msg_count} messages, {len(rep_cache)} recruits, {mention_count} mentions")
    await status_msg.edit(
        content=f"✅ Готово! Сообщений: **{msg_count}**, рекрутов с репами: **{len(rep_cache)}**, упоминаний: **{mention_count}**.\n"
                f"Теперь можно запустить `/post_reps` для публикации."
    )


@bot.tree.command(name="post_reps", description="Опубликовать репы рекрутов в текущем канале (только ветеран/админ)")
@app_commands.describe(
    min_reps="Минимум рекомендаций (0 = все)",
    last_rep_days="Последний реп не позднее N дней назад (0 = без ограничения)",
    top="Показать только топ N рекрутов по количеству репов (0 = все)",
)
async def post_reps(
    interaction: discord.Interaction,
    min_reps: int = 0,
    last_rep_days: int = 0,
    top: int = 0,
):
    if not await check_access_veteran(interaction):
        await interaction.response.send_message("❌ Нет доступа. Только ветеран или администратор.", ephemeral=True)
        return

    if not rep_cache:
        await interaction.response.send_message(
            "❌ Кэш пустой. Сначала запустите `/parse_reps`.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("❌ Команда только в текстовом канале сервера.", ephemeral=True)
        return

    # Ники берём с гилды где рекруты
    source_guild = bot.get_guild(FORMS_GUILD_ID)
    if source_guild and not source_guild.chunked:
        await source_guild.chunk()

    posted = 0

    now_utc = discord.utils.utcnow()

    # Фильтрация и сортировка
    candidates = list(rep_cache.items())

    if min_reps > 0:
        candidates = [(uid, m) for uid, m in candidates if len(m) >= min_reps]

    if last_rep_days > 0:
        cutoff_ts = now_utc - datetime.timedelta(days=last_rep_days)
        candidates = [(uid, m) for uid, m in candidates if max(ts for _, _, ts in m) >= cutoff_ts]

    # Сортировка по количеству репов (убыв.)
    candidates.sort(key=lambda x: len(x[1]), reverse=True)

    if top > 0:
        candidates = candidates[:top]

    for user_id, mentions in candidates:
        if not source_guild:
            continue
        member = source_guild.get_member(user_id) or await _fetch_member_backoff(source_guild, user_id)
        if member is None:
            continue

        nick = member.nick or member.display_name

        # Дни на сервере
        if member.joined_at:
            days = (now_utc - member.joined_at).days
            if days > 120:
                days_icon = "👴"
            elif days > 30:
                days_icon = "🟢"
            else:
                days_icon = "⏳"
            join_str = f"🕐 Зашел на сервер {days} дн. назад {days_icon}"
        else:
            join_str = "🕐 Дата вступления неизвестна"

        # Дата последнего репа
        if mentions:
            last_ts = max(ts for _, _, ts in mentions)
            last_rep_str = f"📅 Последняя рекомендация: {last_ts.strftime('%d.%m.%Y')}"
        else:
            last_rep_str = ""

        # Проверяем: всё ещё рекрут или уже повышен
        still_recruit = _is_rep_recruit(member)
        status_str = "" if still_recruit else " | ⬆️ повышен"

        lines = [
            f"<@{user_id}> **{nick}**",
            f"{join_str} | Репов: **{len(mentions)}**{status_str}",
        ]
        if last_rep_str:
            lines.append(last_rep_str)

        msg = await channel.send("\n".join(lines))

        try:
            thread = await msg.create_thread(
                name=f"Репы: {nick}"[:100],
                auto_archive_duration=1440
            )
            for i, (_, url, _) in enumerate(mentions):
                await thread.send(f"**[→ Перейти к сообщению]({url})**")
                if i > 0 and i % 5 == 0:
                    await asyncio.sleep(1)
        except Exception as e:
            log.warning(f"[REPS] Failed to create thread for {nick}: {e}")

        posted += 1
        await asyncio.sleep(0.5)

    log.info(f"[REPS] Posted {posted} recruits")
    await interaction.followup.send(
        f"✅ Опубликовано **{posted}** рекрутов.",
        ephemeral=True
    )


if __name__ == "__main__":
    if not TOKEN:
        print("❌ Error: DISCORD_TOKEN not found in .env file")
    else:
        bot.run(TOKEN)