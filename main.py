import os
import asyncio
import aiosqlite
import tempfile
import shutil
import requests
from dataclasses import dataclass
from contextlib import suppress
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from yt_dlp import YoutubeDL

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# İstəyə bağlı: Öz Heroku proxy API-nin URL-i (məs: https://sənin-proxy.herokuapp.com)
PROXY_API_URL = os.getenv("PROXY_API_URL", "").strip()

# Bir neçə admin üçün (vergüllə ayrılmış ID-lər, məsələn "123456789,987654321")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

if not BOT_TOKEN or not CHANNEL_ID or not ADMIN_IDS:
    raise SystemExit("BOT_TOKEN, CHANNEL_ID və ADMIN_IDS (vergüllə ayrılmış) dəyişənlərini qurun.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
DB_PATH = "submissions.db"

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    query TEXT NOT NULL,
    file_id TEXT NOT NULL,
    title TEXT,
    artist TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);
"""

@dataclass
class Submission:
    id: int
    user_id: int
    username: str | None
    query: str
    file_id: str
    title: str | None
    artist: str | None
    status: str

def admin_kb(sub_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Qəbul", callback_data=f"approve:{sub_id}"),
        InlineKeyboardButton(text="❌ Rədd", callback_data=f"reject:{sub_id}")
    ]])

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

# -----------------------------------------------------------
# MAHNI YÜKLƏMƏ — Proxy → Piped/Invidious → YouTube axtarış (filtr + fallback)
# -----------------------------------------------------------
def download_track(query: str):
    """
    1) PROXY_API_URL varsa: <proxy>/api/search?query=...
    2) Piped/Invidious instansları (yalnız JSON cavablıları qəbul edir)
    3) Son çarə: yt-dlp 'ytsearch10' — nəticələri filtrlə (15s–12dq, shorts deyil) və bir-bir yoxla
    Geri: (mp3_path, title, artist, tmpdir)
    """
    tmpdir = tempfile.mkdtemp(prefix="track_")
    outtmpl = os.path.join(tmpdir, "%(title).200B.%(ext)s")

    def find_mp3_path() -> str:
        for name in os.listdir(tmpdir):
            if name.lower().endswith(".mp3"):
                return os.path.join(tmpdir, name)
        return ""

    def build_cookiefile() -> str | None:
        # Termux-da yaratdığın cookies.b64 dəyərini Heroku-da Config Vars → COOKIES_B64 kimi qoysan, buradan oxunacaq
        b64 = os.getenv("COOKIES_B64", "").strip()
        if not b64:
            return None
        p = os.path.join(tmpdir, "cookies.txt")
        try:
            import base64
            with open(p, "wb") as f:
                f.write(base64.b64decode(b64))
            return p
        except Exception as e:
            print(f"[cookies decode error] {e}")
            return None

    def ytdlp_try(url: str, title_fallback="Naməlum Mahnı", author_fallback="Naməlum"):
        cookies_path = build_cookiefile()
        common_opts = {
            "outtmpl": outtmpl,
            "quiet": True,
            "noplaylist": True,
            "geo_bypass": True,
            "nocheckcertificate": True,
            "allow_unplayable_formats": False,
            "http_headers": {"User-Agent": "Mozilla/5.0"},
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "FFmpegMetadata"},
            ],
        }
        if cookies_path:
            common_opts["cookiefile"] = cookies_path

        probe_opts = dict(common_opts)
        probe_opts["skip_download"] = True
        probe_opts["format"] = "bestaudio/best"

        with YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info or ("shorts/" in (info.get("webpage_url") or "")):
            raise RuntimeError("Uyğun olmayan nəticə (shorts və ya səhifə)")

        fmts = info.get("formats") or []
        audio_only = [f for f in fmts if (f.get("vcodec") in (None, "none")) and (f.get("acodec") and f.get("acodec") != "none")]

        if not audio_only:
            audio_like = [f for f in fmts if f.get("acodec") and f.get("acodec") != "none"]
            if not audio_like:
                raise RuntimeError("Audio format tapılmadı")
            best = max(audio_like, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0))
        else:
            best = max(audio_only, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0))

        forced_format_id = best.get("format_id")
        if not forced_format_id:
            raise RuntimeError("Format ID tapılmadı")

        dl_opts = dict(common_opts)
        dl_opts["format"] = f"{forced_format_id}"

        with YoutubeDL(dl_opts) as ydl:
            ydl.download([url])

        for name in os.listdir(tmpdir):
            if name.lower().endswith(".mp3"):
                mp3_path = os.path.join(tmpdir, name)
                break
        else:
            raise RuntimeError("MP3 faylı yaradılmadı")

        return mp3_path, title_fallback, author_fallback, tmpdir

    # 1) PROXY varsa
    if PROXY_API_URL:
        try:
            api = PROXY_API_URL.rstrip("/") + "/api/search"
            url = f"{api}?query={requests.utils.quote(query)}"
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                j = r.json()
                if "url" in j:
                    print(f"[PROXY OK] {j.get('source','')}")
                    return ytdlp_try(j["url"], j.get("title","Naməlum Mahnı"), j.get("author","Naməlum"))
            else:
                print(f"[PROXY FAIL] status={r.status_code}")
        except Exception as e:
            print(f"[PROXY ERROR] {e}")

    # 2) Piped / Invidious instansları
    SOURCES = [
        ("piped", "https://piped.video"),
        ("piped", "https://pipedapi.kavin.rocks"),
        ("piped", "https://piped.mha.fi"),
        ("invidious", "https://iv.ggtyler.dev"),
        ("invidious", "https://yewtu.be"),
    ]
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    for typ, base in SOURCES:
        try:
            if typ == "invidious":
                req = f"{base}/api/v1/search?q={requests.utils.quote(query)}"
            else:
                req = f"{base}/api/v1/search?q={requests.utils.quote(query)}&filter=music"

            resp = requests.get(req, timeout=10, headers=headers)
            ct = resp.headers.get("content-type", "")
            if not resp.ok or "application/json" not in ct.lower():
                print(f"[{base}] JSON deyil və/ya status {resp.status_code}")
                continue

            data = resp.json()
            item = data[0] if isinstance(data, list) and data else (data if data else None)
            if not item:
                continue

            title = item.get("title") or "Naməlum Mahnı"
            artist = item.get("uploader") or item.get("author") or "Naməlum"
            video_url = (
                f"https://youtube.com/watch?v={item.get('videoId')}"
                if "videoId" in item else f"{base}{item.get('url')}"
            )
            if "shorts/" in video_url:  # Shorts-ları atla
                continue
            return ytdlp_try(video_url, title, artist)
        except Exception as e:
            print(f"[{base}] xətası: {e}")
            continue

    # 3) Son çarə: birbaşa YouTube axtarışı — nəticələri filtrlə və sırayla yoxla
    try:
        cookies_path = build_cookiefile()
        search_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "default_search": "ytsearch10",
            "geo_bypass": True,
            "nocheckcertificate": True,
            "http_headers": {"User-Agent": "Mozilla/5.0"},
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "FFmpegMetadata"},
            ],
        }
        if cookies_path:
            search_opts["cookiefile"] = cookies_path

        with YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(query, download=False)

        entries = []
        if info:
            if info.get("_type") == "playlist":
                entries = [e for e in info.get("entries", []) if e]
            else:
                entries = [info]

        # Filtr: 15s–12dq, URL-də shorts olmasın
        candidates = []
        for e in entries:
            dur = (e.get("duration") or 0)
            url = e.get("webpage_url") or e.get("url") or ""
            if 15 <= dur <= 12 * 60 and "shorts/" not in url:
                candidates.append(e)
        if not candidates:
            candidates = entries  # heç nə düşmədinsə, hamısını cəhd et

        last_err = None
        for e in candidates:
            try:
                url = e.get("webpage_url") or e.get("url")
                title = e.get("title") or "Naməlum Mahnı"
                artist = e.get("artist") or e.get("uploader") or "Naməlum"
                return ytdlp_try(url, title, artist)
            except Exception as ex:
                last_err = ex
                continue

        raise RuntimeError(last_err or "Uyğun format tapılmadı")
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Mahnı tapılmadı — bütün mənbələr uğursuz oldu: {e}")

# ----------------------- Handlers -----------------------

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("Salam! Mahnı adı yaz, mən onu tapım və adminlərə göndərim 🎧")

@dp.message(F.text & ~F.via_bot)
async def on_query(m: Message):
    query = m.text.strip()
    if not query:
        return await m.answer("Zəhmət olmasa mahnı adını yazın 🎵")
    wait = await m.answer("Axtarıram və yükləyirəm... ⏳")

    try:
        file_path, title, artist, tmpdir = await asyncio.to_thread(download_track, query)
    except Exception as e:
        print(f"[download_track error] {e}")
        await wait.edit_text("Tapmaq mümkün olmadı. ❌  (fərqli adla yenidən yoxla)")
        return

    caption = (
        f"🎶 Yeni tövsiyə\n"
        f"📌 Axtarış: {query}\n"
        f"🎵 Başlıq: {title or '—'}\n"
        f"👤 Göndərən: @{m.from_user.username or m.from_user.id}"
    )

    first_admin = ADMIN_IDS[0]
    try:
        msg = await bot.send_audio(first_admin, FSInputFile(file_path), caption=caption)
        file_id = msg.audio.file_id
    except Exception as e:
        print(f"[send_audio first_admin error] {e}")
        await wait.edit_text("Admin PM-ə göndərmək alınmadı ❌")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO submissions (user_id, username, query, file_id, title, artist) VALUES (?, ?, ?, ?, ?, ?)",
            (m.from_user.id, m.from_user.username, query, file_id, title, artist)
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        sub_id = (await cur.fetchone())[0]

    # bütün adminlərə PM
    for admin_id in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_audio(
                admin_id,
                file_id,
                caption=caption + f"\nID: #{sub_id}",
                title=title or query,
                performer=artist or "",
                reply_markup=admin_kb(sub_id)
            )

    await wait.edit_text("Tövsiyəniz adminlərə göndərildi ✅")

@dp.callback_query(F.from_user.id.in_(ADMIN_IDS), F.data.startswith(("approve:", "reject:")))
async def on_moderate(cb: CallbackQuery):
    action, sub_id_str = cb.data.split(":")
    sub_id = int(sub_id_str)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,))
        row = await cur.fetchone()
        if not row:
            return await cb.answer("Tapılmadı.", show_alert=True)
        sub = Submission(**row)

        if sub.status != "pending":
            return await cb.answer("Artıq emal olunub.", show_alert=True)

        if action == "approve":
            cap = f"{sub.title or sub.query}\n#MusicAzerbaycan #Tövsiyə"
            await bot.send_audio(CHANNEL_ID, sub.file_id, caption=cap)
            await db.execute("UPDATE submissions SET status='approved' WHERE id=?", (sub.id,))
            await db.commit()
            await cb.answer("✅ Qəbul edildi")
            with suppress(Exception):
                await cb.message.edit_caption(cb.message.caption + "\n✅ Qəbul olundu")
            with suppress(Exception):
                await bot.send_message(sub.user_id, "✅ Tövsiyən qəbul olundu və kanala paylaşıldı.")
        else:
            await db.execute("UPDATE submissions SET status='rejected' WHERE id=?", (sub.id,))
            await db.commit()
            await cb.answer("❌ Rədd edildi")
            with suppress(Exception):
                await cb.message.edit_caption(cb.message.caption + "\n❌ Rədd edildi")
            with suppress(Exception):
                await bot.send_message(sub.user_id, "❌ Təəssüf, tövsiyəniz rədd edildi.")

# ----------------------- Runner -----------------------

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())