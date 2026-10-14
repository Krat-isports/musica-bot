import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.getenv('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

import asyncio
import os
from collections import defaultdict
import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("COMMAND_PREFIX", "!")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.webpage_url = data.get('webpage_url')

    @classmethod
    async def from_url(cls, search: str, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(search, download=False)
        )

        if 'entries' in data and data['entries']:
            data = data['entries'][0]

        filename = data['url']
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)


class GuildMusicManager:
    def __init__(self):
        self.queue = []
        self.current = None

    def add(self, item):
        self.queue.append(item)

    def get_next(self):
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        self.current = None
        return None

    def clear(self):
        self.queue.clear()
        self.current = None


music_managers = defaultdict(GuildMusicManager)


def get_manager(guild_id: int) -> GuildMusicManager:
    return music_managers[guild_id]


async def play_next_song(ctx):
    manager = get_manager(ctx.guild.id)
    next_track = manager.get_next()

    if next_track is None:
        await ctx.send("Queue finished. Disconnecting from voice channel...")
        await ctx.voice_client.disconnect()
        return

    ctx.voice_client.play(
        next_track['source'],
        after=lambda error: asyncio.run_coroutine_threadsafe(
            handle_song_end(ctx, error), bot.loop
        ),
    )
    await ctx.send(f"🎶 **Now Playing:** {next_track['title']}")


async def handle_song_end(ctx, error):
    if error:
        print(f"Playback error: {error}")
    await play_next_song(ctx)


@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user.name} ({bot.user.id})")


@bot.command(name="join", help="Joins your voice channel")
async def join(ctx):
    if not ctx.author.voice:
        return await ctx.send("You must be connected to a voice channel!")
    channel = ctx.author.voice.channel
    if ctx.voice_client is not None:
        return await ctx.voice_client.move_to(channel)
    await channel.connect()
    await ctx.send(f"Joined **{channel.name}**")


@bot.command(name="play", help="Plays audio from YouTube URL or search query")
async def play(ctx, *, search: str):
    if not ctx.author.voice:
        return await ctx.send("You need to be in a voice channel!")

    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()

    async with ctx.typing():
        try:
            player = await YTDLSource.from_url(search, loop=bot.loop)
        except Exception as e:
            return await ctx.send(f"Error processing track: {e}")

        manager = get_manager(ctx.guild.id)
        track_info = {
            'source': player,
            'title': player.title,
            'url': player.webpage_url,
        }

        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            manager.add(track_info)
            await ctx.send(f" Added to queue: **{player.title}**")
        else:
            manager.current = track_info
            ctx.voice_client.play(
                player,
                after=lambda error: asyncio.run_coroutine_threadsafe(
                    handle_song_end(ctx, error), bot.loop
                ),
            )
            await ctx.send(f"🎶 **Now Playing:** {player.title}")


@bot.command(name="pause", help="Pauses the currently playing track")
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused playback.")
    else:
        await ctx.send("Nothing is currently playing.")


@bot.command(name="resume", help="Resumes paused track")
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed playback.")
    else:
        await ctx.send("Playback is not paused.")


@bot.command(name="skip", help="Skips the current track")
async def skip(ctx):
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped current track.")
    else:
        await ctx.send("Nothing is playing to skip.")


@bot.command(name="queue", help="Shows current track and queue")
async def queue_info(ctx):
    manager = get_manager(ctx.guild.id)
    embed = discord.Embed(title="Music Queue", color=discord.Color.blue())

    if manager.current:
        embed.add_field(
            name="Now Playing", value=manager.current['title'], inline=False
        )

    if manager.queue:
        queue_list = "\n".join(
            [f"{idx + 1}. {item['title']}" for idx, item in enumerate(manager.queue)]
        )
        embed.add_field(name="Up Next", value=queue_list, inline=False)
    elif not manager.current:
        embed.description = "The queue is empty."

    await ctx.send(embed=embed)


@bot.command(name="stop", help="Stops music, clears queue, and disconnects")
async def stop(ctx):
    manager = get_manager(ctx.guild.id)
    manager.clear()

    if ctx.voice_client:
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send("🛑 Stopped music, cleared queue, and left channel.")
    else:
        await ctx.send("Bot is not in a voice channel.")


@bot.command(name="leave", help="Alias for stop command")
async def leave(ctx):
    await stop(ctx)


if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        raise ValueError("Please supply a valid DISCORD_TOKEN in the .env file")
    bot.run(TOKEN)
