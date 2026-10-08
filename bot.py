#!/usr/bin/env python3
"""
Discord music bot - one file.

  /play <url|search>   Spotify / YouTube / SoundCloud / Bandcamp / 1000+ sites (via yt-dlp)
  /pause /resume /skip /shuffle /queue /stop /panel

A button panel is posted in the channel and stays usable (persistent View).
Auto-advances the queue; every track is resolved lazily at play time.

ponytail: single process, no Lavalink, no DB, no web dashboard. State lives in RAM.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.request
from array import array
from dataclasses import dataclass, field
from functools import partial

import discord
import yt_dlp
from discord.ext import commands

# --------------------------------------------------------------------------- config

UA = "Mozilla/5.0 (X11; Linux aarch64) discord-music-bot/1.0"


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID", "").strip()      # optional: instant slash-command sync in one server
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "500"))    # ponytail: hard cap, one user cannot OOM the Pi
COOKIES = os.getenv("YTDLP_COOKIES", "").strip()  # optional: export YouTube cookies if you hit bot-checks
VOLUME = float(os.getenv("VOLUME", "0.2"))        # ponytail: consoles (PS5) report 1.0 as deafening; one knob
IDLE_TIMEOUT = float(os.getenv("IDLE_TIMEOUT", "300"))  # seconds with nothing playing before leaving voice (0 = never)
STREAM_TTL = 7200                                 # trust a resolved googlevideo URL for 2h (real expiry is ~6h)
PLAYLISTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playlists.json")

YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": False,
    "extract_flat": "in_playlist",  # full formats for a single video, flat list for playlist entries
    "source_address": "0.0.0.0",    # avoids IPv6-only hangs
    "nocheckcertificate": True,
    "default_search": "ytsearch1",
}
if COOKIES:
    YTDL_OPTS["cookiefile"] = COOKIES

# YouTube needs a JS runtime to solve nsig/po-tokens; without one yt-dlp warns, drops
# formats and some streams come back 403. deno is yt-dlp's default (not installed here),
# node is - hand it the absolute path so it also works from the systemd unit's PATH.
_NODE = shutil.which("node") or "/home/ema/.local/bin/node"
if os.access(_NODE, os.X_OK):
    YTDL_OPTS["js_runtimes"] = {"node": {"path": _NODE}}

# rw_timeout: without it a silently dead connection blocks the reader forever; with it
# ffmpeg errors out after 15s of no data and the reconnect logic takes over.
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000 -nostdin"
FFMPEG_OPTS = "-vn -loglevel error"


def ydl(**over):
    return yt_dlp.YoutubeDL({**YTDL_OPTS, **over})


# --------------------------------------------------------------------------- resolve

SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist|episode)/([A-Za-z0-9]+)")


@dataclass
class Track:
    title: str
    webpage_url: str | None = None
    duration: int | None = None
    requester: str = ""
    thumbnail: str | None = None
    stream: str | None = None  # resolved direct media URL, good for hours (see STREAM_TTL)
    stream_at: float = field(default_factory=time.monotonic)  # when `stream` was obtained

    @property
    def label(self) -> str:
        return f"[{discord.utils.escape_markdown(self.title)}]({self.webpage_url})" if self.webpage_url else self.title


@dataclass
class Progress:
    """Counters bumped by the resolve() worker thread, polled by the event loop."""

    done: int = 0
    total: int = 0  # 0 = unknown (one extract, no per-entry counter)
    note: str = ""


def _get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read()


def spotify_queries(url: str) -> list[tuple[str, str]]:
    """Spotify serves no audio without OAuth creds -> map names to a YouTube search."""
    m = SPOTIFY_RE.search(url)
    if not m:
        return []
    kind, sid = m.groups()
    if kind == "track":
        j = json.loads(_get(f"https://open.spotify.com/oembed?url={url}"))
        return [(f"ytsearch1:{j['title']} {j.get('author_name', '')}".strip(), j["title"])]

    # album / playlist / episode: scrape the public embed page for its track list
    html = _get(f"https://open.spotify.com/embed/{kind}/{sid}").decode("utf8", "replace")
    m2 = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m2:
        return []
    found: list[dict] = []

    def walk(o):
        if isinstance(o, list):
            for i in o:
                walk(i)
        elif isinstance(o, dict):
            if str(o.get("uri", "")).startswith("spotify:track"):
                found.append(o)
            else:
                for v in o.values():
                    walk(v)

    walk(json.loads(m2.group(1)))
    out = []
    for t in found:
        q = " ".join(x for x in (t.get("title"), t.get("subtitle")) if x)
        out.append((f"ytsearch1:{q}", q))
    return out


def resolve(query: str, skipped: list[str] | None = None, progress: "Progress | None" = None) -> list[Track]:
    """Blocking. URL or plain search text -> playable tracks.

    Tracks that cannot be resolved (age-gated, deleted, region-locked, bot-checked) go to
    `skipped` instead of raising: one dead hit must not kill the other 49 of a playlist.
    `progress` is bumped per entry so a slow playlist can show a moving counter.
    """
    if SPOTIFY_RE.search(query):
        tracks = []
        qs = spotify_queries(query)
        if progress:
            progress.note, progress.total = "Spotify → YouTube", len(qs)
        for q, title in qs:
            if progress:
                progress.done += 1  # counts entries processed, dead ones included
            try:
                # flat listing only: 3x faster than a full extract and it cannot blow up on an
                # age-gated hit - the real stream is resolved at play time, where a failure
                # skips that one track and keeps the queue going
                with ydl() as y:
                    info = y.extract_info(q, download=False)
                e = (info.get("entries") or [info])[0]
            except Exception as ex:  # removed / blocked / no result for this title
                print(f"resolve: skipping {title!r}: {str(ex).splitlines()[0][:160]}", flush=True)
                if skipped is not None:
                    skipped.append(title)
                continue
            if e:
                tracks.append(Track(e.get("title") or title,
                                    e.get("url") or e.get("webpage_url") or q,
                                    e.get("duration"),
                                    thumbnail=(e.get("thumbnails") or [{}])[-1].get("url")))
        if not tracks:
            raise RuntimeError("could not read that Spotify link (album/playlist page changed?)")
        return tracks

    if not query.startswith("http"):
        query = "ytsearch1:" + query
    if progress:
        progress.note = "yt-dlp"  # one extract: a YouTube playlist arrives whole, no counter
    with ydl() as y:
        info = y.extract_info(query, download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    if entries:
        return [Track(e.get("title") or "unknown",
                      e.get("url") or e.get("webpage_url"),
                      e.get("duration"),
                      thumbnail=(e.get("thumbnails") or [{}])[-1].get("url")) for e in entries]
    return [Track(info.get("title") or "unknown", info.get("webpage_url"),
                  info.get("duration"), thumbnail=info.get("thumbnail"), stream=info.get("url"))]


def stream_url(track: Track) -> str:
    """Blocking. Googlevideo URLs expire (~6h), so do not trust an old cached one."""
    if track.stream and time.monotonic() - track.stream_at < STREAM_TTL:
        return track.stream
    with ydl(extract_flat=False, noplaylist=True) as y:
        info = y.extract_info(track.webpage_url, download=False)
    e = (info.get("entries") or [info])[0]
    if not e or not e.get("url"):
        raise RuntimeError("no audio stream found")
    return e["url"]


def fmt(sec: int | None) -> str:
    if not sec:
        return "--:--"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- playlists

def load_playlists() -> dict[str, list[dict]]:
    try:
        with open(PLAYLISTS, encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _pl_key(pl: dict, name: str) -> str:
    """The existing key, case-insensitively, so 'Mix' and 'mix' stay one playlist."""
    for k in pl:
        if k.lower() == name.strip().lower():
            return k
    return name.strip()[:60]


def _pl_write(pl: dict) -> None:
    tmp = PLAYLISTS + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(pl, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PLAYLISTS)


def pl_store(name: str, tracks: list[Track], append: bool = False) -> list[dict]:
    """Adds or replaces a saved playlist, returns its stored entries.

    ponytail: read-modify-write, no locking - one process writes it and two /pladd in the
    same instant would lose one. Add a lock when that shows up for real.
    """
    pl = load_playlists()
    k = _pl_key(pl, name)
    entries = [{"title": t.title, "webpage_url": t.webpage_url, "duration": t.duration} for t in tracks]
    pl[k] = pl.get(k, []) + entries if append else entries
    _pl_write(pl)
    return pl[k]


def pl_rm(name: str, position: int) -> list[dict] | None:
    """Removes the 1-based `position`; None when the playlist or the index is missing."""
    pl = load_playlists()
    k = _pl_key(pl, name)
    if k not in pl or not 1 <= position <= len(pl[k]):
        return None
    pl[k].pop(position - 1)
    _pl_write(pl)
    return pl[k]


def pl_del(name: str) -> bool:
    pl = load_playlists()
    k = _pl_key(pl, name)
    if k not in pl:
        return False
    del pl[k]
    _pl_write(pl)
    return True


def playlist_tracks(name: str) -> list[Track] | None:
    """A saved playlist is what /play matches first: its entries are already resolved."""
    pl = load_playlists()
    k = _pl_key(pl, name)
    if k not in pl:
        return None
    return [Track(**e) for e in pl[k]]


# --------------------------------------------------------------------------- progress

def progress_embed(pr: Progress, elapsed: float) -> discord.Embed:
    if pr.total:
        filled = min(10, round(10 * pr.done / pr.total))
        bar = "▓" * filled + "░" * (10 - filled)
        left = elapsed / pr.done * (pr.total - pr.done) if pr.done else 0
        line = f"`{bar}` {pr.done}/{pr.total}" + (f" · ~{left:.0f}s left" if pr.total > pr.done else "")
    else:
        line = "⏳ resolving"  # one extract, nothing to count per-track
    return discord.Embed(title="Loading", colour=0x5865F2,
                         description=f"{line}\n{pr.note or '…'} · {elapsed:.0f}s")


async def resolve_with_progress(channel, query: str, skipped: list[str]) -> tuple[list[Track], "discord.Message | None"]:
    """resolve() off the event loop, with a progress line that only appears if it is slow.

    ponytail: the worker thread only bumps ints (atomic under the GIL), the loop polls them
    once a second - no locks, no cross-thread callbacks. 3s grace so one song never flashes.
    """
    pr = Progress()
    task = asyncio.ensure_future(asyncio.to_thread(resolve, query, skipped, pr))
    msg, dead = None, False
    t0 = time.monotonic()
    while True:
        done, _ = await asyncio.wait({task}, timeout=1.0)
        if done:
            break
        el = time.monotonic() - t0
        if dead:
            continue
        if msg is None:
            if el >= 3:
                msg = await channel.send(embed=progress_embed(pr, el))
        else:
            try:
                await msg.edit(embed=progress_embed(pr, el))
            except discord.HTTPException:
                dead = True  # deleted -> stop editing, keep resolving
    try:
        tracks = task.result()
    except Exception:
        if msg is not None and not dead:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
        raise
    return tracks, msg


# --------------------------------------------------------------------------- player

class Player:
    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.voice: discord.VoiceClient | None = None
        self.panel: discord.Message | None = None
        self.channel: discord.abc.Messageable | None = None
        self.loop = asyncio.get_running_loop()  # _after runs on ffmpeg's thread and needs a loop ref
        self._advancing = False
        self._pending = 0  # advance requests held back by the guard (counted, see _replay)
        self._started = False
        self._prefetching: Track | None = None
        self._idle: asyncio.Task | None = None
        self._source = None  # current PCMVolumeTransformer, so a stalled one can be killed
        self._epoch = 0  # bumped per track and on halt: stale `after` callbacks are ignored
        self._played_at = 0.0  # when the current track started (early-death detection)
        self._user_stop = False  # skip()/stop() are deliberate: no "dropped mid-stream" warning

    # --- voice plumbing -----------------------------------------------------
    async def connect(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        if self.voice and self.voice.is_connected():
            if self.voice.channel != channel:
                await self.voice.move_to(channel)
        else:
            self.voice = await channel.connect(self_deaf=True)
        return self.voice

    async def enqueue(self, tracks: list[Track], requester: str, front: bool = False) -> None:
        for t in tracks:
            t.requester = requester
        self.cancel_idle()  # something is queued again
        room = MAX_QUEUE - len(self.queue)
        head = tracks[:room]
        if front:
            self.queue[0:0] = head  # /playnext: these first, order kept
        else:
            self.queue.extend(head)
        if not (self.voice and self.voice.is_playing()) and self.current is None:
            await self.play_next()
        else:
            await self.refresh()
            self._prefetch()  # /playnext moved the head: keep one track resolved ahead

    async def play_next(self, error: Exception | None = None) -> None:
        # ponytail: reentrancy guard. _after (ffmpeg thread), skip() and enqueue() can all
        # land here inside the same resolution window; without it a track is popped twice
        # and the first one is silently lost. Dropped when play_next gets an await-free path.
        if self._advancing:
            # A caller that lands here must be DEFERRED when the in-flight call has already
            # started a track (its ffmpeg died instantly, or a user pressed Skip -> the event
            # means "advance again"), but dropped when it is merely resolving: there the event
            # is a duplicate of the advance already under way. Dropping the first case strands
            # a full queue forever ("bot joined the channel and never plays anything").
            if self._started:
                self._pending += 1
            return
        self._advancing = True
        self._started = False
        try:
            # A track that died before its duration ran out (403, reset, stall) must be
            # reported, or it silently vanishes and the changeover looks like a glitch.
            prev, self.current = self.current, None
            if prev is not None and not self._user_stop and prev.duration:
                played = time.monotonic() - self._played_at
                if played < prev.duration - 15:  # 15s slack: shorter leftovers are an edit
                    print(f"play: stream died on {prev.title!r} after {played:.0f}s of "
                          f"{prev.duration}s" + (f": {str(error).splitlines()[0][:120]}" if error else ""),
                          flush=True)
                    await self.notify(f"⚠️ **{prev.title[:60]}** dropped mid-stream, skipping.")
            self._user_stop = False
            if not (self.voice and self.voice.is_connected()):
                await self.refresh()
                return
            while self.queue:
                track = self.queue.pop(0)
                try:
                    url = await asyncio.to_thread(stream_url, track)
                except Exception as e:  # yt-dlp: video gone, region lock, age gate...
                    print(f"play: skipping {track.title!r}: {str(e).splitlines()[0][:160]}", flush=True)
                    await self.notify(f"Skipping **{track.title}** - `{e}`")
                    continue
                if not (self.voice and self.voice.is_connected()):
                    return  # stopped while resolving
                track.stream = None  # URLs expire; re-resolve next time
                self.current = track
                self._epoch += 1
                self._source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTS),
                    volume=VOLUME,
                )
                self.voice.play(self._source, after=partial(self._after, epoch=self._epoch))
                self._started = True
                self._played_at = time.monotonic()
                await self.refresh()
                self.cancel_idle()
                self._prefetch()  # resolve the next one while this plays
                return
            await self.refresh()
            self.arm_idle()  # nothing left to play -> leave after IDLE_TIMEOUT
        finally:
            self._advancing = False
            if self._pending:
                n, self._pending = self._pending, 0
                asyncio.create_task(self._replay(n))

    def _after(self, error: Exception | None, epoch: int) -> None:
        """Runs in ffmpeg's thread - hop back to the event loop.

        Killing a stalled source also fires its `after`; the epoch tells those stale
        callbacks apart from the live track's (a stale one would double-advance the queue).
        """
        if epoch != self._epoch:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.play_next(error), self.loop)
        except RuntimeError:
            pass  # loop already closed (shutdown)

    async def _replay(self, n: int) -> None:
        """Run the advances that the guard held back while a play_next was in flight.

        Each arrives with a track just started (a Skip pressed during the transition) or
        just dead (its ffmpeg failed instantly): cut whatever is playing, then advance.
        """
        for _ in range(n):
            if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
                self._halt()  # the deferred skip means "cut the track that just started"
            await self.play_next()

    # --- queue plumbing -----------------------------------------------------
    def _prefetch(self) -> None:
        """Resolve the next track's stream while the current one plays.

        ponytail: a resolved googlevideo URL stays valid for hours (measured ~6h), so one
        obtained during playback is still good when its turn comes. That is what removes the
        ~1.6s yt-dlp extract from the gap between tracks, leaving ffmpeg's ~0.25s spawn.
        STREAM_TTL still bounds it: a track queued hours ago is re-resolved, not 403'd.
        """
        if self._prefetching is not None or not self.queue:
            return
        t = self.queue[0]
        if t.stream and time.monotonic() - t.stream_at < STREAM_TTL:
            return
        self._prefetching = t

        async def run() -> None:
            try:
                t.stream = await asyncio.to_thread(stream_url, t)
                t.stream_at = time.monotonic()
            except Exception as e:  # play time retries it and skips it if it is really gone
                print(f"prefetch: {t.title!r}: {str(e).splitlines()[0][:120]}", flush=True)
            finally:
                self._prefetching = None

        asyncio.create_task(run())

    def arm_idle(self) -> None:
        """Leave the voice channel after IDLE_TIMEOUT with nothing playing."""
        if not IDLE_TIMEOUT or self._idle is not None:
            return
        self._idle = asyncio.create_task(self._idle_wait())

    def cancel_idle(self) -> None:
        if self._idle is not None:
            task, self._idle = self._idle, None
            task.cancel()

    async def _idle_wait(self) -> None:
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
            if self.queue or self.current or not (self.voice and self.voice.is_connected()):
                return  # something is playing again
            print(f"idle: nothing playing for {IDLE_TIMEOUT:g}s, leaving voice", flush=True)
            await self.notify(f"👋 Leaving — nothing playing for {IDLE_TIMEOUT / 60:g} min.")
            await self.stop()
        except asyncio.CancelledError:
            pass
        finally:
            if self._idle is asyncio.current_task():
                self._idle = None

    # --- controls -----------------------------------------------------------
    def _halt(self) -> None:
        """Stop the current track AND kill its ffmpeg.

        voice.stop() does not wake a player thread blocked in read() on a stalled stream:
        without killing the source, the `after` callback (which advances the queue) waits
        for ffmpeg's reconnect loop to give up - minutes. That is the "Skip does nothing".
        """
        if not self.voice:
            return
        self._epoch += 1  # the kill below fires `after`; we advance ourselves, ignore it
        src = self.voice.source  # grab before stop(): it drops the player reference
        self.voice.stop()
        if src is not None:
            try:
                src.cleanup()  # SIGKILLs ffmpeg -> a blocked read() returns EOF at once
            except Exception:
                pass

    async def skip(self) -> None:
        self._user_stop = True
        playing = bool(self.voice and (self.voice.is_playing() or self.voice.is_paused()))
        if playing:
            self._halt()  # works even when the stream is stalled
        if self._advancing:
            self._pending += 1  # mid-transition: hold the skip, _replay will run it
        else:
            await self.play_next()

    async def stop(self) -> None:
        self._user_stop = True
        self.queue.clear()
        self.current = None
        if self.voice:
            self._halt()
            await self.voice.disconnect()
            self.voice = None
        await self.refresh()

    def shuffle(self) -> None:
        random.shuffle(self.queue)

    # --- panel --------------------------------------------------------------
    def embed(self, status: str = "") -> discord.Embed:
        e = discord.Embed(colour=0x5865F2)
        if self.current:
            e.title = "🎵 Now playing"
            e.description = f"{self.current.label}\n`{fmt(self.current.duration)}` · requested by {self.current.requester}"
            if self.current.thumbnail:
                e.set_thumbnail(url=self.current.thumbnail)
        else:
            e.title = "🎵 Music"
            e.description = "Nothing playing — use `/play <url>`."
        if self.queue:
            head = "\n".join(f"`{i}.` {t.title[:70]}" for i, t in enumerate(self.queue[:8], 1))
            more = f"\n…and {len(self.queue) - 8} more" if len(self.queue) > 8 else ""
            e.add_field(name=f"Up next ({len(self.queue)})", value=head + more, inline=False)
        state = status or ("Paused" if (self.voice and self.voice.is_paused()) else
                           "Playing" if (self.voice and self.voice.is_playing()) else "Idle")
        e.set_footer(text=state)
        return e

    async def ensure_panel(self) -> None:
        if self.panel is not None or self.channel is None:
            return
        self.panel = await self.channel.send(embed=self.embed(), view=panel_view())

    async def refresh(self, status: str = "") -> None:
        await self.ensure_panel()
        if self.panel is None:
            return
        try:
            await self.panel.edit(embed=self.embed(status), view=panel_view())
        except discord.HTTPException:
            self.panel = None  # message deleted -> a new one is posted on the next command

    async def to_bottom(self, status: str = "") -> None:
        """Delete the panel and repost it as the newest message.

        Discord has no sticky messages; the old panel sits above every reply that lands
        below it. Reposting after a command keeps it at the bottom, and the old panel is
        removed instead of piling up.
        """
        if self.channel is None:
            return
        old, self.panel = self.panel, None
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        self.panel = await self.channel.send(embed=self.embed(status), view=panel_view())

    async def notify(self, text: str) -> None:
        if self.channel:
            try:
                await self.channel.send(text)
            except discord.HTTPException:
                pass


# --------------------------------------------------------------------------- UI

async def _say(i: discord.Interaction, text: str) -> None:
    """Ephemeral reply that works whether the interaction is already acked or not."""
    if i.response.is_done():
        await i.followup.send(text, ephemeral=True)
    else:
        await i.response.send_message(text, ephemeral=True)


async def _player(i: discord.Interaction) -> "Player | None":
    """The clicking user's player, or None (+ an ephemeral reason) if unusable."""
    p = i.client.players.get(i.guild_id)
    if p is None or not (p.voice and p.voice.is_connected()):
        await _say(i, "Not connected — use `/play` first.")
        return None
    me = i.guild.me
    if i.user.voice is None or me.voice is None or i.user.voice.channel != me.voice.channel:
        await _say(i, "Join the bot's voice channel first.")
        return None
    return p


class AddModal(discord.ui.Modal, title="Add to queue"):
    """Discord has no inline text input in a message - a modal is the only one there is.

    ponytail: one-shot, no persistence. The last link is not remembered; that would be RAM
    state for a field the user pastes into once.
    """

    link = discord.ui.TextInput(label="URL or search",
                                placeholder="https://open.spotify.com/track/… or 'sultans of swing'")

    async def on_submit(self, i: discord.Interaction) -> None:
        await i.response.defer(ephemeral=True)
        p = await _player(i)  # re-checked: the user may have left voice while typing
        if p is None:
            return
        try:
            tracks = await asyncio.to_thread(resolve, self.link.value)
        except Exception as e:
            return await i.followup.send(f"Could not resolve that: `{e}`", ephemeral=True)
        if not tracks:
            return await i.followup.send("Nothing found.", ephemeral=True)
        await p.enqueue(tracks, i.user.display_name)
        await p.to_bottom()  # keep the panel at the bottom after adding
        head = tracks[0].label if len(tracks) == 1 else f"{len(tracks)} tracks"
        await i.followup.send(f"➕ Queued {head}", ephemeral=True)


def parse_order(text: str) -> list[int]:
    """Positions out of a free-form string: '3 1 2', '3,1,2' and '3\\n1\\n2' all work."""
    return [int(n) for n in re.findall(r"\d+", text)]


def queue_order(q: list[Track], order: list[int]) -> bool:
    """Reorder `q` in place from a 1-based permutation. False if `order` is not one."""
    if sorted(order) != list(range(1, len(q) + 1)):
        return False
    q[:] = [q[i - 1] for i in order]
    return True


class OrderModal(discord.ui.Modal, title="Reorder the queue"):
    """Type the new order as positions; the field is pre-filled with the current one.

    ponytail: Discord has no drag & drop and a modal holds at most 5 fields, so the whole
    queue is edited as ONE permutation. The queue can move while the modal is open (a /play
    lands): the submitted permutation then no longer matches and is refused, never applied
    to the wrong tracks.
    """

    order = discord.ui.TextInput(label="Positions, top first (e.g. 3 1 2)",
                                 style=discord.TextStyle.paragraph, required=True)

    def __init__(self, n: int):
        super().__init__()
        self.order.default = " ".join(str(k) for k in range(1, n + 1))

    async def on_submit(self, i: discord.Interaction) -> None:
        await i.response.defer(ephemeral=True)
        p = await _player(i)
        if p is None:
            return
        nums = parse_order(self.order.value)
        if not queue_order(p.queue, nums):
            return await i.followup.send(
                f"Send every position from 1 to {len(p.queue)} exactly once, e.g. `3 1 2 …`.",
                ephemeral=True)
        await p.refresh("Reordered")
        await i.followup.send("↕️ Queue reordered.", ephemeral=True)


class Panel(discord.ui.View):
    """timeout=None + fixed custom_ids => survives bot restarts (re-registered in setup_hook)."""

    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, i: discord.Interaction, error: Exception, item) -> None:
        # Discord only ever shows "interaction failed" - keep the real cause in bot.log.
        print(f"panel[{getattr(item, 'custom_id', '?')}] failed: {error!r}", file=sys.stderr, flush=True)

    @discord.ui.button(emoji="🔊", label="Join", style=discord.ButtonStyle.success, custom_id="mb:join")
    async def join(self, i: discord.Interaction, _b):
        # The only button that must NOT go through _player(): connecting is exactly the state
        # _player rejects ("Not connected"), so it does its own, weaker check.
        if i.user.voice is None or i.user.voice.channel is None:
            return await _say(i, "Join a voice channel first.")
        p = i.client.players.setdefault(i.guild_id, Player(i.client, i.guild))
        p.channel = i.channel  # so the panel refresh/repost lands in this channel
        await i.response.defer()
        try:
            await p.connect(i.user.voice.channel)
        except Exception as e:  # missing Connect/Speak permission, channel full...
            return await i.followup.send(f"Could not join: `{e}`", ephemeral=True)
        p.arm_idle()  # joined with nothing queued: do not squat in the channel forever
        await p.refresh()

    @discord.ui.button(emoji="➕", label="Add", style=discord.ButtonStyle.success, custom_id="mb:add")
    async def add(self, i: discord.Interaction, _b):
        if not await _player(i):
            return
        await i.response.send_modal(AddModal())  # must be the first response: it is here

    @discord.ui.button(emoji="↕️", label="Order", style=discord.ButtonStyle.secondary, custom_id="mb:order")
    async def order(self, i: discord.Interaction, _b):
        p = await _player(i)
        if not p:
            return
        if not p.queue:
            return await _say(i, "The queue is empty — nothing to reorder.")
        await i.response.send_modal(OrderModal(len(p.queue)))  # must be the first response

    @discord.ui.button(emoji="⏯", label="Play/Pause", style=discord.ButtonStyle.primary, custom_id="mb:toggle")
    async def toggle(self, i: discord.Interaction, _b):
        p = await _player(i)
        if not p:
            return
        await i.response.defer()  # must precede play_next: it resolves over the network (>3s ack deadline)
        if p.voice.is_paused():
            p.voice.resume()
        elif p.voice.is_playing():
            p.voice.pause()
        else:
            await p.play_next()
        await p.refresh()

    @discord.ui.button(emoji="⏭", label="Skip", style=discord.ButtonStyle.secondary, custom_id="mb:skip")
    async def skip(self, i: discord.Interaction, _b):
        p = await _player(i)
        if not p:
            return
        await i.response.defer()
        await p.skip()

    @discord.ui.button(emoji="🔀", label="Shuffle", style=discord.ButtonStyle.secondary, custom_id="mb:shuffle")
    async def shuffle(self, i: discord.Interaction, _b):
        p = await _player(i)
        if not p:
            return
        p.shuffle()
        await i.response.defer()
        await p.refresh("Shuffled")

    @discord.ui.button(emoji="📜", label="Queue", style=discord.ButtonStyle.secondary, custom_id="mb:queue")
    async def queue(self, i: discord.Interaction, _b):
        # ack first: a long queue + a busy loop can blow the 3s interaction deadline
        await i.response.defer(ephemeral=True)
        p = await _player(i)
        if p:
            await i.followup.send(embed=p.embed(), ephemeral=True)

    @discord.ui.button(emoji="⏹", label="Stop", style=discord.ButtonStyle.danger, custom_id="mb:stop")
    async def stop(self, i: discord.Interaction, _b):
        p = await _player(i)
        if not p:
            return
        await i.response.defer()
        await p.stop()



_PANEL: "Panel | None" = None


def panel_view() -> Panel:
    """The panel singleton, built on first use INSIDE a running loop.

    discord.ui.BaseView.__init__ captures the running loop into its private __stopped future.
    A View built at import time has no loop, so __stopped stays None - and ViewStore.dispatch_view
    then drops every click silently: no log, no error, Discord only shows "interaction failed".
    """
    global _PANEL
    if _PANEL is None:
        _PANEL = Panel()
    return _PANEL


# --------------------------------------------------------------------------- bot

intents = discord.Intents.default()  # no privileged intents required
if os.getenv("ENABLE_PREFIX_COMMANDS", "").lower() in ("1", "true", "yes"):
    intents.message_content = True  # disable unless you toggled it in the dev portal, or login fails
bot = commands.Bot(command_prefix="!", intents=intents)
bot.players: dict[int, Player] = {}


def player(ctx: commands.Context) -> Player:
    return bot.players.setdefault(ctx.guild.id, Player(bot, ctx.guild))


@bot.event
async def setup_hook():
    bot.add_view(panel_view())  # re-bind buttons after restart
    if GUILD_ID:
        g = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=g)
        await bot.tree.sync(guild=g)
    else:
        await bot.tree.sync()


@bot.event
async def on_ready():
    print(f"logged in as {bot.user} ({len(bot.guilds)} guilds)")
    for g in bot.guilds:
        print(f"  guild: {g.name}  id={g.id}")


async def enqueue_query(ctx: commands.Context, query: str, front: bool = False) -> None:
    """Shared body of /play and /playnext: voice check -> resolve (progress) -> queue."""
    await ctx.defer()
    p = player(ctx)
    voice = ctx.author.voice
    if voice is None or voice.channel is None:
        return await ctx.send("You must be in a voice channel.", ephemeral=True)
    await p.connect(voice.channel)
    p.channel = ctx.channel

    saved = None if query.strip().startswith("http") else playlist_tracks(query)
    msg = None
    skipped: list[str] = []
    if saved is not None:
        tracks = saved  # saved entries are already resolved: nothing to look up
    else:
        try:
            tracks, msg = await resolve_with_progress(ctx.channel, query, skipped)
        except Exception as e:
            return await ctx.send(f"Could not resolve that: `{e}`")
    if not tracks:
        p.arm_idle()  # a next try may come, but do not sit in the channel forever
        return await ctx.send("Nothing found.")

    await p.enqueue(tracks, ctx.author.display_name, front=front)
    head = tracks[0] if len(tracks) == 1 else f"{len(tracks)} tracks"
    text = (f"➕ Queued {head if isinstance(head, str) else head.label}"
            + (f"\n⚠️ {len(skipped)} unavailable (age/geo/removed): "
               + ", ".join(f"`{s}`" for s in skipped[:5])
               + (f" +{len(skipped) - 5} more" if len(skipped) > 5 else "")
               if skipped else ""))
    if msg is not None:
        await msg.edit(content=text, embed=None)  # the loading line becomes the result
    else:
        await ctx.send(text)
    await p.to_bottom()  # repost the panel below the reply: pin it at the bottom


@bot.hybrid_command(description="Play a URL (Spotify/YouTube/…), search a song, or load a saved playlist")
async def play(ctx: commands.Context, *, query: str):
    await enqueue_query(ctx, query)


@bot.hybrid_command(description="Jump the queue: play this right after the current track")
async def playnext(ctx: commands.Context, *, query: str):
    await enqueue_query(ctx, query, front=True)


# --- saved playlists ("/play <name>" to load one) ---------------------------------

@bot.hybrid_command(description="Save a song/URL/playlist under a name (no query = save the current queue)")
async def plsave(ctx: commands.Context, name: str, *, query: str = ""):
    await ctx.defer()
    p = player(ctx)
    if not name.strip():
        return await ctx.send("Give the playlist a name.", ephemeral=True)
    if query:
        try:
            tracks = await asyncio.to_thread(resolve, query)
        except Exception as e:
            return await ctx.send(f"Could not resolve that: `{e}`", ephemeral=True)
    else:
        tracks = ([p.current] if p.current else []) + list(p.queue)
    if not tracks:
        return await ctx.send("Nothing to save — pass a query, or queue something first.", ephemeral=True)
    entries = pl_store(name, tracks)
    await ctx.send(f"💾 Saved **{name}** — {len(entries)} tracks. Load it with `/play {name}`")


@bot.hybrid_command(description="Append songs to a saved playlist")
async def pladd(ctx: commands.Context, name: str, *, query: str):
    await ctx.defer()
    try:
        tracks = await asyncio.to_thread(resolve, query)
    except Exception as e:
        return await ctx.send(f"Could not resolve that: `{e}`", ephemeral=True)
    if not tracks:
        return await ctx.send("Nothing found.", ephemeral=True)
    entries = pl_store(name, tracks, append=True)
    await ctx.send(f"➕ **{name}** now has {len(entries)} tracks")


@bot.hybrid_command(description="Remove track N from a saved playlist")
async def plrm(ctx: commands.Context, name: str, position: int):
    entries = pl_rm(name, position)
    if entries is None:
        return await ctx.send(f"No **{name}** playlist, or no track {position}.", ephemeral=True)
    await ctx.send(f"➖ Removed {position} — **{name}** has {len(entries)} left", ephemeral=True)


@bot.hybrid_command(description="Delete a saved playlist")
async def pldel(ctx: commands.Context, name: str):
    ok = pl_del(name)
    await ctx.send(f"🗑 Deleted **{name}**" if ok else f"No **{name}** playlist.", ephemeral=True)


@bot.hybrid_command(description="List saved playlists, or the tracks of one")
async def pllist(ctx: commands.Context, name: str = ""):
    pl = load_playlists()
    if name:
        entries = playlist_tracks(name)
        if entries is None:
            return await ctx.send(f"No **{name}** playlist.", ephemeral=True)
        body = "\n".join(f"`{i}.` {t.title[:70]} `{fmt(t.duration)}`" for i, t in enumerate(entries[:40], 1))
        more = f"\n…and {len(entries) - 40} more" if len(entries) > 40 else ""
        return await ctx.send(embed=discord.Embed(title=f"📜 {_pl_key(pl, name)} ({len(entries)})",
                                                 description=body + more))
    if not pl:
        return await ctx.send("No saved playlists yet — `/plsave <name> <url|song>`", ephemeral=True)
    body = "\n".join(f"**{k}** — {len(v)} tracks" for k, v in sorted(pl.items()))
    await ctx.send(embed=discord.Embed(title=f"📜 Playlists ({len(pl)})", description=body))


@bot.hybrid_command(description="Pause playback")
async def pause(ctx: commands.Context):
    p = player(ctx)
    if p.voice and p.voice.is_playing():
        p.voice.pause()
    await p.refresh()
    await ctx.send("⏸ Paused", ephemeral=True)


@bot.hybrid_command(description="Resume playback")
async def resume(ctx: commands.Context):
    p = player(ctx)
    if p.voice and p.voice.is_paused():
        p.voice.resume()
    elif p.voice and not p.voice.is_playing():
        await p.play_next()
    await p.refresh()
    await ctx.send("▶️ Resumed", ephemeral=True)


@bot.hybrid_command(description="Skip the current track")
async def skip(ctx: commands.Context):
    p = player(ctx)
    await p.skip()
    await ctx.send("⏭ Skipped", ephemeral=True)


@bot.hybrid_command(description="Shuffle the queue")
async def shuffle(ctx: commands.Context):
    p = player(ctx)
    p.shuffle()
    await p.refresh("Shuffled")
    await ctx.send(f"🔀 Shuffled {len(p.queue)} tracks", ephemeral=True)


def queue_move(q: list[Track], source: int, target: int) -> bool:
    """1-based move inside the queue; False when `source` is out of range.

    `target` is clamped, so /move 3 999 means "send it to the end" instead of an error.
    """
    n = len(q)
    if not 1 <= source <= n:
        return False
    target = max(1, min(target, n))
    if source != target:
        q.insert(target - 1, q.pop(source - 1))
    return True


@bot.hybrid_command(description="Move a queued track to another position (e.g. /move 14 1)")
async def move(ctx: commands.Context, source: int, target: int):
    p = player(ctx)
    if not queue_move(p.queue, source, target):
        return await ctx.send(
            f"The queue has {len(p.queue)} track(s) — position `{source}` is out of range.", ephemeral=True)
    await p.refresh("Reordered")
    pos = max(1, min(target, len(p.queue)))
    await ctx.send(f"↕️ Moved **{p.queue[pos - 1].title}** to position {pos}", ephemeral=True)


@bot.hybrid_command(description="Show the queue")
async def queue(ctx: commands.Context):
    p = player(ctx)
    await ctx.send(embed=p.embed())


@bot.hybrid_command(description="Stop and leave the voice channel")
async def stop(ctx: commands.Context):
    p = player(ctx)
    await p.stop()
    await ctx.send("⏹ Stopped", ephemeral=True)


@bot.hybrid_command(description="Post the control panel again")
async def panel(ctx: commands.Context, ephemeral: bool = False):
    p = player(ctx)
    p.channel = ctx.channel
    await p.to_bottom()  # deletes the old panel and moves it to the bottom
    if ephemeral:
        await ctx.send("Panel posted.", ephemeral=True)


# --------------------------------------------------------------------------- checks

class _FakeVoice:
    """Just enough of VoiceClient to run the queue state machine without Discord."""

    def __init__(self):
        self.played: list = []
        self.connected = True
        self.playing = False
        self.paused = False
        self.source = None  # VoiceClient.source: _halt() reads it before stopping

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def play(self, source, after=None):
        self.played.append(source)
        self.after = after
        self.source = source
        self.playing, self.paused = True, False

    def stop(self):
        self.playing = self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    async def disconnect(self):
        self.connected = self.playing = False


class _FailVoice(_FakeVoice):
    """ffmpeg dies the instant playback starts (403 / dead stream) -> after() fires at once."""

    def play(self, source, after=None):
        super().play(source, after)
        if after:
            threading.Thread(target=after, args=(RuntimeError("boom"),), daemon=True).start()


class _Msg:
    async def edit(self, **kw):
        await asyncio.sleep(0)  # a real REST edit yields the loop - that is the race window


class _Chan:
    async def send(self, *a, **kw):
        await asyncio.sleep(0)
        return _Msg()


class _ProgressMsg(_Msg):
    """Records what the progress line showed, so the bar can be asserted."""

    def __init__(self, log: list[str]):
        self.log = log

    async def edit(self, embed=None, **kw):
        await asyncio.sleep(0)
        self.log.append((embed.description if embed else str(kw)).replace("\n", " | "))

    async def delete(self):
        await asyncio.sleep(0)
        self.log.append("deleted")


class _ProgressChan(_Chan):
    def __init__(self):
        self.sent: list[_ProgressMsg] = []
        self.edits: list[str] = []

    async def send(self, *a, **kw):
        await asyncio.sleep(0)
        msg = _ProgressMsg(self.edits)
        self.sent.append(msg)
        return msg


def _check() -> int:
    ok = True

    def t(name: str, cond: bool, extra: str = ""):
        nonlocal ok
        ok &= cond
        print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")

    t("ffmpeg on PATH", shutil.which("ffmpeg") is not None, shutil.which("ffmpeg") or "")
    t("duration format", fmt(65) == "1:05" and fmt(3661) == "1:01:01" and fmt(None) == "--:--", fmt(3661))
    t("spotify url parse", SPOTIFY_RE.search("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT") is not None)

    try:
        q = spotify_queries("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        t("spotify oembed", bool(q), q[0][0] if q else "")
    except Exception as e:
        t("spotify oembed", False, f"{e}")

    try:
        tr = resolve("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        url = stream_url(tr[0]) if tr else ""     # /play queues it, the stream is resolved here
        t("spotify -> youtube", bool(tr) and url.startswith("http"), f"{tr[0].title!r} {fmt(tr[0].duration)}")
    except Exception as e:
        t("spotify -> youtube", False, f"{e}")

    try:
        tr = resolve("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        t("youtube url", bool(tr and tr[0].stream), f"{tr[0].title!r}")
    except Exception as e:
        t("youtube url", False, f"{e}")

    try:
        tr = resolve("sultans of swing")
        t("text search", bool(tr), tr[0].title if tr else "")
    except Exception as e:
        t("text search", False, f"{e}")

    # A playlist must not die on one bad hit. Two separate failure modes, tested without
    # depending on what YouTube's search happens to return today:
    #  - the search itself raises  -> that track goes to `skipped` (and /play warns about it)
    #  - the hit is age-gated      -> only fails at play time (stream_url), where play_next
    #                                 skips it and carries on with the queue
    _orig_sq, _orig_ydl = globals()["spotify_queries"], globals()["ydl"]
    try:
        globals()["spotify_queries"] = lambda _u: [
            ("ytsearch1:broken search", "broken"),
            ("ytsearch1:Sentadona Ai Calica Davi Kneip", "playable"),
        ]
        calls = {"n": 0}

        def flaky_ydl(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("search blew up")
            return _orig_ydl(**kw)

        sk: list[str] = []
        pr = Progress()
        globals()["ydl"] = flaky_ydl
        tr = resolve("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", sk, pr)
        t("playlist skips an unresolvable track", len(tr) == 1 and sk == ["broken"],
          f"queued={len(tr)} skipped={sk}")
        t("progress counts every entry", (pr.total, pr.done) == (2, 2),
          f"{pr.done}/{pr.total} {pr.note}")
    except Exception as e:
        t("playlist skips an unresolvable track", False, f"{e}")
    finally:
        globals()["spotify_queries"], globals()["ydl"] = _orig_sq, _orig_ydl

    try:
        stream_url(Track("age-gated", "https://www.youtube.com/watch?v=-A9nqiIu0Hw"))
        t("age-gated track fails at play time", False, "no error (cookies present?)")
    except Exception as e:
        t("age-gated track fails at play time", True, f"skipped by play_next: {str(e).splitlines()[0][:60]}")

    # the actual playback pipeline: yt-dlp stream URL -> ffmpeg -> raw PCM
    try:
        tr = resolve("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        src = discord.FFmpegPCMAudio(tr[0].stream, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTS)
        pcm, peak = b"", 0
        for _ in range(80):
            chunk = src.read()
            if not chunk:
                break
            pcm += chunk
            peak = max(peak, max(abs(v) for v in array("h", chunk)))
            if len(pcm) >= 48000 * 4 * 2:  # 2s of 48kHz stereo s16le
                break
        src.cleanup()
        t("ffmpeg decodes stream", peak > 500, f"{len(pcm)} bytes, peak {peak}/32767")
    except Exception as e:
        t("ffmpeg decodes stream", False, f"{e}")

    async def volume_applied() -> bool:
        p = Player(bot, None)  # type: ignore[arg-type]
        p.voice = _FakeVoice()  # type: ignore[assignment]
        p.queue = [Track(title="v", webpage_url="http://x", stream="memory://s")]
        await p.play_next()
        s = p.voice.played[0]
        r = isinstance(s, discord.PCMVolumeTransformer) and abs(s.volume - VOLUME) < 1e-9
        s.cleanup()
        return r

    t("volume applied to every source", asyncio.run(volume_applied()), f"{VOLUME:g} = {VOLUME:.0%}")

    async def panel_ok() -> list[bool]:
        v = Panel()                 # inside the loop: that is what makes clicks dispatchable
        bot.add_view(v)
        stopped = getattr(v, "_BaseView__stopped", None)
        m = AddModal()              # the Add button's modal: no text input => nothing to paste into
        om = OrderModal(3)          # the Order button's modal: pre-filled with the current order
        return [
            len(v.children) == 8 and all(isinstance(c, discord.ui.Button) for c in v.children),
            {"mb:join", "mb:add", "mb:order"} <= {c.custom_id for c in v.children},
            stopped is not None and not stopped.done(),
            len(m.children) == 1 and isinstance(m.children[0], discord.ui.TextInput),
            len(om.children) == 1 and om.order.default == "1 2 3",
        ]

    flags = asyncio.run(panel_ok())
    t("panel buttons wired", all(flags[:2]), " ".join(c.custom_id for c in Panel().children))
    t("panel clickable", flags[2], "view __stopped is live (None => clicks dropped silently)")
    t("add modal has its input", flags[3], f"{len(AddModal().children)} component(s)")
    t("order modal is pre-filled", flags[4], f"default={OrderModal(3).order.default!r}")

    def reorder_ok() -> list[bool]:
        q = [Track(f"t{i}") for i in range(1, 6)]
        ok = [queue_order(q, parse_order("3,1 2\n4 5")) and [t.title for t in q] == ["t3", "t1", "t2", "t4", "t5"],
              not queue_order(q, parse_order("1 2 3")) and [t.title for t in q] == ["t3", "t1", "t2", "t4", "t5"],
              not queue_order(q, parse_order("1 1 2 3 4")),      # duplicate -> refused whole
              not queue_order(q, parse_order("1 2 3 4 6")),      # out of range -> refused whole
              # a permutation applies to the CURRENT order: reverse of [t3,t1,t2,t4,t5]
              queue_order(q, parse_order("5 4 3 2 1")) and [t.title for t in q] == ["t5", "t4", "t2", "t1", "t3"],
              queue_order([], [])]                               # empty queue is a no-op, not a crash
        return ok

    flags = reorder_ok()
    t("queue reorder by permutation", all(flags),
      f"{sum(flags)}/{len(flags)} asserts, failed {[i for i, f in enumerate(flags) if not f]}")

    def move_ok() -> list[bool]:
        q = [Track(f"t{i}") for i in range(1, 6)]
        ok = [queue_move(q, 4, 1) and [t.title for t in q] == ["t4", "t1", "t2", "t3", "t5"],
              queue_move(q, 2, 99) and [t.title for t in q] == ["t4", "t2", "t3", "t5", "t1"],  # clamped to the end
              not queue_move(q, 9, 1) and [t.title for t in q] == ["t4", "t2", "t3", "t5", "t1"],
              queue_move(q, 3, 3) and [t.title for t in q] == ["t4", "t2", "t3", "t5", "t1"]]  # no-op
        return ok

    flags = move_ok()
    t("queue move command", all(flags),
      f"{sum(flags)}/{len(flags)} asserts, failed {[i for i, f in enumerate(flags) if not f]}")

    names = {c.name for c in bot.tree.get_commands()}
    want = {"play", "playnext", "pause", "resume", "skip", "shuffle", "move", "queue", "stop", "panel",
            "plsave", "pladd", "plrm", "pllist", "pldel"}
    t("slash commands registered", want <= names, ",".join(sorted(want & names)))

    # queue state machine (add/skip/advance/shuffle/stop) - no Discord needed
    async def queue_logic() -> list[bool]:
        p = Player(bot, None)  # type: ignore[arg-type]
        p.voice = _FakeVoice()  # type: ignore[assignment]
        p.queue = [Track(title=f"t{i}", webpage_url="http://x", stream="memory://s") for i in (1, 2, 3)]
        await p.play_next()
        r = [len(p.voice.played) == 1,
             [x.title for x in p.queue] == ["t2", "t3"],
             p.current is not None and p.current.title == "t1"]
        # the race: two advances in the same window must consume exactly ONE track
        # (the in-flight call had not started a track yet, so the 2nd event is a duplicate)
        await asyncio.gather(p.play_next(), p.play_next())
        r += [len(p.voice.played) == 2, [x.title for x in p.queue] == ["t3"]]
        p.shuffle()
        r.append([x.title for x in p.queue] == ["t3"])
        played = p.voice.played
        await p.stop()
        r += [p.queue == [], p.current is None, p.voice is None]
        for src in played:
            src.cleanup()

        # auto-advance: after() fires on ffmpeg's thread -> the next track must start
        # (this is the classic "bot plays one song then goes silent forever" bug)
        p2 = Player(bot, None)  # type: ignore[arg-type]
        p2.voice = _FakeVoice()  # type: ignore[assignment]
        p2.queue = [Track(title=f"a{i}", webpage_url="http://x", stream="memory://s") for i in (1, 2)]
        await p2.play_next()
        th = threading.Thread(target=p2.voice.after, args=(None,))
        th.start()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if len(p2.voice.played) == 2:
                break
        th.join()
        r += [len(p2.voice.played) == 2, p2.current is not None and p2.current.title == "a2"]
        for src in p2.voice.played:
            src.cleanup()
        return r

    flags = asyncio.run(queue_logic())
    t("queue state machine", all(flags), f"{sum(flags)}/{len(flags)} asserts")

    # every stream dies instantly while play_next is still awaiting refresh(): the queue must
    # still drain. This is the "bot joins the channel, queue shows 50 tracks, plays nothing".
    async def stranded() -> list[bool]:
        p = Player(bot, None)  # type: ignore[arg-type]
        p.voice = _FailVoice()  # type: ignore[assignment]
        p.channel = _Chan()  # type: ignore[assignment]
        p.queue = [Track(title=f"s{i}", webpage_url="http://x", stream="memory://s") for i in range(3)]
        await p.play_next()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if not p.queue and not p._advancing and not p._pending:
                break
        r = [p.queue == [], len(p.voice.played) == 3, not p._pending]
        for src in p.voice.played:
            src.cleanup()
        return r

    flags = asyncio.run(stranded())
    t("queue drains when every stream dies", all(flags), f"{sum(flags)}/{len(flags)} asserts")

    t("stream cache is reused inside TTL",
      stream_url(Track("x", "http://x", stream="memory://s")) == "memory://s")

    # prefetch: the next track is extracted while the current one plays, so the changeover
    # costs only ffmpeg's spawn (~0.25s) instead of a full yt-dlp extract (~1.6s).
    # yt-dlp is stubbed rather than stream_url, so the cache/TTL path is the real one.
    async def prefetch_ok() -> list[bool]:
        calls: list[str] = []

        class _Ydl:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, query, download=False):
                calls.append(query)
                time.sleep(0.05)  # stands in for the extract
                return {"title": query, "url": f"memory://{query}"}

        _orig = globals()["ydl"]
        globals()["ydl"] = lambda **kw: _Ydl()
        try:
            p = Player(bot, None)  # type: ignore[arg-type]
            p.voice = _FakeVoice()  # type: ignore[assignment]
            p.queue = [Track(title=f"p{i}", webpage_url=f"http://p{i}") for i in (1, 2, 3)]
            await p.play_next()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if p.queue[0].stream:
                    break
            r = [calls == ["http://p1", "http://p2"],   # p2 extracted while p1 still plays
                 p.current is not None and p.current.title == "p1",
                 "http://p3" not in calls]              # one track ahead, not the whole queue
            await p.play_next()                         # p2's turn: cached, no second extract
            r.append(calls.count("http://p2") == 1)
            for _ in range(50):                         # p3 is prefetched by the same call
                await asyncio.sleep(0.02)
                if calls.count("http://p3") == 1:
                    break
            r.append(calls.count("http://p3") == 1)
            played, p.voice = p.voice.played, None      # type: ignore[assignment]
            for src in played:
                src.cleanup()
            return r
        finally:
            globals()["ydl"] = _orig

    flags = asyncio.run(prefetch_ok())
    t("prefetch resolves one track ahead", all(flags),
      f"{sum(flags)}/{len(flags)} asserts, failed {[i for i, f in enumerate(flags) if not f]}")

    async def idle_ok() -> list[bool]:
        global IDLE_TIMEOUT
        keep = IDLE_TIMEOUT
        IDLE_TIMEOUT = 0.3
        try:
            p = Player(bot, None)  # type: ignore[arg-type]
            p.voice = _FakeVoice()  # type: ignore[assignment]
            p.channel = _Chan()  # type: ignore[assignment]
            await p.play_next()  # empty queue -> arms the timer
            await asyncio.sleep(0.2)
            r = [p.voice is not None]  # still connected before the timeout
            await asyncio.sleep(0.7)
            r.append(p.voice is None)  # left on its own
            return r
        finally:
            IDLE_TIMEOUT = keep

    flags = asyncio.run(idle_ok())
    t("idle timeout leaves the channel", all(flags), f"{sum(flags)}/{len(flags)} asserts")

    def playlists_ok() -> list[bool]:
        global PLAYLISTS
        keep = PLAYLISTS
        PLAYLISTS = os.path.join(os.path.dirname(keep), "playlists-check.json")  # never the real file
        try:
            if os.path.exists(PLAYLISTS):
                os.remove(PLAYLISTS)
            pl_store("Mix", [Track("a", "http://a", 60)])
            pl_store("mix", [Track("b", "http://b", 61)], append=True)  # same playlist, different case
            r = [[t.title for t in (playlist_tracks("MIX") or [])] == ["a", "b"],
                 playlist_tracks("nope") is None]
            pl_rm("Mix", 1)
            r += [[t.title for t in (playlist_tracks("mix") or [])] == ["b"],
                  pl_rm("Mix", 9) is None]           # out of range -> refused
            r.append(pl_del("MIX") and playlist_tracks("mix") is None)
            return r
        finally:
            for leftover in (PLAYLISTS, PLAYLISTS + ".tmp"):
                if os.path.exists(leftover):
                    os.remove(leftover)
            PLAYLISTS = keep

    flags = playlists_ok()
    t("playlists save/append/remove/delete", all(flags), f"{sum(flags)}/{len(flags)} asserts")

    # the loading line: nothing for a fast resolve, a moving bar once it drags past the grace
    async def progress_ok() -> list[bool]:
        _orig = globals()["resolve"]

        def slow(query, skipped=None, progress=None):
            time.sleep(1.0)
            if progress:
                progress.done, progress.total, progress.note = 3, 3, "Spotify → YouTube"
            time.sleep(4.5)  # still running past the 3s grace, so the line gets edited too
            return [Track("t", "http://t")]

        globals()["resolve"] = slow
        try:
            ch = _ProgressChan()
            tracks, msg = await resolve_with_progress(ch, "x", [])  # type: ignore[arg-type]
            shown = ch.edits[-1] if ch.edits else ""
            return [len(ch.sent) == 1,               # posted once, not per poll
                    len(ch.edits) >= 1,              # and then updated in place
                    "▓" in shown and "3/3" in shown,
                    msg is ch.sent[0],               # handed back to become the result
                    tracks[0].title == "t"]
        finally:
            globals()["resolve"] = _orig

    flags = asyncio.run(progress_ok())
    t("progress bar while loading", all(flags),
      f"{sum(flags)}/{len(flags)} asserts, failed {[i for i, f in enumerate(flags) if not f]}")

    # a resolve that blows up must not leave a "Loading" line behind
    async def progress_error_ok() -> list[bool]:
        _orig = globals()["resolve"]

        def boom(query, skipped=None, progress=None):
            time.sleep(3.2)
            raise RuntimeError("nope")

        globals()["resolve"] = boom
        try:
            ch = _ProgressChan()
            try:
                await resolve_with_progress(ch, "x", [])  # type: ignore[arg-type]
                return [False]
            except RuntimeError:
                return [len(ch.sent) == 1, "deleted" in ch.edits]
        finally:
            globals()["resolve"] = _orig

    flags = asyncio.run(progress_error_ok())
    t("progress line cleaned up on failure", all(flags), f"{sum(flags)}/{len(flags)} asserts")

    # a skip pressed while a transition is resolving must survive the guard, or the track
    # being resolved plays anyway and the button looks dead
    async def skip_mid_transition() -> list[bool]:
        p = Player(bot, None)  # type: ignore[arg-type]
        p.voice = _FakeVoice()  # type: ignore[assignment]
        p.queue = [Track("q", "http://x", stream="memory://s")]
        p._advancing = True  # a resolve for the next track is in flight
        await p.skip()
        r = [p._pending == 1, len(p.voice.played) == 0]
        p._pending = 0
        return r

    flags = asyncio.run(skip_mid_transition())
    t("skip during a transition is counted", all(flags), f"{sum(flags)}/{len(flags)} asserts")

    # the panel is deleted and reposted as the newest message (user: keep it at the bottom)
    async def panel_bottom() -> list[bool]:
        class _P(_Msg):
            def __init__(self, c, i):
                self.c, self.id = c, i

            async def edit(self, **kw):
                self.c.edits += 1
                await asyncio.sleep(0)

            async def delete(self):
                self.c.deleted.append(self.id)
                await asyncio.sleep(0)

        class _C:
            def __init__(self):
                self.sent, self.deleted, self.edits = [], [], 0

            async def send(self, *a, **kw):
                m = _P(self, 100 + len(self.sent))
                self.sent.append(m)
                await asyncio.sleep(0)
                return m

        p = Player(bot, None)  # type: ignore[arg-type]
        p.channel = _C()  # type: ignore[assignment]
        await p.to_bottom()  # first panel: nothing to delete
        first = p.panel
        await p.to_bottom()  # repost: the old one is deleted, the new one is newest
        return [len(p.channel.sent) == 2,
                p.panel is not first,
                p.channel.deleted == [first.id],
                p.panel.id == 101]

    flags = asyncio.run(panel_bottom())
    t("panel reposts to the bottom", all(flags), f"{sum(flags)}/{len(flags)} asserts")

    # a stream dying right after start must be reported, not silently skipped
    async def early_death() -> list[bool]:
        class _C(_Chan):
            def __init__(self):
                self.msgs = []

            async def send(self, *a, **kw):
                self.msgs.append(str(a[0] if a else kw))
                await asyncio.sleep(0)
                return _Msg()

        p = Player(bot, None)  # type: ignore[arg-type]
        p.voice = _FakeVoice()  # type: ignore[assignment]
        p.channel = _C()  # type: ignore[assignment]
        p.current = Track("gone", "http://x", duration=200, stream="memory://s")
        p._played_at = time.monotonic()  # it started just now...
        p.queue = [Track("next", "http://x", stream="memory://s")]
        await p.play_next()  # ...and died right away: this is the advance after the death
        r = [any("dropped mid-stream" in m for m in p.channel.msgs),
             p.current is not None and p.current.title == "next"]
        for src in p.voice.played:
            src.cleanup()
        return r

    flags = asyncio.run(early_death())
    t("early stream death is reported", all(flags), f"{sum(flags)}/{len(flags)} asserts")

    return 0 if ok else 1


def main() -> int:
    if "--check" in sys.argv:
        return _check()
    if not TOKEN:
        print("DISCORD_TOKEN missing. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2
    bot.run(TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
