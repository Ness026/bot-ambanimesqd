import io
import os
import re
import sqlite3
import textwrap
import unicodedata
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

# 1. Muat konfigurasi dari file .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
INTRO_CHANNEL_ID = int(os.getenv("INTRO_CHANNEL_ID", "0").strip() or "0")
KTP_CHANNEL_ID = int(os.getenv("KTP_CHANNEL_ID", "0").strip() or "0")
MEMBER_ROLE_ID = int(os.getenv("MEMBER_ROLE_ID", "0").strip() or "0")
MALE_ROLE_ID = int(os.getenv("MALE_ROLE_ID", "0").strip() or "0")
FEMALE_ROLE_ID = int(os.getenv("FEMALE_ROLE_ID", "0").strip() or "0")

# Kata kunci pencocokan role server AMBANIMESQD
KARTU_KEYWORDS = ["KING", "QUEEN", "JACK", "SPADE", "DIAMOND", "HEART", "CLUB"]
ANIME_KEYWORDS = [
    "ANIME ENJOYER", "ACTION", "ADVENTURE", "COMEDY", "DRAMA",
    "FANTASY", "ROMANCE", "SCI-FI", "SLICE OF LIFE",
    "SUPRANATURAL", "SUPERNATURAL", "MISTERY", "MYSTERY"
]


# 2. Deteksi otomatis berbagai variasi penulisan gender
def detect_gender(text: str):
    clean = text.strip().lower()
    words = set(re.findall(r"[a-z]+", clean))

    female_exact = {
        "w", "p", "f", "pr", "wanita", "perempuan", "cewek", "cewe", "cwe",
        "female", "woman", "girl", "wedok", "awewe", "akhwat", "betina",
        "she", "her", "sis", "mbak", "teh", "neng", "putri", "ratu"
    }
    male_exact = {
        "l", "m", "lk", "pria", "laki", "lakilaki", "lelaki", "cowok", "cowo", "cwo",
        "male", "man", "boy", "lanang", "lalaki", "ikhwan", "jantan",
        "he", "him", "bro", "mas", "bang", "kang", "pangeran", "raja"
    }

    if words & female_exact:
        return "female"
    if words & male_exact:
        return "male"

    female_sub = ["wanita", "perempuan", "cewek", "cewe", "female", "woman", "girl", "wedok", "awewe", "akhwat"]
    male_sub = ["pria", "laki", "cowok", "cowo", "lanang", "lalaki", "ikhwan", "jantan"]

    if any(k in clean for k in female_sub):
        return "female"
    if any(k in clean for k in male_sub):
        return "male"

    return None


# 3. Pisahkan baris Anime & Hobi dari Biodata Intro secara otomatis
def parse_intro_fields(biodata: str):
    anime_hobby_lines = []
    bio_lines = []

    anime_hobby_pattern = re.compile(
        r"^[\-\•\*\s]*(anime|fav(?:orit|orite)?\s*anime|hobi|hobby|hobbies|manga)\b",
        re.IGNORECASE
    )
    bio_prefix_pattern = re.compile(
        r"^[\-\•\*\s]*(?:bio|biodata(?:\s*intro)?)\s*[:\-]\s*",
        re.IGNORECASE
    )

    for raw_line in biodata.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if anime_hobby_pattern.match(line):
            anime_hobby_lines.append(line)
        else:
            cleaned_bio = bio_prefix_pattern.sub("", line).strip()
            if cleaned_bio:
                bio_lines.append(cleaned_bio)

    return anime_hobby_lines, bio_lines


# 4. Setup Database SQLite untuk menyimpan data intro & nomor urut member
def init_db():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS members (
            user_id INTEGER PRIMARY KEY,
            member_number INTEGER,
            asal TEXT,
            gender TEXT,
            umur_zodiak TEXT,
            waifu_husbu TEXT,
            biodata TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_member_data(user_id, asal, gender, umur_zodiak, waifu_husbu, biodata):
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT member_number FROM members WHERE user_id = ?", (user_id,))
    row = c.fetchone()

    if row:
        member_number = row[0]
        c.execute("""
            UPDATE members
            SET asal = ?, gender = ?, umur_zodiak = ?, waifu_husbu = ?, biodata = ?
            WHERE user_id = ?
        """, (asal, gender, umur_zodiak, waifu_husbu, biodata, user_id))
    else:
        c.execute("SELECT COALESCE(MAX(member_number), 0) + 1 FROM members")
        member_number = c.fetchone()[0]
        c.execute("""
            INSERT INTO members (user_id, member_number, asal, gender, umur_zodiak, waifu_husbu, biodata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, member_number, asal, gender, umur_zodiak, waifu_husbu, biodata))

    conn.commit()
    conn.close()
    return member_number


def get_member_data(user_id):
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("""
        SELECT member_number, asal, gender, umur_zodiak, waifu_husbu, biodata
        FROM members WHERE user_id = ?
    """, (user_id,))
    row = c.fetchone()
    conn.close()
    return row


# 5. Bersihkan emoji dari nama role & ubah font unik nama member jadi huruf normal
def clean_role_name(name: str) -> str:
    cleaned = re.sub(r"[^\x20-\x7E]+", "", name)
    cleaned = cleaned.replace("•", "").strip(" -•|")
    return cleaned if cleaned else name.strip()


def clean_display_name(name: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    cleaned = "".join(ch for ch in normalized if 32 <= ord(ch) <= 126).strip()
    return cleaned[:16] if cleaned else fallback[:16]


# 6. Gambar kapsul (pill badge) role secara otomatis
def draw_role_pills(draw, roles, start_x, start_y, max_x, font):
    x, y = start_x, start_y
    pad_x, pad_y = 14, 7
    gap_x, gap_y = 10, 10

    for role in roles[:4]:
        label = clean_role_name(role.name)
        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        pill_w = text_w + (pad_x * 2)
        pill_h = text_h + (pad_y * 2)

        if x + pill_w > max_x:
            x = start_x
            y += pill_h + gap_y

        rgb = role.color.to_rgb()
        fill_color = rgb if rgb != (0, 0, 0) else (120, 28, 28)

        draw.rounded_rectangle(
            [x, y, x + pill_w, y + pill_h],
            radius=pill_h // 2,
            fill=(*fill_color, 215),
            outline=(212, 175, 55, 255),
            width=1
        )
        draw.text((x + pad_x, y + pad_y - 2), label, font=font, fill=(248, 244, 236, 255))
        x += pill_w + gap_x


# 7. Render gambar ID Card AMBANIMESQD
async def generate_id_card(member: discord.Member, data: tuple) -> io.BytesIO:
    member_number, asal, gender, umur_zodiak, waifu_husbu, biodata = data

    base = Image.open("assets/template.jpeg").convert("RGBA").resize((1280, 720))
    draw = ImageDraw.Draw(base, "RGBA")

    font_name = ImageFont.truetype("assets/Poppins-Bold.ttf", 28)
    font_user = ImageFont.truetype("assets/Poppins-Regular.ttf", 18)
    font_id = ImageFont.truetype("assets/Poppins-Regular.ttf", 17)
    font_val = ImageFont.truetype("assets/Poppins-Bold.ttf", 20)
    font_bio = ImageFont.truetype("assets/Poppins-Regular.ttf", 19)
    font_anime = ImageFont.truetype("assets/Poppins-Regular.ttf", 17)
    font_pill = ImageFont.truetype("assets/Poppins-Bold.ttf", 15)

    ivory = (244, 239, 230, 255)
    soft_gray = (195, 188, 178, 255)

    # --- A. Foto Profil (Avatar) Bulat di Dalam Bingkai Cermin ---
    avatar_bytes = await member.display_avatar.with_size(256).read()
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((152, 152))

    mask = Image.new("L", (152, 152), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 152, 152), fill=255)
    base.paste(avatar, (152, 222), mask)

    # --- B. ID Discord di Kiri Atas ---
    draw.text((352, 102), str(member.id), font=font_id, fill=soft_gray)

    # --- C. Panel Kiri (Nama, Username, Asal, Member Sejak, Gender) ---
    display_name = clean_display_name(member.display_name, member.name)
    draw.text((228, 428), display_name, font=font_name, fill=ivory, anchor="mm")
    draw.text((228, 458), f"@{member.name}", font=font_user, fill=soft_gray, anchor="mm")

    joined_str = member.joined_at.strftime("%d %b %Y") if member.joined_at else datetime.now().strftime("%d %b %Y")
    draw.text((90, 510), asal[:20], font=font_val, fill=ivory)
    draw.text((90, 565), joined_str, font=font_val, fill=ivory)
    draw.text((90, 620), gender[:18], font=font_val, fill=ivory)

    # --- D. Pisahkan teks Anime & Hobi vs Biodata Intro ---
    anime_hobby_lines, bio_lines = parse_intro_fields(biodata)

    # --- E. Kotak Role Kartu (kiri) & Kotak Anime & Hobby (kanan) ---
    role_kartu = [r for r in reversed(member.roles) if any(k in r.name.upper() for k in KARTU_KEYWORDS)]
    role_anime = [r for r in reversed(member.roles) if any(k in r.name.upper() for k in ANIME_KEYWORDS)]

    draw_role_pills(draw, role_kartu, start_x=444, start_y=236, max_x=790, font=font_pill)

    if anime_hobby_lines:
        wrapped_ah = []
        for paragraph in anime_hobby_lines:
            wrapped_ah.extend(textwrap.wrap(paragraph, width=32) or [""])

        ah_y = 234
        for line in wrapped_ah[:3]:
            draw.text((852, ah_y), line, font=font_anime, fill=ivory)
            ah_y += 24
    else:
        draw_role_pills(draw, role_anime, start_x=852, start_y=236, max_x=1198, font=font_pill)

    # --- F. Kotak Biodata Intro ---
    if bio_lines:
        wrapped_bio = []
        for paragraph in bio_lines:
            wrapped_bio.extend(textwrap.wrap(paragraph, width=54) or [""])

        bio_y = 396
        for line in wrapped_bio[:3]:
            draw.text((444, bio_y), line, font=font_bio, fill=ivory)
            bio_y += 28
    else:
        draw.text((444, 396), "Member resmi AMBANIMESQD.", font=font_bio, fill=soft_gray)

    # --- G. 3 Kotak Bawah (Umur/Zodiak, Waifu/Husbu, Nomor Member) ---
    draw.text((444, 576), umur_zodiak[:18], font=font_val, fill=ivory)
    draw.text((715, 576), waifu_husbu[:18], font=font_val, fill=ivory)
    draw.text((986, 576), f"Member #{member_number:03d}", font=font_val, fill=ivory)

    output = io.BytesIO()
    base.save(output, format="PNG")
    output.seek(0)
    return output


# 8. Formulir Pop-up (Modal) Intro
class IntroModal(discord.ui.Modal, title="Formulir Intro AMBANIMESQD"):
    asal = discord.ui.TextInput(label="Asal / Domisili", placeholder="Contoh: Jawa Tengah / Jakarta", max_length=25)
    gender = discord.ui.TextInput(label="Gender", placeholder="Contoh: Pria / Wanita / Cowok / Cewek", max_length=20)
    umur_zodiak = discord.ui.TextInput(label="Umur / Zodiak", placeholder="Contoh: 20 • Scorpio", max_length=20)
    waifu_husbu = discord.ui.TextInput(label="Waifu / Husbu", placeholder="Contoh: Frieren / Gojo", max_length=20)
    biodata = discord.ui.TextInput(
        label="Anime Favorit, Hobi & Biodata",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "Anime Favorit: Attack on Titan\n"
            "Hobi: Gaming, Nonton Anime\n"
            "Bio: Salam kenal semuanya!"
        ),
        max_length=220
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user

        roles_to_add = []
        roles_to_remove = []
        role_info = []

        # 1) Siapkan role ♧ CLUB
        member_role = interaction.guild.get_role(MEMBER_ROLE_ID)
        if member_role:
            role_info.append(member_role.name)
            if member_role not in member.roles:
                roles_to_add.append(member_role)

        # 2) Deteksi & siapkan role Gender (Pria / Wanita)
        gender_type = detect_gender(self.gender.value)
        male_role = interaction.guild.get_role(MALE_ROLE_ID)
        female_role = interaction.guild.get_role(FEMALE_ROLE_ID)

        if gender_type == "male" and male_role:
            role_info.append(male_role.name)
            if male_role not in member.roles:
                roles_to_add.append(male_role)
            if female_role and female_role in member.roles:
                roles_to_remove.append(female_role)
        elif gender_type == "female" and female_role:
            role_info.append(female_role.name)
            if female_role not in member.roles:
                roles_to_add.append(female_role)
            if male_role and male_role in member.roles:
                roles_to_remove.append(male_role)

        # Terapkan perubahan role ke member
        try:
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Selesai mengisi intro")
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Update gender intro")
        except discord.Forbidden:
            print(
                f"PERINGATAN: Bot gagal mengatur role untuk {member.display_name}. "
                f"Pastikan posisi role bot berada di atas role CLUB dan role Gender."
            )
        except Exception as e:
            print(f"Error saat mengatur role: {e}")

        # Ambil data member terbaru dari Discord agar role yang baru masuk langsung tampil di kartu pertama
        try:
            member = await interaction.guild.fetch_member(member.id)
        except Exception:
            pass

        # Simpan data ke database
        member_number = save_member_data(
            member.id, self.asal.value, self.gender.value,
            self.umur_zodiak.value, self.waifu_husbu.value, self.biodata.value
        )

        # Render ID Card
        data = (member_number, self.asal.value, self.gender.value,
                self.umur_zodiak.value, self.waifu_husbu.value, self.biodata.value)
        card_buffer = await generate_id_card(member, data)
        file = discord.File(fp=card_buffer, filename=f"ktp_{member.id}.png")

        role_text = ", ".join(f"**{r}**" for r in role_info) if role_info else "member"

        # Kirim ke channel hasil ID Card
        ktp_channel = interaction.guild.get_channel(KTP_CHANNEL_ID)
        if ktp_channel:
            await ktp_channel.send(
                content=f"🪪 **KARTU IDENTITAS AMBANIMESQD** — Selamat bergabung {member.mention}!",
                file=file
            )
            await interaction.followup.send(
                f"Intro berhasil disimpan! Role {role_text} sudah aktif dan ID Card kamu dikirim ke {ktp_channel.mention}.",
                ephemeral=True
            )
        else:
            await interaction.followup.send("ID Card berhasil dibuat!", file=file, ephemeral=True)


# 9. Tombol Permanen (tetap aktif meski bot restart)
class IntroButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Isi Intro & Buat ID Card",
        emoji="🪪",
        style=discord.ButtonStyle.success,
        custom_id="ambanimesqd_intro_button"
    )
    async def open_intro_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(IntroModal())


# 10. Inisialisasi Bot
class AmbanimesqdBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(IntroButtonView())
        await self.tree.sync()

    async def on_ready(self):
        print(f"Bot berhasil online sebagai {self.user}!")


bot = AmbanimesqdBot()


# Event: Sapa otomatis saat member baru join
@bot.event
async def on_member_join(member: discord.Member):
    intro_ch = member.guild.get_channel(INTRO_CHANNEL_ID)
    if intro_ch:
        await intro_ch.send(
            f"Selamat datang {member.mention} di **AMBANIMESQD**! "
            f"Silakan baca rules lalu klik tombol **Isi Intro & Buat ID Card** di channel ini untuk membuka akses server."
        )


# Perintah Admin: Memasang panel tombol intro (ketik !setup_intro di channel intro)
@bot.command()
@commands.has_permissions(administrator=True)
async def setup_intro(ctx: commands.Context):
    embed = discord.Embed(
        title="🪪 Pendaftaran Member AMBANIMESQD",
        description=(
            "Klik tombol di bawah ini untuk mengisi formulir intro.\n"
            "Setelah selesai, bot akan otomatis:\n"
            "• Memberikan role **♧ CLUB** & role **Gender**\n"
            "• Mencetak **Kartu Identitas AMBANIMESQD** milikmu"
        ),
        color=0xD4AF37
    )
    await ctx.send(embed=embed, view=IntroButtonView())


# Perintah Slash: /ktp untuk melihat atau memperbarui tampilan role di ID Card
@bot.tree.command(name="ktp", description="Tampilkan Kartu Identitas AMBANIMESQD milikmu atau member lain")
@app_commands.describe(member="Pilih member (kosongkan untuk melihat KTP sendiri)")
async def ktp_command(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    data = get_member_data(target.id)
    if not data:
        await interaction.response.send_message(
            f"{target.mention} belum mengisi intro. Silakan isi lewat tombol di channel intro dulu!",
            ephemeral=True
        )
        return

    await interaction.response.defer()
    card_buffer = await generate_id_card(target, data)
    file = discord.File(fp=card_buffer, filename=f"ktp_{target.id}.png")
    await interaction.followup.send(
        content=f"🪪 **KARTU IDENTITAS AMBANIMESQD — {target.display_name}**",
        file=file
    )


if __name__ == "__main__":
    if not TOKEN or TOKEN == "TEMPEL_TOKEN_BARU_KAMU_DI_SINI":
        print("ERROR: Token belum diisi di file .env!")
    else:
        init_db()
        bot.run(TOKEN)