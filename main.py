import os
import contextlib
from typing import Tuple, Optional

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified

from youtubesearchpython import VideosSearch
from yt_dlp import YoutubeDL

# ==== ENV ====
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise SystemExit("API_ID, API_HASH və BOT_TOKEN dəyişənlərini Heroku Config Vars-da qurun.")

# ==== Pyrogram Client ====
app = Client("music_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Yükləmələr üçün qovluq
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

HELP_TEXT = (
    "__Mahnı adı qeyd edin...__\n\n"
    "Nümunə: ```/song Miro Təcili Yardım```"
)

def search_youtube(query: str) -> Optional[str]:
    """Sorğuya görə birinci video linkini qaytarır."""
    try:
        r = VideosSearch(query, limit=1).result()
        if r.get("result"):
            return r["result"][0]["link"]
    except Exception:
        pass
    return None

def download_mp3(video_url: str) -> Tuple[str, str, str, int]:
    """
    Verilən YouTube linkindən MP3 çıxarır.
    Qaytarır: (mp3_path, title, author, duration)
    """
    outtmpl = os.path.join(DOWNLOAD_DIR, "%(title).200B [%(id)s].%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
        # “Requested format is not available” üçün alternativ client
        "extractor_args": {"youtube": {"player_client": ["android", "tvhtml5"]}},
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
        ],
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        base = ydl.prepare_filename(info)
        mp3_path = os.path.splitext(base)[0] + ".mp3"

        if not os.path.exists(mp3_path):
            raise RuntimeError("MP3 faylı yaradılmadı")

        title = info.get("title") or "Naməlum Mahnı"
        author = info.get("artist") or info.get("uploader") or "Naməlum"
        duration = int(info.get("duration") or 0)
        return mp3_path, title, author, duration

@app.on_message(filters.command(["start"]))
async def start_handler(_, m: Message):
    await m.reply(
        "Salam! /song ilə mahnı adını yaz, mən tapıb MP3 göndərim 🎧\n\n" + HELP_TEXT,
        quote=True
    )

@app.on_message(filters.command(['song'], prefixes=['/', '!']) & (filters.group | filters.private))
async def song_handler(_, m: Message):
    if len(m.command) == 1:
        return await m.reply(HELP_TEXT, quote=True)

    query = m.text.split(None, 1)[1].strip()
    status = await m.reply("<b>🔍 Mahnı axtarılır...</b>", quote=True)

    url = search_youtube(query)
    if not url:
        return await status.edit("❌ Mahnı tapılmadı. Yenidən cəhd edin...")

    try:
        await status.edit("<b>⏬ Yüklənilir...</b>")
        mp3_path, title, author, duration = download_mp3(url)
    except Exception as e:
        with contextlib.suppress(MessageNotModified):
            await status.edit(f"❌ Xəta: {e}")
        return

    with contextlib.suppress(MessageNotModified):
        await status.edit("<b>📤 Göndərilir...</b>")

    try:
        await m.reply_audio(
            audio=mp3_path,
            duration=duration or None,
            performer=author,
            title=title,
            caption=f"<b>{title}</b>\n\n<b>Yüklədi:</b> @MusicAzerbaycan"
        )
    finally:
        with contextlib.suppress(Exception):
            os.remove(mp3_path)

    with contextlib.suppress(Exception):
        await status.delete()

if __name__ == "__main__":
    app.run()