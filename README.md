# discord-music-bot

One-file Discord music bot. Plays anything yt-dlp can reach (YouTube, SoundCloud,
Bandcamp, Vimeo, Twitch, 1000+ sites) plus Spotify links. Slash commands + a
button panel in the channel.

```
/play <url or search>   /playnext <url or search>   /pause  /resume  /skip  /shuffle  /move <n> <n>  /queue  /stop  /panel
/plsave <name> [<url or search>]   /pladd <name> <url or search>   /plrm <name> <n>   /pllist [<name>]   /pldel <name>
```

`/play <name>` loads a saved playlist (matched by name before any search), `/playnext`
jumps the queue. `/plsave <name>` with no query snapshots the current queue.
`/move 14 1` moves queue position 14 to position 1 (the target is clamped, so `/move 3 999`
sends it to the end).

Panel buttons: ➕ add (paste a link or search) · ↕️ order (reorder the queue) · ⏯ play/pause ·
⏭ skip · 🔀 shuffle · 📜 queue · ⏹ stop & disconnect

`↕️ order` opens a modal pre-filled with `1 2 3 …`: rewrite it as the permutation you want
(`3 1 2 …`) and submit. Discord has no drag & drop and a modal holds at most 5 fields, so the
whole queue is edited as one list of positions, not by dragging rows.

## Setup

1. https://discord.com/developers/applications → **New Application** → **Bot** → **Reset Token**, copy it.
   No privileged intents needed — leave them all off.
2. Invite it: **OAuth2 → URL Generator** → scopes `bot` + `applications.commands`,
   permissions `View Channels`, `Send Messages`, `Embed Links`, `Connect`, `Speak`.
   Or paste this with your client id:
   `https://discord.com/oauth2/authorize?client_id=YOUR_ID&scope=bot+applications.commands&permissions=3165184`
3. Run:

```bash
cd ~/projects/discord-music-bot
cp .env.example .env      # paste DISCORD_TOKEN=..., and GUILD_ID= right-click-copy-server-id
.venv/bin/python bot.py
```

`.venv` already exists with discord.py[voice] + yt-dlp. Rebuild with:
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

## Run as a service (survives logout / reboot)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/discord-music-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discord-music-bot
tail -f ~/projects/discord-music-bot/bot.log
```

(`journalctl --user` has no storage on this host, so the unit logs to `bot.log`.)

## Self-check (no token needed)

```bash
.venv/bin/python bot.py --check
```

Verifies ffmpeg, parses a Spotify link, resolves it to a real YouTube stream,
resolves a YouTube URL, runs a text search, decodes that stream through ffmpeg
into raw PCM (the real playback pipeline), checks the panel buttons and slash
commands are wired, and runs the queue state machine (advance/skip/shuffle/stop)
against a fake voice client. All against the live network except the last two.

## Notes

- Long resolves (a big Spotify album) show a progress embed after 3s: it becomes the
  `/play` result when it finishes, so a slow playlist never looks stuck.
- The next track's stream URL is resolved while the current one plays (URLs are good for
  hours), which removes the yt-dlp extract from the gap between tracks. `STREAM_TTL`
  re-resolves anything queued longer than 2h.
- **Spotify** exposes no audio stream without OAuth, so a link is resolved to
  `title + artist` and played from YouTube. Track links use the official oEmbed
  endpoint; album/playlist links scrape the public embed page, which Spotify can
  change at any time — if an album link stops working, queue the tracks directly.
- Saved playlists live in `playlists.json` (tracks already resolved, so loading one is
  instant). Queue is in RAM: restarting the bot clears it.
- The bot leaves the voice channel after `IDLE_TIMEOUT` seconds of nothing playing.
- Not included (say the word): loop/repeat, runtime volume, seek, DJ role,
  auto-disconnect when the channel empties, YT search-picker menu.
- If YouTube starts refusing requests, update yt-dlp first (`.venv/bin/pip install -U yt-dlp`).
  If that fails, export cookies into `YTDLP_COOKIES`.
