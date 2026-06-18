import asyncio
import os
import re

import discord
import dotenv
import yt_dlp
from discord.ext import commands
from yandex_music import Client

# --- Настройки ---
dotenv.load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Инициализация клиента Яндекс.Музыки
ym_client = None
if YANDEX_TOKEN:
    ym_client = Client(YANDEX_TOKEN).init()

# Глобальные словари
queues = {}
current_tracks = {}


def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = asyncio.Queue()
    return queues[guild_id]


def get_cookies_string():
    """Возвращает куки сессии Яндекс.Музыки для передачи в FFmpeg."""
    if ym_client is None:
        return ""
    return "; ".join(f"{c.name}={c.value}" for c in ym_client.session.cookies)


async def get_yandex_direct_link(track_id):
    """Асинхронно получает прямую ссылку на трек по ID."""
    if ym_client is None:
        raise RuntimeError("Яндекс.Музыка не настроена")
    loop = asyncio.get_event_loop()

    def _sync():
        track = ym_client.tracks([track_id])[0]
        # get_direct_links=False, чтобы не вызывать автоматически get_direct_link()
        info = track.get_download_info(get_direct_links=False)
        best = max(info, key=lambda x: x.bitrate_in_kbps)
        # явно получаем прямую ссылку внутри потока
        return best.get_direct_link()

    return await loop.run_in_executor(None, _sync)


async def get_yandex_track_info(track_id):
    """Асинхронно получает прямую ссылку и название трека по ID."""
    if ym_client is None:
        raise RuntimeError("Яндекс.Музыка не настроена")
    loop = asyncio.get_event_loop()

    def _sync():
        track = ym_client.tracks([track_id])[0]
        info = track.get_download_info(get_direct_links=False)
        best = max(info, key=lambda x: x.bitrate_in_kbps)
        direct_url = best.get_direct_link()
        return direct_url, track.title

    return await loop.run_in_executor(None, _sync)


async def search_yandex_track(query):
    """Асинхронный поиск трека в Яндекс.Музыке по тексту."""
    if ym_client is None:
        raise RuntimeError("Яндекс.Музыка не настроена")
    loop = asyncio.get_event_loop()

    def _sync():
        result = ym_client.search(query, type_='track')
        if result.tracks and result.tracks.results:
            return result.tracks.results[0].id
        return None

    return await loop.run_in_executor(None, _sync)


async def play_next(ctx):
    """Воспроизводит следующий трек из очереди."""
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    vc = ctx.voice_client

    if vc is None or not vc.is_connected():
        return
    if queue.empty():
        current_tracks[guild_id] = None
        return

    data = await queue.get()
    title = data["title"]

    try:
        if data["type"] == "yandex":
            direct_link = await get_yandex_direct_link(data["track_id"])
        else:
            source_url, _, ok = await get_audio_source_ytdlp(data["query"])
            if not ok or source_url is None:
                raise ValueError("Не удалось получить аудио")
            direct_link = source_url
    except Exception as e:
        await ctx.send(f"❌ Не удалось воспроизвести трек: **{title}**")
        print(f"Ошибка воспроизведения: {e}")
        bot.loop.call_soon_threadsafe(lambda: bot.loop.create_task(play_next(ctx)))
        return

    # Формируем заголовки с куками для FFmpeg
    headers = (
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
        "Accept: */*\r\n"
        "Accept-Language: ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7\r\n"
        "Origin: https://music.yandex.ru\r\n"
        "Referer: https://music.yandex.ru/\r\n"
        + (f"Cookie: {cookie_str}\r\n" if (cookie_str := get_cookies_string()) else "")
    )

    audio = discord.FFmpegOpusAudio(
        direct_link,
        before_options=(
            "-reconnect 1 "
            "-reconnect_streamed 1 "
            "-reconnect_delay_max 10 "
            "-reconnect_max_retries 3 "
            f'-headers "{headers}"'
        ),
        options="-vn"
    )

    current_tracks[guild_id] = title

    def after_playing(error):
        if error:
            print(f"Ошибка воспроизведения: {error}")
        bot.loop.call_soon_threadsafe(
            lambda: bot.loop.create_task(play_next(ctx))
        )

    vc.play(audio, after=after_playing)
    await ctx.send(f"🎵 Сейчас играет: **{title}**")


async def get_audio_source_ytdlp(query):
    """Универсальный загрузчик через yt-dlp (асинхронный)."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
    }

    loop = asyncio.get_event_loop()

    def extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(query, download=False)

    try:
        info = await loop.run_in_executor(None, extract)
    except Exception as e:
        print(f"Ошибка yt-dlp: {e}")
        return None, None, False

    if not info or not isinstance(info, dict):
        return None, None, False

    if 'entries' in info:
        entries = info['entries']
        if not entries:
            return None, None, False
        info = entries[0]
        if not info or not isinstance(info, dict):
            return None, None, False

    title = info.get('title', 'Неизвестный трек')
    formats = info.get('formats', [])
    source_url = None

    if formats:
        audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
        if audio_formats:
            best_audio = max(audio_formats, key=lambda x: x.get('abr', 0) or 0)
            source_url = best_audio.get('url')
        if not source_url:
            source_url = info.get('url')
    else:
        source_url = info.get('url')

    if not source_url:
        return None, None, False

    return source_url, title, True


@bot.event
async def on_ready():
    print(f"Бот {bot.user} готов!")


@bot.command(name='play')
async def play(ctx, *, query):
    """Воспроизвести музыку по ссылке или поисковому запросу."""
    if not ctx.author.voice:
        return await ctx.send("Вы не в голосовом канале!")

    voice_channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    # Проверяем, является ли запрос ссылкой на Яндекс
    yandex_pattern = r'(https?://)?(music\.yandex\.ru|yandex\.ru/music)'
    is_yandex_query = bool(re.match(yandex_pattern, query))

    if is_yandex_query and ym_client:
        track_match = re.search(r'/track/(\d+)', query)
        if track_match:
            track_id = int(track_match.group(1))
            try:
                direct_url, title = await get_yandex_track_info(track_id)
                source_url, title, is_ok = direct_url, title, True
            except Exception as e:
                print(f"Ошибка Яндекс: {e}")
                return await ctx.send("❌ Не удалось получить трек с Яндекса.")
        else:
            return await ctx.send("❌ Некорректная ссылка на Яндекс.Музыку.")
    else:
        # Для обычных запросов или других платформ используем yt-dlp
        source_url, title, is_ok = await get_audio_source_ytdlp(query)
        if not is_ok:
            # Если не получилось через yt-dlp, попробуем поискать в Яндексе (если настроен)
            if ym_client:
                try:
                    track_id = await search_yandex_track(query)
                    if track_id:
                        direct_url, title = await get_yandex_track_info(track_id)
                        source_url, title, is_ok = direct_url, title, True
                        is_yandex_query = True
                    else:
                        return await ctx.send("❌ Ничего не найдено.")
                except Exception:
                    return await ctx.send("❌ Ничего не найдено.")
            else:
                return await ctx.send("❌ Не удалось найти аудио.")

    if not source_url:
        return await ctx.send("❌ Не удалось получить ссылку на аудио.")

    guild_id = ctx.guild.id
    queue = get_queue(guild_id)

    # Сохраняем в очередь с типом источника
    if is_yandex_query and ym_client and track_match:
        queue.put_nowait({
            "type": "yandex",
            "track_id": track_id,
            "title": title,
            "query": query
        })
    else:
        queue.put_nowait({
            "type": "other",
            "query": query,
            "title": title
        })

    vc = ctx.voice_client
    if not vc.is_playing():
        await play_next(ctx)
    else:
        await ctx.send(f"✅ Добавлено в очередь: **{title}**")


@bot.command(name='skip')
async def skip(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await ctx.send("⏭ Трек пропущен.")
    else:
        await ctx.send("Ничего не играет.")


@bot.command(name='pause')
async def pause(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸ Пауза")
    else:
        await ctx.send("Ничего не играет.")


@bot.command(name='resume')
async def resume(ctx):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶ Возобновлено")
    else:
        await ctx.send("Музыка не на паузе.")


@bot.command(name='stop')
async def stop(ctx):
    vc = ctx.voice_client
    if vc:
        guild_id = ctx.guild.id
        queue = get_queue(guild_id)
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        current_tracks[guild_id] = None
        if vc.is_playing():
            vc.stop()
        await vc.disconnect()
        await ctx.send("⏹ Остановлено и отключено.")
    else:
        await ctx.send("Бот не в голосовом канале.")


@bot.command(name='queue')
async def show_queue(ctx):
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)

    if queue.empty():
        await ctx.send("Очередь пуста.")
        return

    items = []
    temp_list = []

    while not queue.empty():
        item = await queue.get()
        temp_list.append(item)
        if len(items) < 5:
            items.append(item["title"])

    for item in temp_list:
        await queue.put(item)

    if items:
        msg = f"📋 Очередь ({len(temp_list)} треков):\n" + \
              "\n".join(f"{i+1}. {name}" for i, name in enumerate(items))
        if len(temp_list) > 5:
            msg += f"\n...и ещё {len(temp_list) - 5}"
        await ctx.send(msg)


bot.run(DISCORD_TOKEN)
