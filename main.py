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


# ---------------------------------------------------------------------------
# Парсинг ссылок Яндекс.Музыки
# ---------------------------------------------------------------------------

def parse_yandex_url(url: str):
    """
    Разбирает URL Яндекс.Музыки и возвращает тип + параметры.

    Поддерживаемые форматы:
      - /track/<id>
      - /album/<id>
      - /album/<album_id>/track/<track_id>
      - /users/<login>/playlists/<kind>

    Возвращает dict с ключом 'type' и нужными полями, либо None.
    """
    # Трек внутри альбома: /album/123/track/456
    m = re.search(r'/album/(\d+)/track/(\d+)', url)
    if m:
        return {'type': 'track', 'track_id': int(m.group(2))}

    # Просто трек: /track/456
    m = re.search(r'/track/(\d+)', url)
    if m:
        return {'type': 'track', 'track_id': int(m.group(1))}

    # Альбом: /album/123
    m = re.search(r'/album/(\d+)', url)
    if m:
        return {'type': 'album', 'album_id': int(m.group(1))}

    # Пользовательский плейлист: /users/<login>/playlists/<kind>
    m = re.search(r'/users/([^/]+)/playlists/(\d+)', url)
    if m:
        return {'type': 'playlist', 'user_login': m.group(1), 'kind': int(m.group(2))}

    return None


async def fetch_yandex_tracks(parsed: dict) -> list[tuple]:
    """
    По результату parse_yandex_url возвращает список (track_id, title).
    Работает через executor, чтобы не блокировать event loop.
    """
    loop = asyncio.get_event_loop()

    if parsed['type'] == 'track':
        def _get():
            t = ym_client.tracks([parsed['track_id']])[0]
            return [(t.id, t.title)]
        return await loop.run_in_executor(None, _get)

    elif parsed['type'] == 'album':
        def _get():
            album = ym_client.albums_with_tracks(parsed['album_id'])
            tracks = []
            for vol in (album.volumes or []):
                for t in vol:
                    tracks.append((t.id, t.title))
            return tracks
        return await loop.run_in_executor(None, _get)

    elif parsed['type'] == 'playlist':
        def _get():
            pl = ym_client.users_playlists(
                kind=parsed['kind'],
                user_id=parsed['user_login']
            )
            # users_playlists может вернуть список — берём первый элемент
            if isinstance(pl, list):
                pl = pl[0]
            fetched = pl.fetch_tracks()
            # fetch_tracks() может вернуть Playlist или list[TrackShort]
            if hasattr(fetched, 'tracks'):
                track_shorts = fetched.tracks or []
            elif isinstance(fetched, list):
                track_shorts = fetched
            else:
                track_shorts = []
            tracks = []
            for pt in track_shorts:
                # TrackShort.fetch_track() -> полный Track
                t = pt.fetch_track() if hasattr(pt, 'fetch_track') else getattr(pt, 'track', None)
                if t:
                    tracks.append((t.id, t.title))
            return tracks
        return await loop.run_in_executor(None, _get)

    return []


# ---------------------------------------------------------------------------
# Воспроизведение
# ---------------------------------------------------------------------------

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
    Возвращает (source_url, title, is_ok) для одиночного запроса.
    """
    yandex_pattern = r'(https?://)?(music\.yandex\.ru|yandex\.ru/music)'
    if re.match(yandex_pattern, query) and ym_client:
        try:
            track_match = re.search(r'/track/(\d+)', query)
            if track_match:
                track_id = int(track_match.group(1))

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


# ---------------------------------------------------------------------------
# События и команды
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Бот {bot.user} готов!")


@bot.command(name='play')
async def play(ctx, *, query):
    """
    Воспроизвести музыку по ссылке или поисковому запросу.
    Поддерживает ссылки на трек, альбом и плейлист Яндекс.Музыки.
    """
    if not ctx.author.voice:
        return await ctx.send("Вы не в голосовом канале!")

    voice_channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    yandex_pattern = r'(https?://)?(music\.yandex\.ru|yandex\.ru/music)'
    is_yandex_url = bool(re.match(yandex_pattern, query))

    # --- Плейлист / альбом ЯМ ---
    if is_yandex_url and ym_client:
        parsed = parse_yandex_url(query)

        if parsed and parsed['type'] in ('album', 'playlist'):
            await ctx.send("⏳ Загружаю треки...")
            try:
                track_list = await fetch_yandex_tracks(parsed)
            except Exception as e:
                print(f"Ошибка загрузки плейлиста/альбома: {e}")
                return await ctx.send("❌ Не удалось загрузить плейлист или альбом.")

            if not track_list:
                return await ctx.send("❌ Плейлист или альбом пуст.")

            guild_id = ctx.guild.id
            queue = get_queue(guild_id)
            vc = ctx.voice_client
            already_playing = vc.is_playing()

            for track_id, title in track_list:
                # Кладём track_id как source (play_next умеет его обработать),
                # original_query тоже track_id — чтобы play_next нашёл ссылку по ID
                await queue.put((track_id, title, str(track_id)))

            label = "плейлист" if parsed['type'] == 'playlist' else "альбом"
            await ctx.send(
                f"✅ Добавлено в очередь {len(track_list)} треков из {label}а."
            )

            if not already_playing:
                await play_next(ctx)
            return

    # --- Одиночный трек / YouTube / поиск ---
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
