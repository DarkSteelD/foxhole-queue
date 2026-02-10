import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import asyncio
import os
from dotenv import load_dotenv
import time

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
PROXY_URL = os.getenv('HTTP_PROXY')

# API Endpoints
WAR_API_BASE = "https://war-service-live.foxholeservices.com/api"
SHARD_STATUS_URL = "https://war-service-live.foxholeservices.com/external/shardStatus/servers"

# Configuration
ALLOWED_GUILD_IDS = [
    355748261958647809,  # Server 1
    1470823990644703363  # Server 2
]
REQUIRED_ROLE_NAME = "404th" # Change this to the role name you want!
ADMIN_USERNAME = "darksteeldragon"

# Setup Bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True # Need this to check roles reliably
bot = commands.Bot(command_prefix='!', intents=intents, proxy=PROXY_URL)

def check_access(interaction: discord.Interaction) -> bool:
    """Checks if user is allowed to use the bot"""
    # 1. Check Server
    if interaction.guild_id not in ALLOWED_GUILD_IDS:
        return False
        
    # 2. Check Admin (Bypass role check)
    if interaction.user.name.lower() == ADMIN_USERNAME.lower():
        return True
        
    # 3. Check Role by Name
    if isinstance(interaction.user, discord.Member):
        for role in interaction.user.roles:
            if role.name.lower() == REQUIRED_ROLE_NAME.lower():
                return True
                
    return False

# Global Cache
map_names = [] # List of official map names
queue_cache = None
QUEUE_CACHE_TTL = 180  # 3 минуты

async def fetch_maps():
    global map_names
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WAR_API_BASE}/worldconquest/maps", proxy=PROXY_URL) as response:
                if response.status == 200:
                    map_names = await response.json()
                    print(f"Loaded {len(map_names)} maps.")
                else:
                    print(f"Failed to load maps: {response.status}")
    except Exception as e:
        print(f"Error fetching maps: {e}")

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
                    print(f"Failed to fetch queue data: {response.status}")
    except Exception as e:
        print(f"Error fetching queues: {e}")
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
    print(f'Logged in as {bot.user.name}')
    await fetch_maps()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

@bot.command(name="sync", help="Синхронизировать команды (для админа)")
async def sync(ctx):
    msg = await ctx.send("🔄 Синхронизация команд...")
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await msg.edit(content=f"✅ Успешно синхронизировано {len(synced)} команд для этого сервера! Теперь они должны появиться мгновенно.")
    except Exception as e:
        await msg.edit(content=f"❌ Ошибка синхронизации: {e}")

@bot.tree.command(name="queues", description="Показать очереди на всех картах (видно только вам)")
async def queues(interaction: discord.Interaction):
    if not check_access(interaction):
        await interaction.response.send_message(f"Бот сейчас не работает, **напишите @darksteeldragon**", ephemeral=True)
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

    # Колония 🟢 / Вардены 🔵; жирный >10, 🔴 >30; выравнивание по ширине
    max_name_len = max(len(q["name"]) for q in active_queues)
    body = ""
    for q in active_queues:
        name_padded = q["name"].ljust(max_name_len)
        body += f"**{name_padded}**  {fmt_cell(q['col'], True)}  {fmt_cell(q['ward'], False)}\n"

    emb = discord.Embed(
        title="🚀 Очереди в реальном времени",
        description=body,
        color=embed_color_by_queue(total_in_queues),
    )
    await interaction.followup.send(embed=emb, ephemeral=True)

@bot.tree.command(name="map", description="Показать очередь на конкретной карте")
@app_commands.describe(name="Название карты (на английском)")
async def map_stats(interaction: discord.Interaction, name: str):
    if not check_access(interaction):
        await interaction.response.send_message(f"Бот сейчас не работает, **напишите @darksteeldragon**", ephemeral=True)
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
        print("Error: DISCORD_TOKEN not found.")
    else:
        bot.run(TOKEN)
