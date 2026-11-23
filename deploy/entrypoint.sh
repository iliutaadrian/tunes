#!/bin/sh
# YouTube changes often and an old yt-dlp stops downloading. Update at start and once a day.
update() { pip install --no-cache-dir -q -U "yt-dlp[default]" || echo "yt-dlp update failed" >&2; }
update
(while sleep 86400; do update; done) &
exec python3 -u /app/server/tunes.py
