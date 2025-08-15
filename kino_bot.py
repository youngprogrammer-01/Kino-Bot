#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kino Bot (Disk-first minimal version)
- Admin video/document yuboradi -> bot 'kinolar/' papkaga saqlaydi -> kanalga shablon bilan post qiladi -> faylni lokal diskdan o'chiradi
- Bazaga (movies.json) faqat: name, code, channel_message_id saqlanadi
- Foydalanuvchi kod yuborsa -> bot kanalidagi shu xabarni copy qilib userga yuboradi (fayl qayta yuklanmaydi)

Talablar:
- aiogram v3.7+
- .env: BOT_TOKEN, CHANNEL_ID, ADMIN_PHONES
"""

import asyncio
import json
import logging
import os
import random
import re
import string
from pathlib import Path
from typing import Dict, Any, Optional
import html

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")  # Backward compat: treated as PREVIEW if specific vars not set
FULL_CHANNEL_ID = os.getenv("FULL_CHANNEL_ID", "@myfilms_01")
PREVIEW_CHANNEL_ID = os.getenv("PREVIEW_CHANNEL_ID", CHANNEL_ID or "@uzbekchakinolar60")
ADMIN_PHONES = [p.strip() for p in os.getenv("ADMIN_PHONES", "").split(",") if p.strip()]
Bot_url = os.getenv("Bot_url")
if not BOT_TOKEN or not ADMIN_PHONES:
    raise RuntimeError(".env da BOT_TOKEN, ADMIN_PHONES to'ldiring. Kanal ID lar uchun FULL_CHANNEL_ID va PREVIEW_CHANNEL_ID ni ham kiriting.")

BASE_DIR = Path(__file__).parent
KINOLAR_DIR = BASE_DIR / "kinolar"
KINOLAR_DIR.mkdir(parents=True, exist_ok=True)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ====== DB ======
class DB:
    def __init__(self, base: Path):
        self.users_p = base / "users.json"
        self.movies_p = base / "movies.json"
        self.users: Dict[int, Dict[str, Any]] = {}
        self.movies: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        self.users = self._load(self.users_p, key_cast=int)
        self.movies = self._load(self.movies_p, key_cast=None)

    def _load(self, path: Path, key_cast=None):
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if key_cast:
                return {key_cast(k): v for k, v in data.items()}
            return data
        except Exception as e:
            logging.error(f"JSON load error for {path}: {e}\
            {path.read_text(encoding='utf-8') if path.exists() else ''}")
            return {}

    def _save(self, path: Path, data: Dict):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def save_users(self):
        self._save(self.users_p, {str(k): v for k, v in self.users.items()})

    def save_movies(self):
        self._save(self.movies_p, self.movies)

    @staticmethod
    def norm_phone(phone: str) -> str:
        t = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if t.startswith("00"): t = "+" + t[2:]
        if not t.startswith("+") and t.isdigit():
            t = "+" + t
        return t

    def is_admin_phone(self, phone: str) -> bool:
        p = self.norm_phone(phone)
        return p in [self.norm_phone(a) for a in ADMIN_PHONES]

    def upsert_user(self, uid: int, name: str, phone: str, is_admin: bool):
        self.users[uid] = {"name": name, "phone": self.norm_phone(phone), "is_admin": is_admin}
        self.save_users()

    def get_user(self, uid: int) -> Optional[Dict[str, Any]]:
        return self.users.get(uid)

    def is_admin(self, uid: int) -> bool:
        u = self.get_user(uid)
        return bool(u and u.get("is_admin"))

    def add_movie(self, code: str, info: Dict[str, Any]):
        self.movies[code] = info
        self.save_movies()

    def get_movie(self, code: str) -> Optional[Dict[str, Any]]:
        return self.movies.get(code.upper())


db = DB(BASE_DIR)

# ====== HELPERS ======
ALPHABET = string.digits

def gen_code() -> str:
    """2-3 xonali faqat raqamlardan iborat unikal kod"""
    for _ in range(2000):
        l = random.choice([2, 3])
        c = "".join(random.choices(ALPHABET, k=l))
        if not db.get_movie(c):
            return c
    # Fallback 3 xonali
    while True:
        c = "".join(random.choices(ALPHABET, k=3))
        if not db.get_movie(c):
            return c

SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_\- ]+")

def safe_filename(name: str, ext: str) -> str:
    base = SAFE_NAME_RE.sub("", name).strip() or "kino"
    return f"{base}.{ext}"

class KB:
    @staticmethod
    def admin():
        return types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="🎬 Kanalga kino joylash")],
                [types.KeyboardButton(text="👥 Foydalanuvchilar")]
            ], resize_keyboard=True
        )

    @staticmethod
    def contact():
        return types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )

    @staticmethod
    def remove():
        return types.ReplyKeyboardRemove()


def full_caption(name: str, year: str, genre: str, duration: str, code: str,
                 country: str = "-", imdb: str = "-", quality: str = "-", language: str = "-") -> str:
    # HTML parse mode: maxsus belgilarni escape qilamiz
    s_name = html.escape(name or "-")
    s_year = html.escape(year or "-")
    s_genre = html.escape(genre or "-")
    s_country = html.escape(country or "-")
    s_imdb = html.escape(imdb or "-")
    s_quality = html.escape(quality or "-")
    s_language = html.escape(language or "-")
    channel_url = f"https://t.me/{PREVIEW_CHANNEL_ID.lstrip('@')}"
    bot_url = f"https://t.me/{Bot_url.lstrip('@')}"
    return (
        f"🎬: &quot;{s_name}&quot; [{s_year}]\n"
        f"➖➖➖➖➖➖➖➖➖➖\n"
        f"• 🌍Davlati: {s_country} \n"
        f"• 🌟IMBD: {s_imdb} \n"
        f"• 🎭Janri: {s_genre}\n"
        f"• 📸Sifat: {s_quality}\n"
        f"• 🇺🇿Tili: {s_language}\n\n"
        f"🔢 Kino kodi: <code>{html.escape(code)}</code>\n\n"
        f"🔹Kanal: <a href=\"{channel_url}\">©️KinolarOlami</a>\n"
        f"🔹Bot: <a href=\"{bot_url}\">CinemadiaUz bot</a>"

    )

def preview_caption(name: str, code: str) -> str:
    return (
        f"🎬 {name}\n"
        f"🔑 Kod: `{code}`\n\n"
        "Kodni botga yuboring va kinoni oling."
    )

# ====== SUBSCRIPTION CHECK ======
async def is_subscribed_to_preview(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(PREVIEW_CHANNEL_ID, user_id)
        return member.status in {"member", "administrator", "creator"}
    except Exception:
        return False

def subscribe_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Kanalga obuna bo'lish", url=f"https://t.me/{PREVIEW_CHANNEL_ID.lstrip('@')}")],
        [InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub")]
    ])

# ====== FILTERS ======
class IsAdmin(BaseFilter):
    async def __call__(self, m: types.Message) -> bool:
        return db.is_admin(m.from_user.id)

class ContactSelf(BaseFilter):
    async def __call__(self, m: types.Message) -> bool:
        return bool(m.contact and m.contact.user_id == m.from_user.id)

# ====== STATES ======
class Reg(StatesGroup):
    name = State()
    contact = State()

class Up(StatesGroup):
    file = State()
    name = State()
    year = State()
    genre = State()
    country = State()
    imdb = State()
    quality = State()
    language = State()
    duration = State()
    preview = State()

# ====== COMMANDS ======
@dp.message(Command("start"))
async def start(m: types.Message, state: FSMContext):
    await state.clear()
    u = db.get_user(m.from_user.id)
    if u:
        if u.get("is_admin"):
            await m.answer("Salom Admin! Kanalga kino joylashingiz mumkin!", reply_markup=KB.admin())
        else:
            await m.answer("Salom! Avval kanalga obuna bo'ling, so'ngra kod yuboring.", reply_markup=KB.remove())
            if not await is_subscribed_to_preview(m.from_user.id):
                await m.answer("Iltimos, avval quyidagi kanalga obuna bo'ling:", reply_markup=KB.remove())
                await m.answer(f"{PREVIEW_CHANNEL_ID}", reply_markup=subscribe_kb())
    else:
        await m.answer("Xush kelibsiz! Ismingizni kiriting:", reply_markup=KB.remove())
        await state.set_state(Reg.name)

# (handler order fixed) Generic Up.preview fallback moved below specific handlers

# ====== REG ======
@dp.message(Reg.name)
async def reg_name(m: types.Message, state: FSMContext):
    name = (m.text or "").strip()
    if len(name) < 2:
        await m.answer("Ism kamida 2 belgi bo'lsin.")
        return
    await state.update_data(name=name)
    await m.answer("Telefon raqamingizni tugma orqali yuboring:", reply_markup=KB.contact())
    await state.set_state(Reg.contact)

@dp.message(Reg.contact, ContactSelf())
async def reg_contact(m: types.Message, state: FSMContext):
    name = (await state.get_data())["name"]
    phone = m.contact.phone_number
    is_admin = db.is_admin_phone(phone)
    db.upsert_user(m.from_user.id, name, phone, is_admin)
    await state.clear()
    if is_admin:
        await m.answer("Admin sifatida ro'yxatdan o'tdingiz. Endi kino joylashni boshlang yoki menyuni tanlang.", reply_markup=KB.admin())
    else:
        await m.answer("Ro'yxatdan o'tdingiz! Kod yuboring.", reply_markup=KB.remove())

@dp.message(Reg.contact)
async def reg_contact_invalid(m: types.Message):
    await m.answer("Iltimos, kontaktni tugma orqali yuboring.", reply_markup=KB.contact())

# ====== ADMIN UPLOAD ======
@dp.message(IsAdmin(), F.text == "🎬 Kanalga kino joylash")
async def admin_hint(m: types.Message, state: FSMContext):
    # Agar hozir preview bosqichida bo'lsa, avval preview yuborishni so'raymiz
    cur = await state.get_state()
    if cur == Up.preview.state:
        await m.answer("Avval preview (rasm yoki qisqa video) yuboring.")
        return
    await state.set_state(Up.file)
    await m.answer("Iltimos video yoki video-hujjat yuboring.", reply_markup=KB.remove())

@dp.message(IsAdmin(), F.text == "👥 Foydalanuvchilar")
async def admin_users(m: types.Message):
    users = db.users
    total = len(users)
    # Bir nechta namuna ko'rsatamiz (eng ko'pi 20 ta)
    lines = [f"👥 Jami foydalanuvchilar: {total}"]
    cnt = 0
    for uid, info in users.items():
        cnt += 1
        flag = "(admin)" if info.get("is_admin") else ""
        lines.append(f"• {info.get('name','?')} {flag} — {info.get('phone','?')} — id:{uid}")
        if cnt >= 20:
            break
    await m.answer("\n".join(lines), reply_markup=KB.admin())

@dp.message(IsAdmin(), F.video)
async def admin_video(m: types.Message, state: FSMContext):
    # Agar hozir preview bosqichida bo'lsa, bu handler ishlamasin
    cur = await state.get_state()
    if cur == Up.preview.state:
        return
    await state.clear()
    await state.update_data(file_id=m.video.file_id, file_type="video")
    await m.answer("Kino nomini kiriting:")
    await state.set_state(Up.name)

@dp.message(IsAdmin(), F.document)
async def admin_document(m: types.Message, state: FSMContext):
    # Agar hozir preview bosqichida bo'lsa, bu handler ishlamasin
    cur = await state.get_state()
    if cur == Up.preview.state:
        return
    await state.clear()
    doc = m.document
    mt = (doc.mime_type or "").lower()
    if mt.startswith("video/") or (doc.file_name or "").lower().endswith((".mp4", ".mkv", ".avi", ".mov")):
        await state.update_data(file_id=doc.file_id, file_type="document", filename=doc.file_name)
        await m.answer("Kino nomini kiriting:")
        await state.set_state(Up.name)
    else:
        await m.answer("Faqat video yuboring (mp4/mkv/avi/mov).")

@dp.message(Up.name)
async def up_name(m: types.Message, state: FSMContext):
    name = (m.text or "").strip()
    if not name:
        await m.answer("Kino nomini kiriting:")
        return
    await state.update_data(name=name)
    await m.answer("Yilini kiriting (masalan, 2024):")
    await state.set_state(Up.year)

@dp.message(Up.year)
async def up_year(m: types.Message, state: FSMContext):
    await state.update_data(year=(m.text or "").strip())
    await m.answer("Janrni kiriting (masalan, Drama):")
    await state.set_state(Up.genre)

@dp.message(Up.genre)
async def up_genre(m: types.Message, state: FSMContext):
    await state.update_data(genre=(m.text or "").strip())
    await m.answer("Davlati (masalan, AQSH):")
    await state.set_state(Up.country)

@dp.message(Up.country)
async def up_country(m: types.Message, state: FSMContext):
    await state.update_data(country=(m.text or "").strip())
    await m.answer("IMBD (masalan, 7/10):")
    await state.set_state(Up.imdb)

@dp.message(Up.imdb)
async def up_imdb(m: types.Message, state: FSMContext):
    await state.update_data(imdb=(m.text or "").strip())
    await m.answer("Sifat (masalan, 720P):")
    await state.set_state(Up.quality)

@dp.message(Up.quality)
async def up_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=(m.text or "").strip())
    await m.answer("Tili (masalan, Uzbekcha):")
    await state.set_state(Up.language)

@dp.message(Up.language)
async def up_language(m: types.Message, state: FSMContext):
    await state.update_data(language=(m.text or "").strip())
    await m.answer("Davomiylik (masalan, 1h50m):")
    await state.set_state(Up.duration)

@dp.message(Up.duration)
async def up_duration(m: types.Message, state: FSMContext):
    await state.update_data(duration=(m.text or "").strip())
    # Kodni bot o'zi tanlaydi (2-3 xonali raqam, unikal)
    code = gen_code()
    await state.update_data(code=code)

    # Faylni yuklab olish va FULL_CHANNEL_ID ga yuborish
    data = await state.get_data()
    name = data["name"]
    file_id = data["file_id"]
    file_type = data.get("file_type", "video")
    downloaded = False
    ext = "mp4"
    local_path = None
    try:
        tg_file = await bot.get_file(file_id)
        if file_type == "document":
            fn = data.get("filename") or "file.bin"
            low = fn.lower()
            if low.endswith((".mkv", ".avi", ".mov", ".mp4")):
                ext = low.split(".")[-1]
        local_name = safe_filename(name, ext)
        local_path = KINOLAR_DIR / local_name
        await bot.download(tg_file, destination=local_path)
        downloaded = True
    except TelegramBadRequest as e:
        logging.warning(f"Faylni yuklab olish imkoni yo'q (TelegramBadRequest): {e}. file_id orqali yuborishga o'tamiz.")
    except Exception as e:
        logging.warning(f"Faylni yuklab olishda xato: {e}. file_id orqali yuborishga o'tamiz.")

    cap_full = full_caption(
        name=name,
        year=data.get('year','-'),
        genre=data.get('genre','-'),
        duration=data.get('duration','-'),
        code=code,
        country=data.get('country','-'),
        imdb=data.get('imdb','-'),
        quality=data.get('quality','-'),
        language=data.get('language','-'),
    )

    sent_full: types.Message
    if file_type == "document":
        if downloaded and local_path is not None:
            sent_full = await bot.send_document(FULL_CHANNEL_ID, FSInputFile(local_path), caption=cap_full)
        else:
            sent_full = await bot.send_document(FULL_CHANNEL_ID, file_id, caption=cap_full)
    else:
        if downloaded and local_path is not None:
            sent_full = await bot.send_video(FULL_CHANNEL_ID, FSInputFile(local_path), caption=cap_full)
        else:
            sent_full = await bot.send_video(FULL_CHANNEL_ID, file_id, caption=cap_full)

    # Lokal faylni faqat yuklangan bo'lsa o'chiramiz
    if downloaded and local_path is not None:
        try:
            local_path.unlink(missing_ok=True)
        except Exception as e:
            logging.warning(f"Fayl o'chirishda xatolik: {e}")

    # Bazaga asosiy ma'lumotlarni vaqtincha previewsiz saqlaymiz
    db.add_movie(code, {
        "name": name,
        "year": data.get("year","-"),
        "genre": data.get("genre","-"),
        "duration": data.get("duration","-"),
        "full_message_id": sent_full.message_id,
        "preview_message_id": None
    })

    await m.answer("Asosiy kanal (preview) uchun rasm yoki qisqa video yuboring:")
    await state.set_state(Up.preview)

@dp.message(Up.preview, F.photo)
async def up_preview_photo(m: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data["code"]
    name = data["name"]
    # Preview postida ham to'liq shablon ishlatiladi
    cap_prev = full_caption(name, data.get('year','-'), data.get('genre','-'), data.get('duration','-'), code,
                            data.get('country','-'), data.get('imdb','-'), data.get('quality','-'), data.get('language','-'))
    try:
        sent = await bot.send_photo(PREVIEW_CHANNEL_ID, m.photo[-1].file_id, caption=cap_prev)
    except Exception as e:
        logging.error(f"Preview photo yuborishda xato: {e}")
        await m.answer("Preview kanalga yuborishda xato. Botni preview kanalga admin qilganingizni va fayl formatini tekshirib qayta urinib ko'ring.")
        return
    rec = db.get_movie(code) or {}
    rec["preview_message_id"] = sent.message_id
    db.add_movie(code, rec)
    await m.answer("Preview kanalga joylandi!", reply_markup=KB.admin())
    await state.clear()

# Up.preview holatida noto'g'ri kontent turlari uchun javob (fallback) - eng oxirida turishi kerak
@dp.message(Up.preview)
async def up_preview_other(m: types.Message):
    await m.answer("Iltimos, preview uchun rasm, video, video note yoki GIF yuboring.")

@dp.message(Up.preview, F.video_note)
async def up_preview_video_note(m: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data["code"]
    name = data["name"]
    cap_prev = full_caption(
        name,
        data.get('year','-'),
        data.get('genre','-'),
        data.get('duration','-'),
        code,
        data.get('country','-'),
        data.get('imdb','-'),
        data.get('quality','-'),
        data.get('language','-')
    )
    try:
        sent = await bot.send_video_note(PREVIEW_CHANNEL_ID, m.video_note.file_id)
        # Video note caption yo'q; alohida matn bilan yuboramiz
        await bot.send_message(PREVIEW_CHANNEL_ID, cap_prev)
    except Exception:
        # Agar video_note yuborish mumkin bo'lmasa, oddiy video sifatida urinib ko'ramiz
        sent = await bot.send_video(PREVIEW_CHANNEL_ID, m.video_note.file_id, caption=cap_prev)
    rec = db.get_movie(code) or {}
    rec["preview_message_id"] = sent.message_id
    db.add_movie(code, rec)
    await m.answer("Preview kanalga joylandi!", reply_markup=KB.admin())
    await state.clear()

@dp.message(Up.preview, F.animation)
async def up_preview_gif(m: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data["code"]
    name = data["name"]
    cap_prev = full_caption(
        name,
        data.get('year','-'),
        data.get('genre','-'),
        data.get('duration','-'),
        code,
        data.get('country','-'),
        data.get('imdb','-'),
        data.get('quality','-'),
        data.get('language','-')
    )
    sent = await bot.send_animation(PREVIEW_CHANNEL_ID, m.animation.file_id, caption=cap_prev)
    rec = db.get_movie(code) or {}
    rec["preview_message_id"] = sent.message_id
    db.add_movie(code, rec)
    await m.answer("Preview kanalga joylandi!", reply_markup=KB.admin())
    await state.clear()

# Document sifatida yuborilgan preview (rasm/video) ni ham qabul qilamiz
@dp.message(Up.preview, F.document)
async def up_preview_document(m: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data["code"]
    name = data["name"]
    cap_prev = full_caption(
        name,
        data.get('year','-'),
        data.get('genre','-'),
        data.get('duration','-'),
        code,
        data.get('country','-'),
        data.get('imdb','-'),
        data.get('quality','-'),
        data.get('language','-')
    )
    doc = m.document
    mt = (doc.mime_type or "").lower()
    fn = (doc.file_name or "").lower()
    try:
        if mt.startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp")):
            sent = await bot.send_photo(PREVIEW_CHANNEL_ID, doc.file_id, caption=cap_prev)
        elif mt.startswith("video/") or fn.endswith((".mp4", ".mov", ".mkv", ".avi")):
            sent = await bot.send_video(PREVIEW_CHANNEL_ID, doc.file_id, caption=cap_prev)
        else:
            await m.answer("Iltimos, preview uchun rasm yoki qisqa video yuboring.")
            return
    except Exception as e:
        logging.error(f"Preview (document) yuborishda xato: {e}")
        await m.answer("Preview yuborishda xatolik. Keyinroq urinib ko'ring yoki boshqa format yuboring.")
        return

    rec = db.get_movie(code) or {}
    rec["preview_message_id"] = sent.message_id
    db.add_movie(code, rec)
    await m.answer("Preview kanalga joylandi!", reply_markup=KB.admin())
    await state.clear()

@dp.message(Up.preview, F.video)
async def up_preview_video(m: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data["code"]
    name = data["name"]
    # Preview postida ham to'liq shablon ishlatiladi
    cap_prev = full_caption(
        name,
        data.get('year','-'),
        data.get('genre','-'),
        data.get('duration','-'),
        code,
        data.get('country','-'),
        data.get('imdb','-'),
        data.get('quality','-'),
        data.get('language','-')
    )
    try:
        sent = await bot.send_video(PREVIEW_CHANNEL_ID, m.video.file_id, caption=cap_prev)
    except Exception as e:
        logging.error(f"Preview video yuborishda xato: {e}")
        await m.answer("Preview kanalga yuborishda xato. Botni preview kanalga admin qilganingizni va fayl formatini tekshirib qayta urinib ko'ring.")
        return
    rec = db.get_movie(code) or {}
    rec["preview_message_id"] = sent.message_id
    db.add_movie(code, rec)
    await m.answer("Preview kanalga joylandi!", reply_markup=KB.admin())
    await state.clear()

# ====== USER: GET BY CODE ======
@dp.message()
async def user_by_code(m: types.Message):
    u = db.get_user(m.from_user.id)
    if not u:
        await m.answer("Avval /start orqali ro'yxatdan o'ting.")
        return
    if u.get("is_admin"):
        return  # admin uchun kod handler ishlatmaymiz
    # Obuna tekshiruvi
    if not await is_subscribed_to_preview(m.from_user.id):
        await m.answer("Botdan foydalanish uchun kanalga obuna bo'ling:")
        await m.answer(f"{PREVIEW_CHANNEL_ID}", reply_markup=subscribe_kb())
        return
    code = (m.text or "").strip().upper()
    rec = db.get_movie(code)
    if not rec:
        await m.answer("Bunday kod topilmadi!")
        return
    try:
        await bot.copy_message(chat_id=m.chat.id, from_chat_id=FULL_CHANNEL_ID, message_id=rec["full_message_id"])
    except Exception as e:
        logging.error(f"copy_message error: {e}")
        await m.answer("Hozircha yuborib bo'lmadi. Keyinroq urinib ko'ring.")

# ====== CALLBACK: SUBSCRIPTION RE-CHECK ======
@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: types.CallbackQuery):
    user_id = call.from_user.id
    if await is_subscribed_to_preview(user_id):
        await call.message.answer("✅ Obuna tasdiqlandi! Endi kodni yuborishingiz mumkin.")
    else:
        await call.message.answer("Hali obuna bo'lmadingiz. Iltimos, kanalga obuna bo'ling.")
        await call.message.answer(f"{PREVIEW_CHANNEL_ID}", reply_markup=subscribe_kb())
    await call.answer()

# ====== RUN ======
async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
