import asyncio
import os
import threading
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# --- Health Check Server for Web Hosting ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.getenv('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# --- Bot Setup ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'extract_flat': 'in_playlist',
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32k -analyzeduration 0',
    'options': '-vn -ac 2 -ar 48000 -b:a 96k -bufsize 64k',
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.webpage_url = data.get('webpage_url')

    @classmethod
    async def extract_info(cls, search: str, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: ytdl.extract_info(search, download=False)
        )

    @classmethod
    async def from_url(cls, search: str, *, loop=None):
        data = await cls.extract_info(search, loop=loop)
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

async def play_next_song(interaction: discord.Interaction):
    manager = get_manager(interaction.guild_id)
    next_track = manager.get_next()

    if next_track is None:
        await interaction.channel.send("Queue finished. Disconnecting...")
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect()
        return

    if 'lazy_url' in next_track:
        try:
            player = await YTDLSource.from_url(next_track['lazy_url'], loop=bot.loop)
            next_track['source'] = player
            next_track['title'] = player.title
        except Exception as e:
            await interaction.channel.send(f"⚠️ Error playing {next_track['title']}: {e}")
            await play_next_song(interaction)
            return

    interaction.guild.voice_client.play(
        next_track['source'],
        after=lambda error: asyncio.run_coroutine_threadsafe(
            handle_song_end(interaction, error), bot.loop
        ),
    )
    await interaction.channel.send(f"🎶 **Now Playing:** {next_track['title']}")

async def handle_song_end(interaction: discord.Interaction, error):
    if error:
        print(f"Playback error: {error}")
    await play_next_song(interaction)

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} Slash Command(s)")
    except Exception as e:
        print(f"Failed to sync Slash Commands: {e}")
    print(f"Bot connected as {bot.user.name} ({bot.user.id})")

# --- Slash Commands ---
@bot.tree.command(name="join", description="Joins your current voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message("You must be in a voice channel!", ephemeral=True)
    channel = interaction.user.voice.channel
    if interaction.guild.voice_client is not None:
        await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()
    await interaction.response.send_message(f"Joined **{channel.name}**")

@bot.tree.command(name="play", description="Play a song or playlist from YouTube")
@app_commands.describe(search="YouTube URL, search terms, or playlist link")
async def play(interaction: discord.Interaction, search: str):
    if not interaction.user.voice:
        return await interaction.response.send_message("You need to be in a voice channel!", ephemeral=True)

    await interaction.response.defer()

    voice_client = interaction.guild.voice_client
    if voice_client is None:
        voice_client = await interaction.user.voice.channel.connect()

    try:
        data = await YTDLSource.extract_info(search, loop=bot.loop)
    except Exception as e:
        return await interaction.followup.send(f"Error processing request: {e}")

    manager = get_manager(interaction.guild_id)

    if 'entries' in data and len(data['entries']) > 1:
        entries = data['entries']
        await interaction.followup.send(f"📑 Adding **{len(entries)}** tracks from playlist to queue...")

        for idx, entry in enumerate(entries):
            track_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
            track_title = entry.get('title', 'Unknown Track')

            if idx == 0 and not voice_client.is_playing() and not voice_client.is_paused():
                player = await YTDLSource.from_url(track_url, loop=bot.loop)
                track_info = {'source': player, 'title': player.title, 'url': player.webpage_url}
                manager.current = track_info
                voice_client.play(
                    player,
                    after=lambda error: asyncio.run_coroutine_threadsafe(
                        handle_song_end(interaction, error), bot.loop
                    ),
                )
                await interaction.channel.send(f"🎶 **Now Playing:** {player.title}")
            else:
                manager.add({'lazy_url': track_url, 'title': track_title})
        return

    player = await YTDLSource.from_url(search, loop=bot.loop)
    track_info = {'source': player, 'title': player.title, 'url': player.webpage_url}

    if voice_client.is_playing() or voice_client.is_paused():
        manager.add(track_info)
        await interaction.followup.send(f" Added to queue: **{player.title}**")
    else:
        manager.current = track_info
        voice_client.play(
            player,
            after=lambda error: asyncio.run_coroutine_threadsafe(
                handle_song_end(interaction, error), bot.loop
            ),
        )
        await interaction.followup.send(f"🎶 **Now Playing:** {player.title}")

@bot.tree.command(name="pause", description="Pause current track")
async def pause(interaction: discord.Interaction):
    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.pause()
        await interaction.response.send_message("⏸️ Paused playback.")
    else:
        await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)

@bot.tree.command(name="resume", description="Resume paused track")
async def resume(interaction: discord.Interaction):
    if interaction.guild.voice_client and interaction.guild.voice_client.is_paused():
        interaction.guild.voice_client.resume()
        await interaction.response.send_message("▶️ Resumed playback.")
    else:
        await interaction.response.send_message("Playback is not paused.", ephemeral=True)

@bot.tree.command(name="skip", description="Skip the current track")
async def skip(interaction: discord.Interaction):
    if interaction.guild.voice_client and (interaction.guild.voice_client.is_playing() or interaction.guild.voice_client.is_paused()):
        interaction.guild.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped current track.")
    else:
        await interaction.response.send_message("Nothing is playing to skip.", ephemeral=True)

@bot.tree.command(name="queue", description="Show current queue")
async def queue_info(interaction: discord.Interaction):
    manager = get_manager(interaction.guild_id)
    embed = discord.Embed(title="Music Queue", color=discord.Color.blue())

    if manager.current:
        embed.add_field(name="Now Playing", value=manager.current['title'], inline=False)

    if manager.queue:
        queue_list = "\n".join([f"{idx + 1}. {item['title']}" for idx, item in enumerate(manager.queue)])
        embed.add_field(name="Up Next", value=queue_list, inline=False)
    elif not manager.current:
        embed.description = "The queue is empty."

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="stop", description="Stop music, clear queue, and leave channel")
async def stop(interaction: discord.Interaction):
    manager = get_manager(interaction.guild_id)
    manager.clear()

    if interaction.guild.voice_client:
        if interaction.guild.voice_client.is_playing() or interaction.guild.voice_client.is_paused():
            interaction.guild.voice_client.stop()
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("🛑 Stopped music, cleared queue, and left channel.")
    else:
        await interaction.response.send_message("Bot is not in a voice channel.", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)
