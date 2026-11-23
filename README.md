# Tunes

A personal music recommender. You pick a mood, it asks YouTube what plays next to your seeds, you listen and press Keep or Skip.

- **Mood**: a named set of seeds. Seeds are a library playlist, pasted YouTube links, and the tracks you keep in that mood.
- **Round**: reads the YouTube Mix of each seed, then ranks tracks that several seeds agree on.
- **Library**: plays the music you already own, by playlist, newest first, with shuffle. One button saves the playing track to the device.
- **Settings**: the last mood, the last playlist and shuffle are kept in `data/settings.json` on the server, so every device opens the same way. Only the token stays in the browser.
- **Keep**: downloads the audio into a playlist folder. **Skip**: never offered again.

No dependencies beyond Python 3.11, `yt-dlp` and `ffmpeg`. No build step. A Docker image is included.

## Run

```sh
cp server/tunes.example.json server/tunes.json   # set a token and the paths
python3 server/tunes.py
```

Open `http://localhost:8792` and paste the token.

## Config (`server/tunes.json`, never committed)

| Key | Meaning |
|---|---|
| `token` | Bearer token the web app sends |
| `port` | Default 8792 |
| `data_dir` | Where `state.db` lives |
| `music_dir` | Kept tracks land in `<music_dir>/<folder>` |
| `folders` | Playlist folders you can keep into |
| `beets_db`, `pinchflat_db` | Read only. Used for "already owned" and playlist seeds |
| `library_dir` | Where the library's audio files are on this machine. Without it the Library tab lists tracks but cannot play them |
| `beets_dir` | The folder beets believes the library is in, for absolute paths in its DB (default `/music`) |
| `seeds_per_round` | Seeds sampled per round (default 40) |
| `queue_size` | Tracks kept per round (default 50) |
| `ytdlp` | The yt-dlp command (default `["yt-dlp"]`). It can be another container's copy: `["docker", "exec", "pinchflat", "yt-dlp"]` |
| `ytdlp_music_dir` | `music_dir` as that yt-dlp sees it, when it runs in a container |
| `download_owner` | Optional `[uid, gid]` for downloaded files |
| `import_cmd` | Optional command behind the "Send to Plex" button |
| `auto_import_after` | Optional. Run `import_cmd` by itself once downloads have been quiet this many seconds |

## Docker (homelab)

```sh
docker compose up -d --build
```

- The image carries its own `yt-dlp`, `ffmpeg` and `deno`. yt-dlp updates at start and once a day.
- Host folders are mounted at the same paths, so `tunes.json` uses host paths.
- The import script runs inside the container and reaches beets through the Docker socket.

## Ranking

- Each seed's mix gives a track `1 / (1 + position * 0.15)`.
- Rank is the mean per seed, times `1 + 0.25 * sqrt(seeds - 1)`.
- A track needs two seeds behind it. Channels you only ever skipped count 0.4.
- With 12 or more seeds, a track recommended by over 25% of them is dropped as generic filler.
- Another upload of a seed's own song (lyrics video, reupload) is dropped.
- Owned, kept and skipped tracks never come back.

## Old recommender data

```sh
python3 server/tunes.py --import /path/to/music-tools/data
```

Loads verdicts, the download ledger, the mix cache, seed files and suggestion lists. Safe to run twice. It never writes back to the old files.

## Tests

```sh
python3 -m unittest discover -s tests
```
