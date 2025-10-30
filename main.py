import os
import asyncio
import aiosqlite
import tempfile
import shutil
from dataclasses import dataclass
from contextlib import suppress
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from yt_dlp import YoutubeDL

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

if not BOT_TOKEN or not CHANNEL_ID or not ADMIN_CHAT_ID:
    raise SystemExit("BOT_TOKEN, CHANNEL_ID, ADMIN_CHAT_ID dəyişənlərini qurun.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
DB_PATH = "submissions.db"

CREATE_SQL = '''
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
'''

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
        InlineKeyboardButton(text='✅ Qəbul', callback_data=f'approve:{sub_id}'),
        InlineKeyboardButton(text='❌ Rədd', callback_data=f'reject:{sub_id}')
    ]])

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

def download_track(query: str):
    tmpdir = tempfile.mkdtemp(prefix='track_')
    outtmpl = os.path.join(tmpdir, '%(title).200B.%(ext)s')
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': outtmpl,
        'noplaylist': True,
        'quiet': True,
        'default_search': 'ytsearch1',
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
            {'key': 'FFmpegMetadata'}
        ],
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=True)
        if '_type' in info and info['_type'] == 'playlist' and info['entries']:
            info = info['entries'][0]
        title = info.get('title')
        artist = (info.get('artist') or info.get('uploader') or '')
        base = ydl.prepare_filename(info)
        mp3_path = os.path.splitext(base)[0] + '.mp3'
        return mp3_path, title, artist, tmpdir

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("Salam! Kanala mahnı tövsiyə etmək üçün mahnının adını yaz.")

@dp.message(F.text & ~F.via_bot)
async def on_query(m: Message):
    query = m.text.strip()
    if not query:
        return await m.answer("Zəhmət olmasa mahnı adını yazın.")
    wait = await m.answer("Axtarıram və yükləyirəm... ⏳")

    try:
        file_path, title, artist, tmpdir = await asyncio.to_thread(download_track, query)
    except Exception:
        with suppress(Exception):
            await wait.edit_text("Tapmaq mümkün olmadı. ❌")
        return

    caption = f"🎵 Tövsiyə: {query}\nBaşlıq: {title}\nSənətçi: {artist}\n👤 @{m.from_user.username or m.from_user.id}"
    try:
        msg = await bot.send_audio(ADMIN_CHAT_ID, audio=FSInputFile(file_path), caption=caption)
        file_id = msg.audio.file_id
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT INTO submissions (user_id, username, query, file_id, title, artist) VALUES (?, ?, ?, ?, ?, ?)', 
                         (m.from_user.id, m.from_user.username, query, file_id, title, artist))
        await db.commit()
        cur = await db.execute('SELECT last_insert_rowid()')
        sub_id = (await cur.fetchone())[0]

    await msg.edit_caption(msg.caption + f'\nID: #{sub_id}', reply_markup=admin_kb(sub_id))
    await wait.edit_text("Təşəkkürlər! Tövsiyəniz adminlərə göndərildi. ✅")

@dp.callback_query(F.message.chat.id == ADMIN_CHAT_ID, F.data.startswith(('approve:', 'reject:')))
async def on_moderate(cb: CallbackQuery):
    action, sub_id_str = cb.data.split(':')
    sub_id = int(sub_id_str)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT * FROM submissions WHERE id = ?', (sub_id,))
        row = await cur.fetchone()
        if not row:
            return await cb.answer('Tapılmadı.', show_alert=True)
        sub = Submission(**row)
        if sub.status != 'pending':
            return await cb.answer('Artıq emal olunub.', show_alert=True)
        if action == 'approve':
            cap = f"{sub.title or sub.query}\n#MusicAzerbaycan"
            await bot.send_audio(CHANNEL_ID, audio=sub.file_id, caption=cap, title=sub.title or sub.query, performer=sub.artist or '')
            await db.execute('UPDATE submissions SET status="approved" WHERE id=?', (sub.id,))
            await db.commit()
            with suppress(Exception):
                await bot.send_message(sub.user_id, "✅ Tövsiyən qəbul olundu və kanalda paylaşıldı.")
            await cb.message.edit_caption(cb.message.caption + "\n\n✅ Qəbul olundu və paylaşıldı.")
        else:
            await db.execute('UPDATE submissions SET status="rejected" WHERE id=?', (sub.id,))
            await db.commit()
            with suppress(Exception):
                await bot.send_message(sub.user_id, "❌ Təəssüf, rədd edildi.")
            await cb.message.edit_caption(cb.message.caption + "\n\n❌ Rədd edildi.")

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
