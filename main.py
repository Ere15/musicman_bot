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

    old_source, title, original_query = await queue.get()
    track_id = None

    try:
        track_url_match = re.search(r'track/(\d+)', str(original_query))

        if track_url_match:
            track_id = track_url_match.group(1)
        elif hasattr(old_source, 'id'):
            track_id = old_source.id
        elif isinstance(old_source, (int, str)) and str(old_source).isdigit():
            track_id = old_source
        else:
            # Асинхронная обёртка для поиска
            loop = asyncio.get_event_loop()
            search_result = await loop.run_in_executor(
                None, lambda: ym_client.search(original_query, type_='track')
            )
            if search_result.tracks and search_result.tracks.results:
                track_id = search_result.tracks.results[0].id
            else:
                raise ValueError("Трек не найден")

        if not track_id:
            raise ValueError("Не удалось определить ID трека")

        # Асинхронно получаем свежую прямую ссылку
        def get_direct():
            track_obj = ym_client.tracks([track_id])
            info = track_obj[0].get_download_info(get_direct_links=True)
            best = max(info, key=lambda x: x.bitrate_in_kbps)
            return best.get_direct_link()

        direct_link = await asyncio.get_event_loop().run_in_executor(None, get_direct)

    except Exception as e:
        await ctx.send(f"❌ Не удалось воспроизвести трек: **{title}**")
        print(f"Ошибка обновления ссылки для '{title}': {e}")
        bot.loop.call_soon_threadsafe(lambda: bot.loop.create_task(play_next(ctx)))
        return

    headers = (
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
        "Referer: https://yandex.ru\r\n"
    )

    audio = discord.FFmpegOpusAudio(
        direct_link,
        before_options=(
            "-reconnect 1 "
            "-reconnect_streamed 1 "
            "-reconnect_delay_max 5 "
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


async def get_audio_source(query):
    """
    Возвращает (source_url, title, is_ok) для запроса.
    """
    yandex_pattern = r'(https?://)?(music\.yandex\.ru|yandex\.ru/music)'
    if re.match(yandex_pattern, query) and ym_client:
        try:
            track_match = re.search(r'/track/(\d+)', query)
            if track_match:
                track_id = int(track_match.group(1))

                # Асинхронно получаем инфу о треке
                def get_yandex():
                    track = ym_client.tracks([track_id])[0]
                    download_info = track.get_download_info(get_direct_links=True)
                    if download_info:
                        best = max(download_info, key=lambda x: x.bitrate_in_kbps)
                        direct_url = best.get_direct_link()
                        return direct_url, track.title
                    return None, None

                direct_url, title = await asyncio.get_event_loop().run_in_executor(None, get_yandex)
                if direct_url:
                    return direct_url, title, True

                # Fallback на yt-dlp
                return await get_audio_source_ytdlp(query)

        except Exception as e:
            print(f"Ошибка Яндекс API: {e}")
            return await get_audio_source_ytdlp(query)

    return await get_audio_source_ytdlp(query)


async def get_audio_source_ytdlp(query):
    """Универсальный загрузчик через yt-dlp. АСИНХРОННЫЙ."""
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

    source_url, title, is_ok = await get_audio_source(query)
    if not is_ok or source_url is None:
        return await ctx.send("❌ Не удалось найти аудио по запросу.")

    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    await queue.put((source_url, title, query))

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
            items.append(item[1])

    for item in temp_list:
        await queue.put(item)

    if items:
        msg = f"📋 Очередь ({len(temp_list)} треков):\n" + \
              "\n".join(f"{i+1}. {name}" for i, name in enumerate(items))
        if len(temp_list) > 5:
            msg += f"\n...и ещё {len(temp_list) - 5}"
        await ctx.send(msg)


bot.run(DISCORD_TOKEN)
