import discord
import wavelink
from discord.ext import commands
import dotenv
import os

dotenv.load_dotenv()

TOKEN = os.getenv("TOKEN")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD")

if LAVALINK_PASSWORD is None:
    raise ValueError("LAVALINK_PASSWORD не найден в .env файле")

if TOKEN is None:
    raise ValueError("TOKEN не найден в .env файле")

# Настройка интентов (из прошлого шага)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Функция подключения к серверу Lavalink при запуске бота
async def connect_nodes():
    await bot.wait_until_ready()

    nodes = [
        wavelink.Node(
            uri="http://127.0.0.1:2333",  # Адрес запущенного Lavalink
            password=LAVALINK_PASSWORD    # Стандартный пароль из application.yml
        )
    ]
    # Подключаем пул серверов к боту
    await wavelink.Pool.connect(nodes=nodes, client=bot)

@bot.event
async def on_ready():
    print(f"Мы вошли как {bot.user}")
    # Запускаем подключение к Lavalink асинхронно
    bot.loop.create_task(connect_nodes())

# Команда воспроизведения музыки
@bot.command(name='play')
async def play(ctx: commands.Context, *, search: str):
    # Проверяем, находится ли пользователь в голосовом канале
    if not ctx.author.voice:
        return await ctx.send("Сначала войдите в голосовой канал!")

    # Подключаемся к каналу или берем существующий плеер
    if not ctx.voice_client:
        vc: wavelink.Player = await ctx.author.voice.channel.connect(cls=wavelink.Player)
    else:
        vc: wavelink.Player = ctx.voice_client

    # Поиск трека (в данном случае через YouTube)
    tracks = await wavelink.Playable.search(search, source=wavelink.TrackSource.YouTube)
    if not tracks:
        return await ctx.send("Ничего не найдено по вашему запросу.")

    track = tracks[0]  # Берем первый трек из результатов поиска

    # Если музыка уже играет, добавляем в очередь, иначе запускаем
    if vc.playing:
        await vc.queue.put_wait(track)
        await ctx.send(f"Добавлено в очередь: **{track.title}**")
    else:
        await vc.play(track)
        await ctx.send(f"Сейчас играет: **{track.title}**")

# Команда остановки и выхода из канала
@bot.command(name='stop')
async def stop(ctx: commands.Context):
    vc: wavelink.Player = ctx.voice_client
    if vc:
        await vc.disconnect()
        await ctx.send("Музыка остановлена, бот покинул канал.")
    else:
        await ctx.send("Бот не находится в голосовом канале.")

# Замените токен на ваш актуальный
bot.run(TOKEN)
