"""
Модуль заказов самолётов для 404th.
Команды:
  /order   — оформить заказ (интерактивный wizard)
  /queue   — список невыполненных заказов
  /planes  — группа команд администратора
"""

import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import logging
import datetime

log = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "planes_config.json")

DEFAULT_CONFIG = {
    "plane_types": [
        "Истребитель",
        "Дайв Бомбер",
        "Паратрупер",
        "Бобёр",
    ],
    "payment_options": [
        {"label": "2 контейнера серы на уголь", "active": True},
        {"label": "3 контейнера компов на уголь", "active": True},
        {"label": "Перевезти поезд А→Б", "active": True},
        {"label": "40к сальваги", "active": False},
        {"label": "70 рарок", "active": True},
        {"label": "По акции (50k Е-баллов)", "active": True},
    ],
    "admin_roles": ["Штабной"],
    "spreadsheet_id": os.getenv("PLANES_SPREADSHEET_ID", ""),
    "worksheet_name": "Заказы",
}

# ─── Config helpers ──────────────────────────────────────────────────────────


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ─── Google Sheets helpers ────────────────────────────────────────────────────

def _get_sheets_client():
    """Return a gspread client or None if creds are missing."""
    try:
        import gspread
    except ImportError:
        log.error("gspread not installed — run: pip install gspread google-auth")
        return None

    creds_file = os.getenv("GOOGLE_SHEETS_CREDS", "service_account.json")
    if not os.path.exists(creds_file):
        log.warning(f"Google Sheets creds not found: {creds_file}")
        return None

    try:
        return gspread.service_account(filename=creds_file)
    except Exception as e:
        log.error(f"Failed to create Sheets client: {e}")
        return None


def _get_worksheet():
    """Return the orders worksheet, creating header row if new."""
    config = load_config()
    spreadsheet_id = config.get("spreadsheet_id") or os.getenv("PLANES_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        return None

    client = _get_sheets_client()
    if not client:
        return None

    try:
        import gspread
        sh = client.open_by_key(spreadsheet_id)
        worksheet_name = config.get("worksheet_name", "Заказы")
        try:
            ws = sh.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=10)
            ws.append_row(["#", "Дата", "Discord", "Самолёт", "Оплата", "Склад", "Когда оплата", "Статус"])
        return ws
    except Exception as e:
        log.error(f"Failed to get worksheet: {e}")
        return None


def append_order(order: dict) -> bool:
    """Append one order row. Returns True on success."""
    ws = _get_worksheet()
    if not ws:
        return False
    try:
        all_rows = ws.get_all_values()
        order_num = max(len(all_rows), 1)  # header = row 1
        ws.append_row([
            order_num,
            order["timestamp"],
            order["discord_user"],
            order["plane_type"],
            order["payment"],
            order["warehouse"],
            order["when_payment"],
            "",  # Status — admin fills later
        ])
        return True
    except Exception as e:
        log.error(f"append_order failed: {e}")
        return False


def get_pending_orders() -> list[dict]:
    """Return rows that don't have ✅ in the Status column."""
    ws = _get_worksheet()
    if not ws:
        return []
    try:
        rows = ws.get_all_values()
        if len(rows) < 2:
            return []
        pending = []
        for row in rows[1:]:
            row += [""] * max(0, 8 - len(row))
            status = row[7]
            if "✅" not in status:
                pending.append({
                    "num": row[0],
                    "timestamp": row[1],
                    "discord_user": row[2],
                    "plane_type": row[3],
                    "payment": row[4],
                    "warehouse": row[5],
                    "when_payment": row[6],
                    "status": status,
                })
        return pending
    except Exception as e:
        log.error(f"get_pending_orders failed: {e}")
        return []


# ─── Discord UI ───────────────────────────────────────────────────────────────


class OrderModal(discord.ui.Modal, title="Детали заказа"):
    warehouse = discord.ui.TextInput(
        label="Название склада на аэродроме",
        placeholder="Например: The best sklad",
        min_length=1,
        max_length=100,
    )
    when_payment = discord.ui.TextInput(
        label="Когда будет оплата?",
        placeholder="Например: сегодня вечером, через 2 часа",
        min_length=1,
        max_length=200,
    )

    def __init__(self, plane_type: str, payment: str):
        super().__init__()
        self.plane_type = plane_type
        self.payment = payment

    async def on_submit(self, interaction: discord.Interaction):
        user_str = f"{interaction.user.display_name} ({interaction.user.name})"
        order = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "discord_user": user_str,
            "plane_type": self.plane_type,
            "payment": self.payment,
            "warehouse": self.warehouse.value,
            "when_payment": self.when_payment.value,
        }

        saved = append_order(order)

        embed = discord.Embed(title="✈️ Заказ принят!", color=discord.Color.green())
        embed.add_field(name="Самолёт", value=self.plane_type, inline=True)
        embed.add_field(name="Оплата", value=self.payment, inline=True)
        embed.add_field(name="Склад", value=self.warehouse.value, inline=False)
        embed.add_field(name="Когда оплата", value=self.when_payment.value, inline=False)

        if saved:
            embed.set_footer(text="Добавлен в таблицу. Ждите 👍 на вашей заявке.")
        else:
            embed.color = discord.Color.yellow()
            embed.set_footer(text="⚠️ Не удалось сохранить в таблицу — обратитесь к администратору.")

        await interaction.response.send_message(embed=embed, ephemeral=True)


class ConfirmView(discord.ui.View):
    def __init__(self, plane_type: str, payment: str):
        super().__init__(timeout=300)
        self.plane_type = plane_type
        self.payment = payment

    @discord.ui.button(label="✅ Оформить заказ", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(OrderModal(self.plane_type, self.payment))

    @discord.ui.button(label="◀ Назад", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        config = load_config()
        embed = discord.Embed(
            title="✈️ Заказ самолёта",
            description="Выберите тип самолёта.",
            color=discord.Color.blue(),
        )
        view = OrderWizardView(config["plane_types"], config["payment_options"])
        await interaction.response.edit_message(embed=embed, view=view)


class PaymentSelect(discord.ui.Select):
    def __init__(self, plane_type: str, payment_options: list):
        self.plane_type = plane_type
        active = [p for p in payment_options if p.get("active", True)]
        options = [discord.SelectOption(label=p["label"][:100]) for p in active[:25]]
        super().__init__(placeholder="Выберите способ оплаты…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        payment = self.values[0]
        embed = discord.Embed(title="✈️ Оформление заказа", color=discord.Color.blue())
        embed.add_field(name="Самолёт", value=self.plane_type, inline=True)
        embed.add_field(name="Оплата", value=payment, inline=True)
        if self.plane_type in ("Дайв Бомбер", "Паратрупер"):
            embed.add_field(name="⚠️ Цена x2", value="Дайв Бомбер и Паратрупер стоят вдвое дороже.", inline=False)
        embed.add_field(name="Следующий шаг", value="Нажми «Оформить заказ» и введи название склада.", inline=False)
        await interaction.response.edit_message(embed=embed, view=ConfirmView(self.plane_type, payment))


class PaymentSelectView(discord.ui.View):
    def __init__(self, plane_type: str, payment_options: list):
        super().__init__(timeout=300)
        self.add_item(PaymentSelect(plane_type, payment_options))

    @discord.ui.button(label="◀ Назад", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        config = load_config()
        embed = discord.Embed(title="✈️ Заказ самолёта", description="Выберите тип самолёта.", color=discord.Color.blue())
        view = OrderWizardView(config["plane_types"], config["payment_options"])
        await interaction.response.edit_message(embed=embed, view=view)


class PlaneTypeSelect(discord.ui.Select):
    def __init__(self, plane_types: list, payment_options: list):
        self.payment_options = payment_options
        options = [discord.SelectOption(label=p[:100]) for p in plane_types[:25]]
        super().__init__(placeholder="Выберите тип самолёта…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        plane_type = self.values[0]
        active = [p for p in self.payment_options if p.get("active", True)]
        if not active:
            await interaction.response.edit_message(
                content="❌ Нет доступных способов оплаты. Обратитесь к администратору.",
                embed=None,
                view=None,
            )
            return
        embed = discord.Embed(title="✈️ Оформление заказа", color=discord.Color.blue())
        embed.add_field(name="Самолёт", value=plane_type, inline=True)
        embed.add_field(name="Шаг 2/3", value="Выберите способ оплаты.", inline=False)
        await interaction.response.edit_message(embed=embed, view=PaymentSelectView(plane_type, self.payment_options))


class OrderWizardView(discord.ui.View):
    def __init__(self, plane_types: list, payment_options: list):
        super().__init__(timeout=300)
        self.add_item(PlaneTypeSelect(plane_types, payment_options))


# ─── Admin group ──────────────────────────────────────────────────────────────


class PlanesAdminGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="planes", description="Управление каталогом самолётов (только для администраторов)")

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        config = load_config()
        admin_roles = set(r.lower() for r in config.get("admin_roles", []))
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(r.name.lower() in admin_roles for r in interaction.user.roles)

    # ── Plane types ──

    @app_commands.command(name="add_plane", description="Добавить тип самолёта в каталог")
    @app_commands.describe(name="Название самолёта")
    async def add_plane(self, interaction: discord.Interaction, name: str):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        config = load_config()
        if name in config["plane_types"]:
            await interaction.response.send_message(f"❌ `{name}` уже есть.", ephemeral=True)
            return
        config["plane_types"].append(name)
        save_config(config)
        await interaction.response.send_message(f"✅ Добавлен самолёт: **{name}**", ephemeral=True)

    @app_commands.command(name="remove_plane", description="Удалить тип самолёта из каталога")
    @app_commands.describe(name="Название самолёта")
    async def remove_plane(self, interaction: discord.Interaction, name: str):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        config = load_config()
        if name not in config["plane_types"]:
            await interaction.response.send_message(f"❌ `{name}` не найден.", ephemeral=True)
            return
        config["plane_types"].remove(name)
        save_config(config)
        await interaction.response.send_message(f"✅ Удалён самолёт: **{name}**", ephemeral=True)

    # ── Payment options ──

    @app_commands.command(name="add_payment", description="Добавить способ оплаты")
    @app_commands.describe(label="Текст способа оплаты")
    async def add_payment(self, interaction: discord.Interaction, label: str):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        config = load_config()
        if any(p["label"] == label for p in config["payment_options"]):
            await interaction.response.send_message(f"❌ Уже есть: `{label}`", ephemeral=True)
            return
        config["payment_options"].append({"label": label, "active": True})
        save_config(config)
        await interaction.response.send_message(f"✅ Добавлен способ оплаты: **{label}**", ephemeral=True)

    @app_commands.command(name="remove_payment", description="Удалить способ оплаты")
    @app_commands.describe(label="Текст способа оплаты")
    async def remove_payment(self, interaction: discord.Interaction, label: str):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        config = load_config()
        before = len(config["payment_options"])
        config["payment_options"] = [p for p in config["payment_options"] if p["label"] != label]
        if len(config["payment_options"]) == before:
            await interaction.response.send_message(f"❌ Не найдено: `{label}`", ephemeral=True)
            return
        save_config(config)
        await interaction.response.send_message(f"✅ Удалён способ оплаты: **{label}**", ephemeral=True)

    @app_commands.command(name="toggle_payment", description="Вкл/выкл способ оплаты (зачеркнуть из списка)")
    @app_commands.describe(label="Текст способа оплаты")
    async def toggle_payment(self, interaction: discord.Interaction, label: str):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        config = load_config()
        for p in config["payment_options"]:
            if p["label"] == label:
                p["active"] = not p.get("active", True)
                save_config(config)
                status = "✅ включён" if p["active"] else "❌ отключён (недоступен для заказа)"
                await interaction.response.send_message(
                    f"Способ оплаты **{label}** теперь {status}", ephemeral=True
                )
                return
        await interaction.response.send_message(f"❌ Не найдено: `{label}`", ephemeral=True)

    @app_commands.command(name="list", description="Показать текущий каталог самолётов и способов оплаты")
    async def show_list(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        config = load_config()
        embed = discord.Embed(title="📋 Каталог самолётов", color=discord.Color.blue())

        planes_str = "\n".join(f"• {p}" for p in config["plane_types"]) or "—"
        embed.add_field(name="Самолёты", value=planes_str, inline=False)

        payments = []
        for p in config["payment_options"]:
            icon = "✅" if p.get("active", True) else "❌"
            payments.append(f"{icon} {p['label']}")
        embed.add_field(name="Способы оплаты", value="\n".join(payments) or "—", inline=False)

        sid = config.get("spreadsheet_id") or "не задан"
        embed.set_footer(text=f"Spreadsheet ID: {sid}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set_spreadsheet", description="Установить ID Google-таблицы для заказов")
    @app_commands.describe(spreadsheet_id="ID таблицы из URL docs.google.com/spreadsheets/d/...")
    async def set_spreadsheet(self, interaction: discord.Interaction, spreadsheet_id: str):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        config = load_config()
        config["spreadsheet_id"] = spreadsheet_id
        save_config(config)
        await interaction.response.send_message(
            f"✅ Spreadsheet ID сохранён: `{spreadsheet_id}`", ephemeral=True
        )

    @app_commands.command(name="add_admin_role", description="Добавить роль с правами администратора бота")
    @app_commands.describe(role="Роль Discord")
    async def add_admin_role(self, interaction: discord.Interaction, role: discord.Role):
        if not self._is_admin(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        config = load_config()
        if role.name not in config["admin_roles"]:
            config["admin_roles"].append(role.name)
            save_config(config)
        await interaction.response.send_message(f"✅ Роль **{role.name}** добавлена как админская.", ephemeral=True)


# ─── Cog ─────────────────────────────────────────────────────────────────────


class PlanesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(PlanesAdminGroup())

    @app_commands.command(name="order", description="Оформить заказ на самолёт")
    async def order(self, interaction: discord.Interaction):
        config = load_config()
        plane_types = config.get("plane_types", [])
        payment_options = config.get("payment_options", [])

        if not plane_types:
            await interaction.response.send_message("❌ Нет доступных самолётов.", ephemeral=True)
            return

        active_payments = [p for p in payment_options if p.get("active", True)]
        if not active_payments:
            await interaction.response.send_message("❌ Нет доступных способов оплаты.", ephemeral=True)
            return

        embed = discord.Embed(
            title="✈️ Заказ самолёта",
            description=(
                "Шаг 1/3 — Выберите тип самолёта.\n\n"
                "⚠️ **Дайв Бомбер и Паратрупер** — цена x2 от базовой.\n"
                "Склад должен быть **видимым** (нажать кнопку на аэродроме)."
            ),
            color=discord.Color.blue(),
        )
        view = OrderWizardView(plane_types, payment_options)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="queue", description="Показать список невыполненных заказов")
    async def queue(self, interaction: discord.Interaction):
        await interaction.response.defer()
        orders = get_pending_orders()

        if not orders:
            await interaction.followup.send("✅ Очередь пуста!")
            return

        embed = discord.Embed(
            title=f"📋 Очередь заказов — {len(orders)} шт.",
            color=discord.Color.orange(),
        )
        for o in orders[:10]:
            status = o["status"] or "⏳ ожидает"
            embed.add_field(
                name=f"#{o['num']} — {o['plane_type']}",
                value=(
                    f"👤 {o['discord_user']}\n"
                    f"💰 {o['payment']}\n"
                    f"🏪 {o['warehouse']}\n"
                    f"📅 {o['when_payment']}\n"
                    f"Статус: {status}"
                ),
                inline=False,
            )
        if len(orders) > 10:
            embed.set_footer(text=f"Показано 10 из {len(orders)}. Полный список в таблице.")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(PlanesCog(bot))
    log.info("[PLANES] Cog loaded")
