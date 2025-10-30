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

# İstəyə bağlı: Öz Heroku proxy API-nin URL-i (məs: https://music-proxy-az.herokuapp.com)
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


# ✅ Stabil versiya: 1) PROXY API varsa onu istifadə et; 2) olmazsa 4 alternativ mənbəyə düş
def download_track(query: str):
    """
    Cookies-siz musiqi axtarışı və yükləmə.
    1) PROXY_API_URL varsa: <proxy>/api/search?query=...
    2) Əks halda 4 mənbə: piped.video, pipedapi.kavin.rocks, piped.mha.fi, invidious.snopyta.org
    Geri: (mp3_path, title, artist, tmpdir)
    """
    tmpdir = tempfile.mkdtemp(prefix="track_")
    outtmpl = os.path.join(tmpdir, "audio.mp3")

    def ytdlp_download(video_url: str, title_fallback="Naməlum Mahnı", author_fallback="Naməlum"):
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "noplaylist": True,
            "geo_bypass": True,
            "source_address": "0.0.0.0",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "FFmpegMetadata"},
            ],
        }
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
        if not os.path.exists(outtmpl):
            raise RuntimeError("MP3 faylı yaradılmadı")
        return outtmpl, title_fallback, author_fallback

    # 1) PROXY API cəhdi
    if PROXY_API_URL:
        try:
            api = PROXY_API_URL.rstrip("/") + "/api/search"
            url = f"{api}?query={requests.utils.quote(query)}"
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                j = r.json()
                if "url" in j:
                    print(f"[PROXY OK] {j.get('source','')}")
                    return ytdlp_download(
                        j["url"],
                        j.get("title", "Naməlum Mahnı"),
                        j.get("author", "Naməlum")
                    )
            else:
                print(f"[PROXY FAIL] status={r.status_code}")
        except Exception as e:
            print(f"[PROXY ERROR] {e}")

    # 2) Fallback: açıq mənbələr
    SOURCES = [
        "https://piped.video",
        "https://pipedapi.kavin.rocks",
        "https://piped.mha.fi",
        "https://invidious.snopyta.org"
    ]

    video = None
    video_url = None
    title = "Naməlum Mahnı"
    author = "Naməlum"

    for base_url in SOURCES:
        try:
            if "invidious" in base_url:
                resp = requests.get(
                    f"{base_url}/api/v1/search?q={requests.utils.quote(query)}",
                    timeout=10, headers={"User-Agent": "Mozilla/5.0"}
                )
            else:
                resp = requests.get(
                    f"{base_url}/api/v1/search?q={requests.utils.quote(query)}&filter=music",
                    timeout=10, headers={"User-Agent": "Mozilla/5.0"}
                )
            if resp.status_code == 200 and resp.json():
                data = resp.json()
                video = data[0] if isinstance(data, list) else data
                title = video.get("title") or title
                author = video.get("uploader") or video.get("author") or author
                video_url = (
                    f"https://youtube.com/watch?v={video.get('videoId')}"
                    if "videoId" in video else f"{base_url}{video['url']}"
                )
                print(f"[FALLBACK OK] {video_url}")
                break
        except Exception as e:
            print(f"[{base_url}] xətası: {e}")
            continue

    if not video or not video_url:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("Mahnı tapılmadı — proxy və bütün mənbələr uğursuz oldu")

    # Yüklə
    try:
        return ytdlp_download(video_url, title, author)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Mahnı yüklənmədi: {e}")


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


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())