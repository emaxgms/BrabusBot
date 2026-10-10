# discord-music-bot (BrabusBot)

## What this is

One-file Discord music bot: plays anything `yt-dlp` can reach (YouTube, SoundCloud, Bandcamp,
Vimeo, Twitch, 1000+ sites) plus Spotify links. Slash commands plus a button panel pinned in the
channel; saved playlists persist in `playlists.json`.

- Repo: https://github.com/emaxgms/BrabusBot
- Runs as the systemd **--user** unit `discord-music-bot.service`.

## Layout

- `bot.py` — the whole bot (commands, panel, queue, playlists).
- `requirements.txt` — `discord.py[voice]`, `yt-dlp`.
- `playlists.json` — persisted playlists (runtime state, keep it).
- `deploy/discord-music-bot.service` — the unit file.
- `bot.log`, `bot.py.bak*` — runtime log and stale backups: never commit these.

## Commands

`/play`, `/playnext`, `/pause`, `/resume`, `/skip`, `/shuffle`, `/move <n> <n>`, `/queue`, `/stop`,
`/panel`, plus `/plsave`, `/pladd`, `/plrm`, `/pllist`, `/pldel`.
Panel buttons: join, add, order, play/pause, skip, shuffle.

## How to verify (real commands)

```bash
systemctl --user restart discord-music-bot
systemctl --user status discord-music-bot
journalctl --user -u discord-music-bot -f      # live log
```

Then smoke-test in Discord: `/play <url>` (audio starts), `/queue`, a panel button, `/skip`.
A restart that leaves the bot "running" but silent means a voice-connect or yt-dlp failure — check
the journal before touching the code.

## Conventions and gotchas

- **This repo is the exception to the PR rule: commit directly to `main`.**
- When playback of a specific site breaks, update `yt-dlp` FIRST (`pip install -U yt-dlp`); sites
  change far more often than the bot code does.
- `playlists.json` is user data: never regenerate or overwrite it wholesale.
- Don't commit `bot.log` or `bot.py.bak*`; don't delete them either while debugging (they are the
  only history for a crash that killed the process).
