## FXseek 1.0.3

> Note: `1.0.3` and `1.0.4` existed only during development and were **never released**, so everything
> since 1.0.2 is consolidated into **1.0.3** — the notes for both are merged below.

This release is a run of fixes for problems **real users actually hit**: transcription that either hung
or looped, a batch queue wiped out by a single service hiccup, a menu-bar icon that vanished after a
while, and an About panel whose version number never matched the installer.

### New: subtitles for video
- A **Subtitles** toggle now sits at the right of the player's control bar. With a transcript, it shows
  the spoken lines or lyrics as an overlay pinned to the bottom of the video (your on/off choice is remembered).
- For a video with **no transcript yet**, the toggle turns into **"Transcribe this video"** and the same
  notice the audio panel shows appears below it. One click starts transcription, and subtitles come on
  automatically when it finishes.
- Same source as the audio lyric view: the transcript is one block of text with no per-sentence
  timestamps, so the overlay is synced by dividing the duration evenly across lines — close enough to
  follow, without pretending to be frame-accurate.

### Fixed: the menu-bar icon "disappears after a while"
Three independent root causes stacked up; any one of them was enough to lose the icon for good:
- **Liveness probes went through the system proxy.** The "is the service still alive?" request was
  silently proxied by `urllib` (with dev-sidecar, a corporate proxy, or any accelerator installed), so
  the probe got swallowed — while the log right next to it was full of `GET /health 200`. The tray
  declared the service dead when it was perfectly healthy.
  → Local probes now use a **direct opener** that bypasses proxies.
- **The self-exit code was 0, but the restarter only accepts 2.** The old logic exited by itself after
  3 failed probes (~12 s) with exit code 0; the restarter reads anything other than 2 as "the user is
  quitting the app", so it **never started the helper again**.
  → Failed probes now **warn without exiting** (one line after 32 s), process swaps always use the
  agreed exit code, and the restarter **restarts unconditionally** (up to 6 times) with a new
  "is the user actually quitting?" check.
- **A command file left over from the *previous* session was replayed (the worst one).** The menu bar and
  the main app talk through a small file, and the watcher treated "whatever is on disk at startup" as a
  fresh command — so a `quit` left behind last time was **re-executed on the next launch**, and the app
  quit itself.
  → Startup now records the file's **modification time** as the baseline and only honours later changes;
  the file is also cleared immediately after execution, as a second line of defence.
- The icon itself was made sturdier too: on this system, whether a menu-bar item gets placed is
  **random** (the button window ends up 0 px high, with no error and no warning). Previously we assumed
  a created item was a visible item; now a **main-thread heartbeat** measures the icon window every 4
  seconds and rebuilds it on the spot if it drops.

### Fixed: the version shown in About never matched the installer (stuck on v1.0.0)
- **Root cause**: the version used to be persisted along with the rest of the settings, while the save
  path never updated it — so the **stale value** in the user data directory shadowed the real version
  when settings were read back.
- **Fix**: the version now comes from code only (`app.py` holds the single source of truth) and is forced
  on load; `build_app.sh` / `release.sh` read it from the same place and now **fail loudly** instead of
  silently packaging a stale version number.

### Fixed: transcription quality — repetition detection rewritten
- **The old "characters per second > 8" heuristic is gone.** It had two real bugs: its allow-list regex
  only understood CJK + ASCII, so Korean, Russian and Japanese kana were stripped to zero characters and
  those languages could **never** be detected as looping; and `8.0` was calibrated on Chinese, while
  normal English at 150 wpm already runs ~8.8 letters/sec — so it **killed legitimate English tracks**
  and triggered a cascading re-cut from 60 s down to 20 s windows (tripling the request count).
- Replaced with language-, tempo- and chorus-agnostic signals: **zlib compression ratio** (threshold
  relaxed by the square root of text length) + **sentence uniqueness** (≥ 8 sentences and uniqueness
  < 15 %), plus a third **characters-per-second** test (threshold 15/s) to catch run-on loops that the
  first two let through. Measured across the whole library: 419 clean windows max out at 11.00/s, 123
  looping windows start at 22.75/s — **zero false positives, zero misses**.
- The ingest gate no longer keeps its own copy of the heuristic; it reuses the transcription-time check,
  so "accepted while transcribing, discarded while indexing" can't happen again.

### Fixed: transcription no longer takes "minutes and never finishes"
- **Degenerate windows were running to the model service's default output ceiling.** A looping local ASR
  **never stops on its own** and generates all the way to `max_tokens`: measured at 24,564–32,758
  characters and 130–220 seconds for a single window. Output is now capped by "tokens per second of
  audio": the same window went from **216.0 s / 28,664 chars → 25.6 s / 4,185 chars (8.4×)**. If the
  server doesn't recognise this non-standard field it returns 4xx, we automatically retry once without
  it, and remember that permanently.
- **The first window warms the model up for all the others** (measured: 31.5 s for the first, 4–5 s
  afterwards), so the first window's timeout was widened and no longer kills a perfectly normal start.
- When a re-cut window is **still** degenerate, the truncated looping output is no longer ingested
  (previously 4,961 characters of junk made it into the index, polluting keywords and vectors and
  getting whole tracks dropped by the ingest gate).

Measured: `say yeah` went from 4,961 characters of junk to 2,106 characters of real lyrics in 69.1 s;
a normal long track from 231.9 s to 12.6 s.

### Fixed: one service hiccup no longer wipes out the whole batch queue
- Connection failures / `504` / timeouts / `database is locked` are a **service-wide outage**, but they
  used to be recorded against each individual file — 200 pending jobs were each blacklisted three times
  and the entire queue froze. Such failures now only trigger **global backoff and skipping**, and never
  write a per-file blacklist.
- "Fill in now" no longer marks itself finished when every remaining job is on cooldown; it resumes when
  the cooldown expires.

### Install
1. Download `FXseek-1.0.3.dmg`, double-click to mount, and drag `FXseek.app` into *Applications*
2. **Delete the old copy first**: `/Applications/FXseek.app` (installing over it leaves stale files behind)
3. **On first launch use right-click → Open** (the app is not notarised, so Gatekeeper blocks it once)
4. Requires macOS 13 or newer + Apple Silicon (M-series)
5. The first launch downloads a ~1.8 GB model; after that it works **fully offline**

Free for noncommercial use (PolyForm Noncommercial 1.0.0); **commercial use requires a licence** — see [COMMERCIAL.md](https://github.com/FRr688/FXseek/blob/main/COMMERCIAL.md).

---

## FXseek 1.0.2

Another round of fixes for problems **real users actually hit**: a local model cut off by the system
proxy, a single bad file freezing the whole queue, and iPhone photos that could not get into the library.

### Fixed: local ASR / vision models broken by the system proxy
- **Symptom**: background transcription would stop partway through, with the same file failing over and
  over in the log:
  `HTTP 504: DevSidecar: no response from upstream ➜ http://127.0.0.1:9977/v1/audio/transcriptions`
- **Root cause**: the request went through the system proxy (dev-sidecar / corporate proxy / accelerators)
  while your local model service (oMLX / Ollama / LM Studio) lives on `127.0.0.1`. Short requests are
  fine, but **long audio uploads get cut by the proxy's own 60-second timeout** — which is why the same
  track transcribes in oMLX but fails in FXseek.
- **Fix**: a new `net_util.py` routes by destination — **loopback and LAN go direct; only public traffic
  uses the system proxy.** The vision model used for tagging goes the same way.
- Upload size came down as a bonus: audio is converted to 16 kHz mono 48 kbps mp3 first (a 26.4 MB track
  becomes **1.42 MB**, transcription 216.7 s → **182.8 s**). Measured accuracy differs from the lossless
  version by one punctuation mark (121 vs 121 characters, 99.17 % similarity).

### Fixed: one bad file froze the entire background queue
- **Symptom**: background fill-in sat forever at "round 1/10"; one failing file retried in place endlessly
  and every other pending job starved.
- **Fix**: each task gets its own `try/except` and **moves on to the next one** on failure; a file that
  fails 3 times in a row is "cold-stored" for 15 minutes; failures still count against the round's quota
  (no more misleading 0/10). The status bar gained "failed this round: N" and "skipped for now: N
  (3 consecutive failures, retrying later)".
- Also: a failure used to sleep 120 s unconditionally; now only **three consecutive** failures trigger
  the global backoff.

### New: HEIC / HEIF support (iPhone photos)
- `.heic` had always been on the supported-formats list, but Pillow cannot read HEIF out of the box, and
  the indexer **silently skipped** images it couldn't open — so photos taken on an iPhone could not be
  found, had no thumbnails, and never appeared in Memory Lane.
- `pillow-heif` (with libheif) is now bundled and registers its decoders at startup. A 2000×1248 HEIC
  indexes and produces thumbnails correctly in testing.

### Other
- All hard-coded personal absolute paths were removed from the source (the model fallback directory became
  the `FXSEEK_MODEL_DIR` environment variable plus the bundled `model/`, and docs now use `~/`).

### Install
1. Download `FXseek-1.0.2.dmg`, double-click to mount, and drag `FXseek.app` into *Applications*
2. **On first launch use right-click → Open** (the app is not notarised, so Gatekeeper blocks it once)
3. Requires macOS 13 or newer + Apple Silicon (M-series)
4. The first launch downloads a ~1.8 GB model; after that it works **fully offline**

Free for noncommercial use (PolyForm Noncommercial 1.0.0); **commercial use requires a licence** — see [COMMERCIAL.md](https://github.com/FRr688/FXseek/blob/main/COMMERCIAL.md).

---

## FXseek 1.0.1

This release is mainly about **opening a large library being slow**, plus a pass over Memory Lane.

### Performance (the headline)
- **Asset list cold start: 18.8 s → 1.2 s; reopening: 0.03 s.**
  Opening the library used to re-probe metadata for every single file (spawning `ffprobe` for audio and
  video, opening PIL for images), one after another. Results are now cached on disk keyed by
  *path + modification time + size* (`data/media_meta_cache.json`), and the cache invalidates itself when
  a file changes. The first build runs 16 probes in parallel and merges the two `ffprobe` calls per
  audio/video file into one.
- **The front end now renders in batches.**
  The first screen creates only 48 cards (it used to lay out all 270 at once) and appends the next batch
  of 24 as you approach the bottom, showing "showing 48 / 270 · scroll for more" as it goes. Thumbnails
  are still loaded on demand — scroll as far as you like, fetch only what you see.

### Memory Lane
- The scroll engine was rewritten around **per-frame offsets plus card recycling**: displacement never
  exceeds one card, so columns **no longer flicker**, and the top of the viewport always has content
  catching it — **no more blank gaps**.
- The button gained a breathing glow, which settles once you're inside so it doesn't distract.
- Everything inside the scene is locked except the "back to now" button and the thumbnails; clicking any
  thumbnail fades it out gracefully (it does not delete the file and does not touch the index).

### Other
- The "Star on GitHub" link in Settings now points at the real repository and opens in your system browser.
- Assorted small fixes: document format detection, thumbnail width parameter, the dependency audit tool,
  and dead-code cleanup.

### Install
1. Download `FXseek-1.0.1.dmg`, double-click to mount, and drag `FXseek.app` into *Applications*
2. **On first launch use right-click → Open** (the app is not notarised, so Gatekeeper blocks it once)
3. Requires macOS 13 or newer + Apple Silicon (M-series)
4. The first launch downloads a ~1.8 GB model; after that it works **fully offline**

Free for noncommercial use (PolyForm Noncommercial 1.0.0); **commercial use requires a licence** — see [COMMERCIAL.md](https://github.com/FRr688/FXseek/blob/main/COMMERCIAL.md).

---

[简体中文发版说明 →](release-notes.zh-CN.md)
