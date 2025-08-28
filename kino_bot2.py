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
import string
import re
import time
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
from aiogram.fsm.storage.base import StorageKey
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
# Ixtiyoriy: ADMIN_IDS orqali telefon so'ramasdan admin aniqlash
try:
    ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
except Exception:
    ADMIN_IDS = []
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

    def _save(self, path: Path, data: Dict[str, Any]):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # Atomic replace with retries to survive Windows file locks
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError as e:
                logging.warning(f"save: replace failed due to PermissionError (attempt {attempt+1}/5) for {path.name}: {e}")
                time.sleep(0.25)
        # Fallback: direct write to the target file
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # Cleanup tmp if possible
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        except Exception as e2:
            logging.error(f"save: fallback direct write failed for {path.name}. Temp left at {tmp}: {e2}")

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

    def is_admin_id(self, uid: int) -> bool:
        return uid in ADMIN_IDS

    def upsert_user(self, uid: int, name: str, phone: str, is_admin: bool):
        # Mavjud foydalanuvchining sevimlilarini saqlab qolamiz
        existing = self.users.get(uid, {})
        fav = existing.get("fav", [])
        rand_hist = existing.get("rand_hist", [])
        self.users[uid] = {"name": name, "phone": self.norm_phone(phone), "is_admin": is_admin, "fav": fav, "rand_hist": rand_hist}
        self.save_users()

    def get_user(self, uid: int) -> Optional[Dict[str, Any]]:
        u = self.users.get(uid)
        if u is not None:
            changed = False
            if "fav" not in u:
                u["fav"] = []
                changed = True
            if "rand_hist" not in u:
                u["rand_hist"] = []
                changed = True
            if changed:
                self.users[uid] = u
                self.save_users()
        return u

    def is_admin(self, uid: int) -> bool:
        u = self.get_user(uid)
        return bool(u and u.get("is_admin"))

    def add_movie(self, code: str, info: Dict[str, Any]):
        # Default statistik maydonlarni qo'shib saqlaymiz
        info = dict(info)
        stats = info.get("stats", {}) or {}
        stats.setdefault("views", 0)
        stats.setdefault("likes", {"users": [], "count": 0})
        stats.setdefault("ratings", {"users": {}, "sum": 0, "count": 0})
        info["stats"] = stats
        # Yaroqsizlik flagi
        if "broken" not in info:
            info["broken"] = False
        self.movies[code] = info
        self.save_movies()

    def get_movie(self, code: str) -> Optional[Dict[str, Any]]:
        return self.movies.get(code)

    def mark_broken(self, code: str):
        rec = self.movies.get(code)
        if not rec:
            return
        if not rec.get("broken"):
            rec["broken"] = True
            self.movies[code] = rec
            self.save_movies()

    # ==== Movie statistika amallari ====
    def inc_view(self, code: str):
        rec = self.get_movie(code)
        if not rec:
            return
        rec.setdefault("stats", {}).setdefault("views", 0)
        rec["stats"]["views"] += 1
        self.add_movie(code, rec)

    def toggle_like(self, code: str, uid: int) -> bool:
        """Like yoqadi/yopadi. True=like qo'shildi, False=olib tashlandi"""
        rec = self.get_movie(code)
        if not rec:
            return False
        stats = rec.setdefault("stats", {})
        likes = stats.setdefault("likes", {"users": [], "count": 0})
        users = set(likes.get("users", []))
        if uid in users:
            users.remove(uid)
            action_added = False
        else:
            users.add(uid)
            action_added = True
        likes["users"] = list(users)
        likes["count"] = len(users)
        stats["likes"] = likes
        rec["stats"] = stats
        self.add_movie(code, rec)
        return action_added

    def rate_movie(self, code: str, uid: int, rating: int):
        """Foydalanuvchi bahosini qo'yadi (1..5). Avvalgi bahosi bo'lsa yangilanadi."""
        rating = max(1, min(5, int(rating)))
        rec = self.get_movie(code)
        if not rec:
            return
        stats = rec.setdefault("stats", {})
        ratings = stats.setdefault("ratings", {"users": {}, "sum": 0, "count": 0})
        users = ratings.setdefault("users", {})
        old = users.get(str(uid))
        if old is None:
            ratings["sum"] += rating
            ratings["count"] += 1
        else:
            ratings["sum"] += rating - int(old)
        users[str(uid)] = rating
        stats["ratings"] = ratings
        rec["stats"] = stats
        self.add_movie(code, rec)

    # ==== Favorites (Sevimlilar) ====
    def toggle_favorite(self, uid: int, code: str) -> bool:
        """Foydalanuvchining sevimlilariga qo'shadi yoki o'chiradi. True=qo'shildi, False=o'chirildi"""
        u = self.get_user(uid)
        if not u:
            return False
        favs = set(u.get("fav", []))
        if code in favs:
            favs.remove(code)
            added = False
        else:
            favs.add(code)
            added = True
        u["fav"] = list(favs)
        self.users[uid] = u
        self.save_users()
        return added

    def get_favorites(self, uid: int):
        u = self.get_user(uid) or {}
        return list(u.get("fav", []))

    # ==== Random history per user ====
    def get_random_history(self, uid: int):
        u = self.get_user(uid) or {}
        return list(u.get("rand_hist", []))

    def push_random_history(self, uid: int, code: str, max_len: int = 20):
        u = self.get_user(uid)
        if not u:
            return
        hist = list(u.get("rand_hist", []))
        if code in hist:
            hist.remove(code)
        hist.append(code)
        if len(hist) > max_len:
            hist = hist[-max_len:]
        u["rand_hist"] = hist
        self.users[uid] = u
        self.save_users()

    def clear_random_history(self, uid: int):
        u = self.get_user(uid)
        if not u:
            return
        u["rand_hist"] = []
        self.users[uid] = u
        self.save_users()


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
                [types.KeyboardButton(text="👥 Foydalanuvchilar")],
                [types.KeyboardButton(text="👥 Botdagi azolar")],
                [types.KeyboardButton(text="📣 Kanaldagi azolar")]
            ], resize_keyboard=True
        )


    @staticmethod
    def remove():
        return types.ReplyKeyboardRemove()

    @staticmethod
    def user():
        return types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="🎟 Kod yuborish")],
                [types.KeyboardButton(text="🔍 Random"), types.KeyboardButton(text="⭐ Top")],
                [types.KeyboardButton(text="💖 Sevimlilar"), types.KeyboardButton(text="📚 Yordam")],
                [types.KeyboardButton(text="🔔 Obuna tekshirish")]
            ], resize_keyboard=True
        )


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
    )

def preview_channel_caption(code: str) -> str:
    # Kod orqali bazadan nomni olamiz
    bot_url = f"https://t.me/{Bot_url.lstrip('@')}"
    rec = db.get_movie(code) or {}
    s_name = html.escape(rec.get("name", "Kino"))
    channel_url = f"https://t.me/{PREVIEW_CHANNEL_ID.lstrip('@')}"
    s_code = html.escape(code)
    return (
        f"🎬: \"{s_name}\" botimizga to'liq holda joylandi❗\n"
        "➖➖➖➖➖➖➖➖➖➖\n"
        "• Filmni yuklab olish uchun botga kino kodini yuboring\n\n"
        f"• 🔢 Kino kodi: <code>{s_code}</code>\n\n"
        "📥 Kino kodini bu yerga  yuboring: 👇\n"
        f"🔹Bot: <a href=\"{bot_url}\">CinemadiaUz bot</a>"
    )

# ====== MOVIE INTERACTIVE (likes/ratings) ======
def _avg_rating(rec: Dict[str, Any]) -> float:
    stats = (rec or {}).get("stats", {})
    ratings = stats.get("ratings", {})
    s, c = ratings.get("sum", 0), ratings.get("count", 0)
    # Butun songa yaxlitlab ko'rsatamiz
    return int(round((s / c))) if c else 0

def _user_rating(rec: Dict[str, Any], uid: int) -> int:
    stats = (rec or {}).get("stats", {})
    ratings = stats.get("ratings", {})
    users = ratings.get("users", {})
    return int(users.get(str(uid), 0))

def build_stats_text(code: str, uid: int) -> str:
    rec = db.get_movie(code) or {}
    stats = rec.get("stats", {})
    views = stats.get("views", 0)
    avg = _avg_rating(rec)
    ur = _user_rating(rec, uid)
    stars = "".join("⭐" for _ in range(int(round(avg)))) or "-"
    my = f"Sizning baho: {ur}/5" if ur else "Baholanmagan"
    return (
        "📊 Statistika\n"
        f"👁️ Ko'rishlar: {views}\n"
        f"⭐ O'rtacha: {avg} {stars}\n"
        f"👤 {my}"
    )

def build_stats_kb(code: str, uid: int) -> InlineKeyboardMarkup:
    rec = db.get_movie(code) or {}
    ur = _user_rating(rec, uid)
    rate_row = []
    for n in range(1, 6):
        text = f"{('✅' if ur==n else '')}⭐{n}"
        rate_row.append(InlineKeyboardButton(text=text, callback_data=f"rate:{code}:{n}"))
    share_btn = InlineKeyboardButton(text="Ulashish 🔗", callback_data=f"share:{code}")
    favs = set(db.get_favorites(uid) or [])
    fav_on = code in favs
    fav_btn = InlineKeyboardButton(text=("💖 Sevimli" if fav_on else "🤍 Sevimlilar"), callback_data=f"fav:{code}")
    # Like va Yangilash tugmalari olib tashlandi
    rows = [rate_row, [fav_btn, share_btn]]
    return InlineKeyboardMarkup(inline_keyboard=rows)

# Yagona caption yaratish: kanaldagi asosiy tafsilotlar + pastdagi statistika
def build_combined_caption(rec: Dict[str, Any], code: str, uid: int) -> str:
    name = rec.get("name", "Kino")
    year = rec.get("year", "-")
    genre = rec.get("genre", "-")
    duration = rec.get("duration", "-")
    country = rec.get("country", "-")
    imdb = rec.get("imdb", "-")
    quality = rec.get("quality", "-")
    language = rec.get("language", "-")

    top = full_caption(
        name=name, year=year, genre=genre, duration=duration, code=code,
        country=country, imdb=imdb, quality=quality, language=language
    ).rstrip()

    # Statistikada endi kod, nom va like ko'rsatilmaydi
    stats = rec.get("stats", {})
    views = stats.get("views", 0)
    avg = _avg_rating(rec)
    ur = _user_rating(rec, uid)
    stars = "".join("⭐" for _ in range(int(round(avg)))) or "-"

    bottom = (
        f"\n\n📊 Statistika\n"
        f"👁️ Ko'rishlar: {views}\n"
        f"⭐ O'rtacha: {avg} {stars}\n"
        f"👤 Sizning baho: {ur if ur else 0}/5"
    )
    return top + bottom

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

class IsCode(BaseFilter):
    """Foydalanuvchi xabari kino kodi ko'rinishida ekanini tekshiradi (faqat 2-3 xonali raqam)."""
    async def __call__(self, m: types.Message) -> bool:
        t = (m.text or "").strip()
        return t.isdigit() and 2 <= len(t) <= 3

# ====== STATES ======
class Reg(StatesGroup):
    name = State()
    contact = State()  # saqlab qo'yamiz, lekin ishlatmaymiz

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
    # Deep-link payload: /start <code>
    payload_code = None
    try:
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) == 2:
            payload_code = parts[1].strip().upper()
    except Exception:
        payload_code = None
    if payload_code:
        await state.update_data(start_code=payload_code)
    u = db.get_user(m.from_user.id)
    if u:
        if u.get("is_admin"):
            await m.answer("Salom Admin! Kanalga kino joylashingiz mumkin!", reply_markup=KB.admin())
        else:
            # Agar payload bilan kelgan bo'lsa va obuna bo'lsa, darhol kinoni yuboramiz
            data = await state.get_data()
            code = data.get("start_code")
            if code and await is_subscribed_to_preview(m.from_user.id):
                rec = db.get_movie(code)
                if rec:
                    try:
                        db.inc_view(code)
                        msg_id = rec.get("full_message_id")
                        if not msg_id:
                            raise ValueError("full_message_id missing")
                        sent = await bot.copy_message(chat_id=m.chat.id, from_chat_id=FULL_CHANNEL_ID, message_id=msg_id)
                        combined = build_combined_caption(rec, code, m.from_user.id)
                        try:
                            await bot.edit_message_caption(chat_id=m.chat.id, message_id=sent.message_id, caption=combined, reply_markup=build_stats_kb(code, m.from_user.id))
                        except TelegramBadRequest:
                            await m.answer(build_stats_text(code, m.from_user.id), reply_markup=build_stats_kb(code, m.from_user.id))
                        await state.update_data(start_code=None)
                        return
                    except Exception:
                        pass
            # Oddiy start javobi
            greet = (
                "Assalomu alaykum!\n\n"
                "Bu bot orqali kinolarni olasiz.\n"
                "• Kino kodi bo'lsa — ‘🎟 Kod yuborish’ tugmasini bosing va kodni jo'nating.\n"
                "• Do'stlarga ulashish — film ostidagi ‘Ulashish 🔗’ tugmasidan foydalaning (faqat bot havolasi yuboriladi).\n"
            )
            await m.answer(greet, reply_markup=KB.user())
            if not await is_subscribed_to_preview(m.from_user.id):
                await m.answer("Iltimos, avval quyidagi kanalga obuna bo'ling:", reply_markup=subscribe_kb())
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
    # Telefon so'ralmaydi; adminlik ADMIN_IDS orqali tekshiriladi
    is_admin = db.is_admin_id(m.from_user.id)
    data = await state.get_data()
    pending_code = data.get("start_code")
    db.upsert_user(m.from_user.id, name=name, phone="", is_admin=is_admin)
    await state.clear()
    if is_admin:
        await m.answer("Admin sifatida ro'yxatdan o'tdingiz. Endi kino joylashni boshlang yoki menyuni tanlang.", reply_markup=KB.admin())
    else:
        # Agar deep-link kodi bo'lsa va obuna bo'lsa, avtomatik kino yuboramiz
        if pending_code and await is_subscribed_to_preview(m.from_user.id):
            rec = db.get_movie(pending_code)
            if rec:
                try:
                    db.inc_view(pending_code)
                    msg_id = rec.get("full_message_id")
                    if not msg_id:
                        raise ValueError("full_message_id missing")
                    sent = await bot.copy_message(chat_id=m.chat.id, from_chat_id=FULL_CHANNEL_ID, message_id=msg_id)
                    combined = build_combined_caption(rec, pending_code, m.from_user.id)
                    try:
                        await bot.edit_message_caption(chat_id=m.chat.id, message_id=sent.message_id, caption=combined, reply_markup=build_stats_kb(pending_code, m.from_user.id))
                    except TelegramBadRequest:
                        await m.answer(build_stats_text(pending_code, m.from_user.id), reply_markup=build_stats_kb(pending_code, m.from_user.id))
                    return
                except Exception:
                    pass
        # Aks holda oddiy oqim
        await m.answer("Ro'yxatdan o'tdingiz! Kod yuboring.", reply_markup=KB.remove())

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

@dp.message(IsAdmin(), F.text == "👥 Botdagi azolar")
async def admin_bot_members(m: types.Message):
    total = len(db.users)
    admins = sum(1 for u in db.users.values() if u.get("is_admin"))
    users_cnt = max(total - admins, 0)
    text = (
        f"👥 Bot foydalanuvchilari: {total}\n"
        f"🔐 Administratorlar: {admins}\n"
        f"👤 Oddiy foydalanuvchilar: {users_cnt}"
    )
    await m.answer(text, reply_markup=KB.admin())

@dp.message(IsAdmin(), F.text == "📣 Kanaldagi azolar")
async def admin_channel_members(m: types.Message):
    preview_count = None
    full_count = None
    errs = []
    try:
        preview_count = await bot.get_chat_member_count(PREVIEW_CHANNEL_ID)
    except Exception as e:
        errs.append(f"Preview: {e}")
    try:
        full_count = await bot.get_chat_member_count(FULL_CHANNEL_ID)
    except Exception as e:
        errs.append(f"Full: {e}")
    lines = ["📣 Kanal a'zolari:"]
    if preview_count is not None:
        lines.append(f"• PREVIEW {PREVIEW_CHANNEL_ID}: {preview_count}")
    else:
        lines.append(f"• PREVIEW {PREVIEW_CHANNEL_ID}: aniqlanmadi")
    if full_count is not None:
        lines.append(f"• FULL {FULL_CHANNEL_ID}: {full_count}")
    else:
        lines.append(f"• FULL {FULL_CHANNEL_ID}: aniqlanmadi")
    if errs:
        lines.append("ℹ️ Botni kanallarga admin qiling va to'g'ri ID/username kiriting.")
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

    # Bazaga to'liq ma'lumotlarni saqlaymiz (statistika maydonlari DB.add_movie ichida setdefault qilinadi)
    db.add_movie(code, {
        "name": name,
        "year": data.get("year","-"),
        "genre": data.get("genre","-"),
        "country": data.get("country","-"),
        "imdb": data.get("imdb","-"),
        "quality": data.get("quality","-"),
        "language": data.get("language","-"),
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
    # Asosiy PREVIEW kanal uchun alohida shablon
    cap_prev = preview_channel_caption(code)
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
    cap_prev = preview_channel_caption(code)
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
    cap_prev = preview_channel_caption(code)
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
    cap_prev = preview_channel_caption(code)
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
    # Asosiy PREVIEW kanal uchun alohida shablon
    cap_prev = preview_channel_caption(code)
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
@dp.message(IsCode())
async def user_by_code(m: types.Message):
    u = db.get_user(m.from_user.id)
    if not u:
        await m.answer("Avval /start orqali ro'yxatdan o'ting.")
        return
    if u.get("is_admin"):
        return  # admin uchun kod handler ishlatmaymiz
    # Obuna tekshiruvi
    if not await is_subscribed_to_preview(m.from_user.id):
        await m.answer("Botdan foydalanish uchun kanalga obuna bo'ling:", reply_markup=subscribe_kb())
        return
    code = (m.text or "").strip().upper()
    rec = db.get_movie(code)
    if not rec:
        await m.answer("Bunday kod topilmadi!")
        return
    try:
        # Ko'rishlar sonini oshiramiz
        db.inc_view(code)
        msg_id = rec.get("full_message_id")
        if not msg_id:
            await m.answer("Afsus, ushbu kino fayli hozircha mavjud emas.")
            return
        # Avval kanal xabarini foydalanuvchiga ko'chiramiz
        sent = await bot.copy_message(chat_id=m.chat.id, from_chat_id=FULL_CHANNEL_ID, message_id=msg_id, protect_content=True)
        # So'ng captionni bitta birlashtirilgan ko'rinishga o'zgartiramiz
        combined = build_combined_caption(rec, code, m.from_user.id)
        try:
            await bot.edit_message_caption(chat_id=m.chat.id, message_id=sent.message_id, caption=combined, reply_markup=build_stats_kb(code, m.from_user.id))
        except TelegramBadRequest:
            # Agar captionni tahrirlab bo'lmasa, alohida statistika xabarini yuboramiz (fallback)
            await m.answer(build_stats_text(code, m.from_user.id), reply_markup=build_stats_kb(code, m.from_user.id))
    except Exception as e:
        logging.error(f"copy_message error: {e}")
        await m.answer("Hozircha yuborib bo'lmadi. Keyinroq urinib ko'ring.")

# ====== CALLBACK: SUBSCRIPTION RE-CHECK ======
@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: types.CallbackQuery):
    user_id = call.from_user.id
    if await is_subscribed_to_preview(user_id):
        # Agar start_code bo'lsa, avtomatik kinoni yuborishga urinamiz
        key = StorageKey(bot_id=call.bot.id, chat_id=call.message.chat.id, user_id=user_id)
        data = await dp.storage.get_data(key)
        start_code = (data or {}).get("start_code")
        if start_code:
            rec = db.get_movie(start_code)
            if rec:
                try:
                    db.inc_view(start_code)
                    msg_id = rec.get("full_message_id")
                    if not msg_id:
                        raise ValueError("full_message_id missing")
                    sent = await bot.copy_message(chat_id=call.message.chat.id, from_chat_id=FULL_CHANNEL_ID, message_id=msg_id)
                    combined = build_combined_caption(rec, start_code, user_id)
                    try:
                        await bot.edit_message_caption(chat_id=call.message.chat.id, message_id=sent.message_id, caption=combined, reply_markup=build_stats_kb(start_code, user_id))
                    except TelegramBadRequest:
                        await call.message.answer(build_stats_text(start_code, user_id), reply_markup=build_stats_kb(start_code, user_id))
                    # start_code ni tozalaymiz
                    await dp.storage.update_data(key, {"start_code": None})
                    await call.answer()
                    return
                except Exception:
                    pass
        await call.message.answer("✅ Obuna tasdiqlandi! Endi kodni yuborishingiz mumkin.")
    else:
        await call.message.answer("Hali obuna bo'lmadingiz. Iltimos, kanalga obuna bo'ling.", reply_markup=subscribe_kb())
    await call.answer()

# ====== CALLBACK: LIKE / RATE / REFRESH ======
@dp.callback_query(F.data.startswith("like:"))
async def cb_like(call: types.CallbackQuery):
    # Like funksiyasi o'chirilgan. Eski xabarlardagi tugma bosilsa —
    # faqat markupni yangilab, ogohlantiramiz.
    try:
        _, code = call.data.split(":", 1)
    except Exception:
        await call.answer()
        return
    try:
        await call.message.edit_reply_markup(reply_markup=build_stats_kb(code, call.from_user.id))
    except TelegramBadRequest:
        pass
    await call.answer("Like funksiyasi o'chirilgan", show_alert=True)

async def _update_stats_message(call: types.CallbackQuery, code: str):
    """Statistika ko'rinishini yangilaydi: matnli xabar bo'lsa edit_text,
    media bo'lsa edit_caption ishlatiladi. 'message is not modified' xatosini e'tiborsiz qoldiradi."""
    rec = db.get_movie(code) or {}
    uid = call.from_user.id
    kb = build_stats_kb(code, uid)
    # Agar bu xabar captionli media bo'lsa — birlashtirilgan captionni yangilaymiz
    if call.message.caption is not None:
        new_caption = build_combined_caption(rec, code, uid)
        cur_caption = call.message.caption or ""
        try:
            if new_caption.strip() != cur_caption.strip():
                await call.bot.edit_message_caption(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    caption=new_caption,
                    reply_markup=kb,
                )
            else:
                # Matn o'zgarmagan — hech bo'lmasa markupni yangilaymiz
                await call.message.edit_reply_markup(reply_markup=kb)
        except TelegramBadRequest as e:
            logging.warning(f"edit_caption failed for {code}: {e}")
            # caption tahrirlanmasa, faqat markupni yangilaymiz
            try:
                await call.message.edit_reply_markup(reply_markup=kb)
            except TelegramBadRequest as e2:
                logging.warning(f"edit_reply_markup (caption case) failed for {code}: {e2}")
    else:
        # Oddiy matnli xabar — statistik matnni yangilaymiz
        txt = build_stats_text(code, uid)
        cur_text = call.message.text or ""
        try:
            if txt.strip() != cur_text.strip():
                await call.message.edit_text(txt)
                await call.message.edit_reply_markup(reply_markup=kb)
            else:
                await call.message.edit_reply_markup(reply_markup=kb)
        except TelegramBadRequest as e:
            logging.warning(f"edit_text failed for {code}: {e}")
            # Matnli emas yoki o'zgarmagan — faqat markupni yangilaymiz
            try:
                await call.message.edit_reply_markup(reply_markup=kb)
            except TelegramBadRequest as e2:
                logging.warning(f"edit_reply_markup (text case) failed for {code}: {e2}")

@dp.callback_query(F.data.startswith("rate:"))
async def cb_rate(call: types.CallbackQuery):
    try:
        _, code, val = call.data.split(":", 2)
        rating = int(val)
    except Exception:
        await call.answer("Xato format", show_alert=False)
        return
    db.rate_movie(code, call.from_user.id, rating)
    try:
        await _update_stats_message(call, code)
    finally:
        # callbackni yopamiz, aks holda foydalanuvchi "Loading"ni ko'radi
        await call.answer("Baholandi ✅", show_alert=False)

@dp.callback_query(F.data.startswith("refresh:"))
async def cb_refresh(call: types.CallbackQuery):
    try:
        _, code = call.data.split(":", 1)
    except Exception:
        await call.answer("Xato format", show_alert=False)
        return
    await _update_stats_message(call, code)
    await call.answer("Yangilandi", show_alert=False)

@dp.callback_query(F.data.startswith("share:"))
async def cb_share(call: types.CallbackQuery):
    try:
        _, code = call.data.split(":", 1)
    except Exception:
        await call.answer()
        return
    bot_username = (Bot_url or "").lstrip("@")
    if not bot_username:
        await call.answer("Bot URL sozlanmagan", show_alert=True)
        return
    rec = db.get_movie(code) or {}
    name = rec.get("name", "Kino")
    url = f"https://t.me/{bot_username}?start={code}"
    # Chiroyli bitta satrli link: faqat matn ichiga qo'yilgan havola
    txt = f"<a href='{html.escape(url)}'>🎬 {html.escape(name)} — kod {html.escape(code)}</a>"
    await call.message.answer(txt, disable_web_page_preview=True)
    await call.answer()

@dp.callback_query(F.data.startswith("fav:"))
async def cb_fav(call: types.CallbackQuery):
    try:
        _, code = call.data.split(":", 1)
    except Exception:
        await call.answer()
        return
    u = db.get_user(call.from_user.id)
    if not u:
        await call.answer("/start orqali ro'yxatdan o'ting", show_alert=True)
        return
    added = db.toggle_favorite(call.from_user.id, code)
    # Keyboardni yangilaymiz
    kb = build_stats_kb(code, call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass
    await call.answer("Sevimlilarga qo'shildi" if added else "Sevimlilardan olib tashlandi", show_alert=False)

# ====== SIMPLE USER MENUS ======
@dp.message(F.text == "📚 Yordam")
async def msg_help(m: types.Message):
    txt = (
        "Yordam:\n"
        "• Kino olish: ‘🎟 Kod yuborish’ni bosing va kodni kiriting.\n"
        "• Ulashish: film ostidagi ‘Ulashish 🔗’ tugmasi faqat bot havolasini beradi.\n"
        "• Obuna shart: kanalda obuna bo'lmasangiz kino berilmaydi."
    )
    await m.answer(txt)

@dp.message(F.text == "🔔 Obuna tekshirish")
async def msg_sub_check(m: types.Message):
    if await is_subscribed_to_preview(m.from_user.id):
        await m.answer("✅ Obuna bor")
    else:
        await m.answer("Kanalga obuna bo'ling:", reply_markup=subscribe_kb())

@dp.message(F.text == "🎟 Kod yuborish")
async def msg_send_code(m: types.Message):
    await m.answer("Kod raqamini yuboring (masalan, 12 yoki 345)")

@dp.message(F.text == "💖 Sevimlilar")
async def msg_favorites(m: types.Message):
    u = db.get_user(m.from_user.id)
    if not u:
        await m.answer("Avval /start orqali ro'yxatdan o'ting.")
        return
    favs = db.get_favorites(m.from_user.id)
    if not favs:
        await m.answer("Sevimlilar bo'sh.")
        return
    bot_username = (Bot_url or "").lstrip("@")
    lines = ["Sevimlilar:"]
    for code in favs[:50]:
        rec = db.get_movie(code) or {}
        name = rec.get("name", code)
        if bot_username:
            url = f"https://t.me/{bot_username}?start={code}"
            title = f"<a href='{html.escape(url)}'>🎬 {html.escape(name)}</a>"
        else:
            title = f"🎬 {html.escape(name)}"
        lines.append(f"• {title} — kod {html.escape(code)}")
    await m.answer("\n".join(lines), disable_web_page_preview=True)

@dp.message(F.text == "🔍 Random")
async def msg_random(m: types.Message):
    # Obuna shart
    if not await is_subscribed_to_preview(m.from_user.id):
        await m.answer("Kanalga obuna bo'ling:", reply_markup=subscribe_kb())
        return
    if not db.movies:
        await m.answer("Hozircha bazada kinolar yo'q.")
        return
    # Faqat nusxa olish mumkin bo'lgan (full yoki preview) va 'broken' bo'lmagan kinolardan tanlaymiz
    candidates = [
        code for code, rec in db.movies.items()
        if (rec.get("full_message_id") or rec.get("preview_message_id")) and not rec.get("broken")
    ]
    if not candidates:
        await m.answer("Hozircha random uchun tayyor kino yo'q.")
        return
    # Foydalanuvchi tarixidan foydalanib takrorlanmasin
    seen_list = db.get_random_history(m.from_user.id)
    seen = set(seen_list)
    base_pool = [c for c in candidates if c not in seen] or candidates
    # Hozirgina chiqqan filmni qayta bermaslikka harakat qilamiz
    if len(base_pool) > 1 and seen_list and seen_list[-1] in base_pool:
        base_pool = [c for c in base_pool if c != seen_list[-1]]

    import random as _r
    pool = base_pool[:]
    _r.shuffle(pool)

    # Bir bosishda bir nechta variantni sinab ko'ramiz
    for code in pool:
        rec = db.get_movie(code)
        if not rec:
            continue
        full_id = rec.get("full_message_id")
        prev_id = rec.get("preview_message_id")
        # Urinishlar ro'yxati: (channel, message_id, label)
        attempts = []
        if full_id:
            attempts.append((FULL_CHANNEL_ID, full_id, "FULL"))
        if prev_id:
            attempts.append((PREVIEW_CHANNEL_ID, prev_id, "PREVIEW"))
        # Cross-try: ba'zan IDlar boshqa kanalnikiga mos keladi
        if prev_id and FULL_CHANNEL_ID != PREVIEW_CHANNEL_ID:
            attempts.append((FULL_CHANNEL_ID, prev_id, "X-PREVIEW@FULL"))
        if full_id and FULL_CHANNEL_ID != PREVIEW_CHANNEL_ID:
            attempts.append((PREVIEW_CHANNEL_ID, full_id, "X-FULL@PREVIEW"))

        success = False
        for chan, mid, label in attempts:
            try:
                db.inc_view(code)
                logging.info(f"random: trying {label} copy code={code} msg_id={mid} chan={chan}")
                await bot.copy_message(chat_id=m.chat.id, from_chat_id=chan, message_id=mid, protect_content=True)
                await m.answer(build_stats_text(code, m.from_user.id), reply_markup=build_stats_kb(code, m.from_user.id))
                db.push_random_history(m.from_user.id, code)
                success = True
                break
            except Exception as e:
                logging.error(f"random copy_message attempt failed ({label}): {e}")
                continue

        if success:
            return
        # Hamma urinishlar ham muvaffaqiyatsiz bo'lsa — broken deb belgilaymiz
        logging.warning(f"random: marking code={code} as broken (all attempts failed)")
        db.mark_broken(code)
        continue

    # Agar hammasi muvaffaqiyatsiz bo'lsa, xabar beramiz
    await m.answer("Hozircha random yuborib bo'lmadi. Keyinroq urinib ko'ring.")

@dp.message(F.text == "⭐ Top")
async def msg_top(m: types.Message):
    if not db.movies:
        await m.answer("Kino topilmadi.")
        return
    bot_username = (Bot_url or "").lstrip("@")
    items = []
    for code, rec in db.movies.items():
        stats = rec.get("stats", {})
        likes = stats.get("likes", {}).get("count", 0)
        views = stats.get("views", 0)
        # avg rating
        avg = _avg_rating(rec)
        name = rec.get("name", code)
        url = f"https://t.me/{bot_username}?start={code}" if bot_username else None
        items.append((avg, likes, views, name, code, url))
    # Saralash: avg desc, likes desc, views desc
    items.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    if not items:
        await m.answer("Kino topilmadi.")
        return
    lines = ["Top kinolar:"]
    for i, (_avg, _likes, _views, name, code, url) in enumerate(items[:10], start=1):
        if url:
            title = f"<a href='{html.escape(url)}'>🎬 {html.escape(name)}</a>"
        else:
            title = f"🎬 {html.escape(name)}"
        lines.append(f"{i}. {title} — ⭐ {_avg} | ❤️ {_likes} | 👁️ {_views}")
    await m.answer("\n".join(lines), disable_web_page_preview=True)

# ====== RUN ======
async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
