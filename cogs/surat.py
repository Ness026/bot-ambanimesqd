import asyncio
import io
import os
import random
import re
import sqlite3
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

# Konfigurasi dari .env
LETTER_LOG_CHANNEL_ID = int(os.getenv("LETTER_LOG_CHANNEL_ID", "0").strip() or "0")
POSTBOX_CHANNEL_ID = int(os.getenv("POSTBOX_CHANNEL_ID", "0").strip() or "0")
SURAT_CHANNEL_ID = int(os.getenv("SURAT_CHANNEL_ID", "0").strip() or "0")

COOLDOWN_SECONDS = 900  # 15 menit (900 detik)
DB_PATH = "database.db"
TEMPLATE_PATH = "assets/template_surat.png"

# Antrean agar di 1 vCPU render tidak bertabrakan jika ada yg kirim bersamaan
render_lock = asyncio.Semaphore(1)


# ==========================================
# 1. DATABASE (COOLDOWN & KOTAK POS)
# ==========================================
def init_letter_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS letter_cooldowns (
            sender_id INTEGER PRIMARY KEY,
            last_sent REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_letters (
            message_id INTEGER PRIMARY KEY,
            sender_id INTEGER,
            receiver_id INTEGER,
            receiver_name TEXT,
            salutation TEXT,
            content TEXT,
            alias TEXT,
            date_str TEXT
        )
    """)
    conn.commit()
    conn.close()


def check_cooldown(sender_id: int) -> int:
    """Mengembalikan sisa detik cooldown (0 jika boleh kirim)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_sent FROM letter_cooldowns WHERE sender_id = ?", (sender_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return 0
    elapsed = time.time() - row[0]
    remaining = int(COOLDOWN_SECONDS - elapsed)
    return remaining if remaining > 0 else 0


def set_cooldown(sender_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO letter_cooldowns (sender_id, last_sent)
        VALUES (?, ?)
        ON CONFLICT(sender_id) DO UPDATE SET last_sent = excluded.last_sent
    """, (sender_id, time.time()))
    conn.commit()
    conn.close()


def save_pending_letter(message_id, sender_id, receiver_id, receiver_name, salutation, content, alias, date_str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO pending_letters
        (message_id, sender_id, receiver_id, receiver_name, salutation, content, alias, date_str)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (message_id, sender_id, receiver_id, receiver_name, salutation, content, alias, date_str))
    conn.commit()
    conn.close()


def get_pending_letter(message_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT sender_id, receiver_id, receiver_name, salutation, content, alias, date_str
        FROM pending_letters WHERE message_id = ?
    """, (message_id,))
    row = c.fetchone()
    conn.close()
    return row


# ==========================================
# 2. FUNGSI RENDER GAMBAR SURAT (PILLOW)
# ==========================================
def clean_text(text: str) -> str:
    """Hapus emoji agar font .ttf tidak mencetak kotak kosong."""
    cleaned = re.sub(r"[^\x20-\x7E\u00C0-\u024F\n]+", "", text)
    return cleaned.strip()


def load_font_safe(path: str, size: int, fallback: str = "assets/Poppins-Regular.ttf"):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(fallback, size)


def wrap_text_by_pixel(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int):
    """Membungkus baris berdasarkan lebar piksel asli huruf cursive."""
    lines = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            if draw.textlength(test, font=font) <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                if draw.textlength(word, font=font) > max_width:
                    chunk = ""
                    for ch in word:
                        if draw.textlength(chunk + ch, font=font) <= max_width:
                            chunk += ch
                        else:
                            lines.append(chunk)
                            chunk = ch
                    current = chunk
                else:
                    current = word
        if current:
            lines.append(current)
    return lines


def render_letter_bytes(receiver_name: str, salutation: str, content: str, alias: str, date_str: str) -> bytes:
    """Merender surat perkamen ke format PNG bytes secara hemat RAM."""
    with Image.open(TEMPLATE_PATH) as img:
        base = img.convert("RGBA").resize((1000, 1250), Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(base)
    ink_dark = (46, 30, 20, 255)    # Cokelat tinta sepia gelap
    ink_soft = (92, 66, 48, 255)    # Cokelat tinta lembut

    clean_receiver = clean_text(receiver_name)[:28] or "Seseorang"
    clean_salutation = clean_text(salutation)[:55]
    clean_body = clean_text(content) or "..."
    clean_alias = clean_text(alias)[:30] or "Pengagum Rahasia"

    # --- A. Header Sapaan ---
    font_header = load_font_safe("assets/GreatVibes-Regular.ttf", 46)
    header_text = clean_salutation if clean_salutation else f"Kepada Yang Terhormat, {clean_receiver},"
    if draw.textlength(header_text, font=font_header) > 740:
        font_header = load_font_safe("assets/GreatVibes-Regular.ttf", 36)
    draw.text((130, 295), header_text, font=font_header, fill=ink_dark)

    # --- B. Isi Surat (Auto-fit ukuran font 36px -> 24px) ---
    max_width = 740
    max_height = 540
    chosen_font = None
    wrapped_lines = []
    line_height = 46

    for font_size in range(36, 22, -2):
        f = load_font_safe("assets/DancingScript-Medium.ttf", font_size)
        lines = wrap_text_by_pixel(draw, clean_body, f, max_width)
        lh = int(font_size * 1.42)
        if len(lines) * lh <= max_height:
            chosen_font = f
            wrapped_lines = lines
            line_height = lh
            break

    if chosen_font is None:
        chosen_font = load_font_safe("assets/DancingScript-Medium.ttf", 24)
        wrapped_lines = wrap_text_by_pixel(draw, clean_body, chosen_font, max_width)[:15]
        line_height = 34

    y = 375
    for line in wrapped_lines:
        draw.text((130, y), line, font=chosen_font, fill=ink_dark)
        y += line_height

    # --- C. Area Tanda Tangan & Tanggal ---
    font_label = load_font_safe("assets/DancingScript-Medium.ttf", 26)
    font_sign = load_font_safe("assets/GreatVibes-Regular.ttf", 48)
    font_date = load_font_safe("assets/Poppins-Regular.ttf", 16)

    sign_center_x = 475
    draw.text((sign_center_x, 975), "Tertanda,", font=font_label, fill=ink_soft, anchor="ma")
    draw.text((sign_center_x, 1015), clean_alias, font=font_sign, fill=ink_dark, anchor="ma")
    draw.text((sign_center_x, 1082), f"AMBANIMESQD • {date_str}", font=font_date, fill=ink_soft, anchor="ma")

    buf = io.BytesIO()
    try:
        base.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    finally:
        buf.close()
        base.close()


# ==========================================
# 3. TOMBOL PERMANEN KOTAK POS (FALLBACK DM)
# ==========================================
class ClaimLetterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Buka Surat Rahasia",
        emoji="✉️",
        style=discord.ButtonStyle.secondary,
        custom_id="ambanimesqd_claim_letter_btn"
    )
    async def claim_letter(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = get_pending_letter(interaction.message.id)
        if not row:
            await interaction.response.send_message(
                "Maaf, arsip surat ini sudah tidak ditemukan.", ephemeral=True
            )
            return

        _, receiver_id, receiver_name, salutation, content, alias, date_str = row

        if interaction.user.id != receiver_id:
            await interaction.response.send_message(
                "🔒 Permisi, surat bersegel ini hanya boleh dibuka oleh penerimanya.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with render_lock:
            img_bytes = await asyncio.to_thread(
                render_letter_bytes, receiver_name, salutation, content, alias, date_str
            )

        file = discord.File(io.BytesIO(img_bytes), filename="surat_ambanimesqd.png")
        await interaction.followup.send(
            content=(
                "✉️ *\"Permisi, ini sepucuk surat dari seseorang di **AMBANIMESQD**. "
                "Tolong diterima dengan baik.\"*"
            ),
            file=file,
            ephemeral=True
        )


# ==========================================
# 4. FORMULIR POP-UP (MODAL) TULIS SURAT
# ==========================================
class LetterModal(discord.ui.Modal, title="Kirim Surat Rahasia • AMBANIMESQD"):
    salutation = discord.ui.TextInput(
        label="Salam Pembuka / Judul (Opsional)",
        placeholder="Kosongkan untuk otomatis: Kepada Yang Terhormat, [Nama]",
        required=False,
        max_length=50
    )
    content = discord.ui.TextInput(
        label="Isi Surat / Pesan",
        style=discord.TextStyle.paragraph,
        placeholder="Tuliskan pesan atau perasaan yang ingin kamu sampaikan...",
        required=True,
        max_length=450
    )
    alias = discord.ui.TextInput(
        label="Nama Samaran / Penulis (Opsional)",
        placeholder="Kosongkan untuk: Pengagum Rahasia",
        required=False,
        max_length=30
    )

    def __init__(self, bot: commands.Bot, target: discord.Member):
        super().__init__()
        self.bot = bot
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        sender = interaction.user
        receiver = self.target
        salutation_val = self.salutation.value.strip()
        content_val = self.content.value.strip()
        alias_val = self.alias.value.strip() or "Pengagum Rahasia"
        date_str = datetime.now().strftime("%d %b %Y")

        async with render_lock:
            img_bytes = await asyncio.to_thread(
                render_letter_bytes,
                receiver.display_name,
                salutation_val,
                content_val,
                alias_val,
                date_str
            )

        status_log = "Terkirim via DM"
        dm_embed = discord.Embed(
            title="✉️ Sepucuk Surat untukmu",
            description=(
                f"*\"Permisi, saya membawa sepucuk surat dari seseorang di **{interaction.guild.name}**. "
                f"Tolong diterima dengan baik.\"*\n\n"
                f"Kamu bisa menyimpan gambar surat di bawah ini."
            ),
            color=0xC69C3A
        )
        dm_embed.set_image(url="attachment://surat_ambanimesqd.png")
        dm_embed.set_footer(text="AMBANIMESQD Postal & Registry")

        try:
            dm_file = discord.File(io.BytesIO(img_bytes), filename="surat_ambanimesqd.png")
            await receiver.send(embed=dm_embed, file=dm_file)
            set_cooldown(sender.id)
            await interaction.followup.send(
                f"✉️ Surat rahasiamu untuk {receiver.mention} telah disegel dan berhasil diantarkan ke DM mereka!",
                ephemeral=True
            )

        except discord.Forbidden:
            postbox_ch = interaction.guild.get_channel(POSTBOX_CHANNEL_ID) if POSTBOX_CHANNEL_ID else None

            if postbox_ch:
                status_log = "DM Terkunci -> Masuk Kotak Pos"
                post_embed = discord.Embed(
                    title="🕊️ Ada Surat di Kotak Pos Kerajaan",
                    description=(
                        f"Permisi {receiver.mention}, kurir **AMBANIMESQD Postal** membawa sepucuk surat rahasia "
                        f"untukmu, namun gerbang DM milikmu sedang tertutup.\n\n"
                        f"Klik tombol di bawah ini untuk membukanya secara privat."
                    ),
                    color=0xC69C3A
                )
                msg = await postbox_ch.send(
                    content=receiver.mention,
                    embed=post_embed,
                    view=ClaimLetterView()
                )
                save_pending_letter(
                    msg.id, sender.id, receiver.id,
                    receiver.display_name, salutation_val, content_val, alias_val, date_str
                )
                set_cooldown(sender.id)
                await interaction.followup.send(
                    f"🕊️ DM {receiver.mention} sedang dikunci, jadi suratmu sudah dititipkan di {postbox_ch.mention} "
                    f"dan hanya bisa dibuka oleh penerima secara privat.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"🔒 Maaf, surat tidak dapat diantarkan karena DM milik {receiver.mention} sedang dikunci. "
                    f"Cooldown kamu tidak berkurang.",
                    ephemeral=True
                )
                return

        if LETTER_LOG_CHANNEL_ID:
            log_ch = interaction.guild.get_channel(LETTER_LOG_CHANNEL_ID)
            if log_ch:
                log_embed = discord.Embed(
                    title="📜 Log Pengiriman Surat Rahasia",
                    color=0x8E1C1C,
                    timestamp=datetime.now()
                )
                log_embed.add_field(name="Pengirim Asli", value=f"{sender.mention}\n`{sender.id}`", inline=True)
                log_embed.add_field(name="Penerima", value=f"{receiver.mention}\n`{receiver.id}`", inline=True)
                log_embed.add_field(name="Status", value=status_log, inline=True)
                log_embed.add_field(name="Nama Samaran", value=alias_val, inline=False)
                log_embed.add_field(name="Isi Teks Asli", value=f"```{content_val[:900]}```", inline=False)
                log_embed.set_image(url="attachment://log_surat.png")

                log_file = discord.File(io.BytesIO(img_bytes), filename="log_surat.png")
                await log_ch.send(embed=log_embed, file=log_file)


# ==========================================
# 5. MENU DROPDOWN PILIH PENERIMA & PANEL TOMBOL PERMANEN
# ==========================================
class SelectReceiverView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Cari & pilih member penerima surat...",
        min_values=1,
        max_values=1
    )
    async def select_receiver(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        chosen = select.values[0]
        target = chosen if isinstance(chosen, discord.Member) else interaction.guild.get_member(chosen.id)

        if not target:
            await interaction.response.send_message(
                "⚠️ Member tersebut tidak ditemukan di server ini.",
                ephemeral=True
            )
            return

        if target.bot:
            await interaction.response.send_message(
                "✉️ Kurir tidak dapat mengirimkan surat kepada sesama bot.",
                ephemeral=True
            )
            return

        is_admin = interaction.user.guild_permissions.administrator
        if target.id == interaction.user.id and not is_admin:
            await interaction.response.send_message(
                "✉️ Kamu tidak bisa mengirim surat rahasia ke dirimu sendiri.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(LetterModal(self.bot, target))


class SuratPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Tulis & Kirim Surat Rahasia",
        emoji="✉️",
        style=discord.ButtonStyle.success,
        custom_id="ambanimesqd_open_letter_panel"
    )
    async def open_letter_picker(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = interaction.user.guild_permissions.administrator

        if not is_admin:
            remaining = check_cooldown(interaction.user.id)
            if remaining > 0:
                menit, detik = divmod(remaining, 60)
                await interaction.response.send_message(
                    f"⏳ Kurir masih beristirahat. Kamu bisa mengirim surat lagi dalam **{menit} menit {detik} detik**.",
                    ephemeral=True
                )
                return

        if not os.path.exists(TEMPLATE_PATH):
            await interaction.response.send_message(
                "⚠️ Template surat (`assets/template_surat.png`) belum terpasang di server.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "✉️ **Langkah 1/2:** Pilih member yang ingin kamu kirimi surat rahasia di bawah ini *(hanya kamu yang bisa melihat menu ini)*:",
            view=SelectReceiverView(self.bot),
            ephemeral=True
        )


# ==========================================
# 6. COG UTAMA (/surat, !setup_surat, & Auto-Reply)
# ==========================================
class SuratCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_reply_idx = -1
        init_letter_db()

    # Auto-reply elegan jika ada member mengetik "surat" atau "confess"
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.content.startswith("!"):
            return
        if message.channel.id in {LETTER_LOG_CHANNEL_ID, POSTBOX_CHANNEL_ID, SURAT_CHANNEL_ID}:
            return
        # Cegah balasan ganda jika pesan juga memicu auto-reply intro/kartu di bot.py
        if re.search(r"\b(kartu|card|intro)\b", message.content, re.IGNORECASE):
            return
        if not re.search(r"\b(surat|confess)\b", message.content, re.IGNORECASE):
            return

        surat_ch = message.guild.get_channel(SURAT_CHANNEL_ID) if SURAT_CHANNEL_ID else None
        surat_mention = surat_ch.mention if surat_ch else "`/surat`"

        replies = [
            f"Ingin menyampaikan pesan atau perasaan secara rahasia? Silakan menuju {surat_mention} untuk menulis surat perkamen bersegel, atau gunakan perintah `/surat`.",
            f"Kurir **AMBANIMESQD Postal** siap mengantarkan surat anonimmu. Kunjungi {surat_mention} lalu klik tombol kirim surat di sana.",
            f"Punya sesuatu yang ingin disampaikan diam-diam? Kamu bisa mengirim surat rahasia langsung melalui panel di {surat_mention}."
        ]
        available = [i for i in range(len(replies)) if i != self.last_reply_idx]
        idx = random.choice(available) if available else 0
        self.last_reply_idx = idx

        embed = discord.Embed(description=replies[idx], color=0xD4AF37)
        await message.reply(embed=embed, mention_author=False)

    # Perintah Admin: Memasang panel tombol surat otomatis ke channel SURAT_CHANNEL_ID
    @commands.command(name="setup_surat")
    @commands.has_permissions(administrator=True)
    async def setup_surat(self, ctx: commands.Context):
        target_ch = ctx.guild.get_channel(SURAT_CHANNEL_ID) if SURAT_CHANNEL_ID else ctx.channel
        if target_ch is None:
            target_ch = ctx.channel

        embed = discord.Embed(
            title="✉️ Layanan Surat Rahasia AMBANIMESQD",
            description=(
                "*\"Jika ada perasaan atau pesan yang ingin disampaikan, kurir kami akan mengantarkannya secara rahasia.\"*\n\n"
                "Klik tombol di bawah ini untuk memilih penerima dan menulis suratmu.\n"
                "• Surat akan dicetak di atas **kertas perkamen bersegel**\n"
                "• Dikirim secara **anonim** langsung ke DM penerima\n"
                "• Identitas aslimu tetap dijaga rahasia dari penerima"
            ),
            color=0xD4AF37
        )
        embed.set_footer(text="AMBANIMESQD Postal & Registry")
        await target_ch.send(embed=embed, view=SuratPanelView(self.bot))

        if target_ch.id == ctx.channel.id:
            try:
                await ctx.message.delete()
            except discord.Forbidden:
                pass
        else:
            await ctx.send(f"✉️ Panel Layanan Surat Rahasia berhasil dipasang di {target_ch.mention}!")

    # Slash command /surat tetap tersedia untuk akses cepat dari channel mana pun
    @app_commands.command(
        name="surat",
        description="Kirim surat rahasia bergaya perkamen secara anonim ke member lain"
    )
    @app_commands.describe(penerima="Pilih member yang ingin kamu kirimi surat")
    async def surat_command(self, interaction: discord.Interaction, penerima: discord.Member):
        if penerima.bot:
            await interaction.response.send_message(
                "✉️ Kurir tidak dapat mengirimkan surat kepada sesama bot.",
                ephemeral=True
            )
            return

        is_admin = interaction.user.guild_permissions.administrator

        if penerima.id == interaction.user.id and not is_admin:
            await interaction.response.send_message(
                "✉️ Kamu tidak bisa mengirim surat rahasia ke dirimu sendiri.",
                ephemeral=True
            )
            return

        if not is_admin:
            remaining = check_cooldown(interaction.user.id)
            if remaining > 0:
                menit, detik = divmod(remaining, 60)
                await interaction.response.send_message(
                    f"⏳ Kurir masih beristirahat. Kamu bisa mengirim surat lagi dalam **{menit} menit {detik} detik**.",
                    ephemeral=True
                )
                return

        if not os.path.exists(TEMPLATE_PATH):
            await interaction.response.send_message(
                "⚠️ Template surat (`assets/template_surat.png`) belum terpasang di server.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(LetterModal(self.bot, penerima))


async def setup(bot: commands.Bot):
    bot.add_view(ClaimLetterView())
    bot.add_view(SuratPanelView(bot))
    await bot.add_cog(SuratCog(bot))