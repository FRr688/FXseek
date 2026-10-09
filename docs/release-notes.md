## FXseek 1.0.8

**This is a bug-fix release — please upgrade. In 1.0.6 and 1.0.7 the settings page is broken: you
hit Save, nothing complains, and nothing is saved.**

### 1. No setting could be persisted (since 1.0.6)

The function that writes settings to disk, `_persist_settings()`, was **called but never defined**.
It was introduced as a call in `42b42b9` (the 1.0.6 ASR-duration fix) and its body was left out — the
function has never existed in any commit.

The result: **since 1.0.6, changing the theme, switching language, entering an API key or picking a
model all silently failed to save.**

- In `load_settings` the call sits inside `try/except: pass`, so the exception vanished;
- in `save_settings` it is unguarded, so it raised `NameError` and the endpoint returned **HTTP 400**.

And the old front-end did `await (await fetch(...)).json()` — **it never checked the status code**, so
it took the error JSON and cheerfully popped up "Settings saved". The UI looked fine; leave the page
and everything reverted.

Both halves are fixed: the function now exists (and writes **atomically** — `.tmp` then `rename`, so a
kill mid-write can't leave a truncated JSON that would make `load_settings` silently fall back to
defaults and appear to lose your keys and prompts), and the front-end now checks `res.ok` and reports
failures honestly.

> Lesson: wrapping the most critical step in `except: pass` is how you turn a failure into silence.

### 2. Official APIs unreachable: wrong URL for Zhipu and Gemini

The endpoint URL was built by asking "does the base end in `/v1`":

```python
url = (base + "/chat/completions") if base.endswith("/v1") else (base + "/v1/chat/completions")
```

That only caters to OpenAI. Two built-in presets hit it:

| Preset | Base | Old result | Outcome |
|---|---|---|---|
| Zhipu GLM | `https://open.bigmodel.cn/api/paas/v4` | `/api/paas/v4/v1/chat/completions` | **404** |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `/v1beta/openai/v1/chat/completions` | **404** |

A shared `net_util.api_url()` now decides by "**does the base carry a path at all**" — with a path,
append the suffix directly (these presets already are the full OpenAI-compatible prefix); only a bare
host (`https://api.deepseek.com`) gets `/v1` inserted. `/models`, `/chat/completions` and
`/audio/transcriptions` all go through it.

Verified against Zhipu: previously **404 Not Found**, now **401 "token expired or invalid"** — the
address is right, so the fake key is correctly rejected.

### Upgrade advice

If you are on 1.0.6 or 1.0.7, **please upgrade to 1.0.8** — otherwise no settings change takes effect.
Your saved configuration is not damaged and will still be read; settings you tried to change while
broken need to be set once more.

---

## FXseek 1.0.7

Search got slow. Not "a bit slower than before" — **typing a query and watching nothing happen for
minutes**. The obvious suspect was the index size. It wasn't the index.

Two costs had been hiding in the code, both of which grow with your library:

**1. Every search opened every image in your library.** A "is this a flat colour block?" check —
used only to down-rank solid-colour placeholders — was being run against every single candidate,
and it did a full `Image.open()` to do it. On an external drive that's ~187 ms per image. With 1,190
images: **223 seconds per search**, three orders of magnitude more than the actual vector search.

The fix is to compute that verdict **once, while the metadata is already being probed** — the image
is open at that moment anyway, and a 64×64 standard deviation is nearly free. The result is cached to
disk, so searching afterwards touches no image files at all.

> ⚠️ If you're reading this while patching your own copy: changing a probe field **requires bumping
> the cache key prefix** (`v2|` → `v3|`). A stale cache missing the new field silently falls back to
> the slow path — everything looks correct, it's just slow. That's the worst kind of bug.

**2. The deep semantic pass used a pure-Python dot-product loop.** 9,661 rows × 2,048 dimensions ×
(expanded terms × 2 instruction vectors). The first two tiers had been using NumPy for a long time;
the third never got converted. One matrix multiply instead of a Python loop: **22× faster**.

Two smaller bugs surfaced while in there:

- **An empty query returned HTTP 400.** It's an `IndexError` from `qvecs[0]` on an empty list, and
  any client that sends an empty box hits it.
- **`threshold` was ignored on four of five code paths.** It only applied to the deepest tier, so the
  same parameter worked or didn't depending on which path your query happened to take. All paths now
  share one filter.

Measured on a 9,661-row library:

| | Before | After |
|---|---|---|
| Text search (warm) | 223 s and climbing | **0.07 – 5.5 s** |
| Per-search image probes | 1,190 | **0** (cached) |
| First browse after restart | 70 s | **0.35 s** |

**Results are byte-for-byte identical.** `女人照片` → `05_人物_839.jpg` at 0.823, `鸡翅` →
` (216).jpg` at 1.015, `狗` → `小狗'(示例).png` at 0.950. A performance change that also changes your
results isn't an optimisation, it's a rewrite.

> One honest caveat: a **brand-new install still pays the probe cost once**, on the first search that
> touches your images. It's not precomputed — it's cached on first use. After that, including across
> restarts, searches are in the tens of milliseconds.

**Also in this release:** the tray menu's status line and the MCP panel's summary lines are dimmed.
They were never actually bold — they're custom-drawn menu items that don't participate in AppKit's
dimming of disabled items, so they rendered at full black next to a row of grey and *looked* heavy.
They now use a secondary colour instead.

### Install

Download `FXseek-1.0.7.dmg` below, drag it to Applications, and on first launch **right-click → Open**
(the app isn't notarised, so Gatekeeper will block a double-click once).

---

## FXseek 1.0.6

A fix for the thing that made long recordings look broken: **transcribing anything longer than about
ten minutes did nothing at all** — no progress, no error you could act on, and the ASR model never
even started. It was never a model problem. Two bugs were stacked on top of each other, and the
second one was the nasty one.

### The limit nobody could get past

`asr_max_duration` defaulted to **600 seconds**, and the check ran *before* the request was sent. A
50-minute concert track was rejected in **4.1 seconds** — faster than the model takes to load. That
is why it looked like transcription "wouldn't start": it was refused at the door.

Then the rejection wrote a permanent `asr_skip: "too_long"` marker, and **the two places that decide
whether to retry only asked "is this marker present?", never "which limit blocked it?"**. One of
them — the pending list shown in the UI — did not even accept a duration limit. So raising the
setting changed nothing: those files stayed skipped forever.

The second layer is worse than the first. The first is just a bad default you could work around. The
second meant **fixing the setting did not help**.

### What changed

- **Default is now `0` = unlimited.** No more gate.
- **A one-time migration moves your frozen `600` to `0`.** Your `settings.json` stored the old
  default, so changing the code default alone would have done nothing for existing installs. It is
  recorded in `_migrations`, so if you later set `600` on purpose it will not be touched again.
- **One shared `_asr_skip_lifted()`** replaces both checks, and both now consult the current limit.
- **`asr_status()` takes the limit**, so the pending list heals itself.
- **Successful transcription clears the skip markers** instead of leaving stale ones behind.
- **The error message now names the setting**: "…raise the duration limit under
  Settings → Smart services → Audio transcription, or set it to 0 for unlimited."

### Verified

The same 37.7-minute file, once the limit was lifted: **373.7 s, 5074 characters**, clean text. Then
a **2989-second (≈50 minute)** concert FLAC through the real HTTP endpoint:

```json
{"ok": true, "text": "快使用双面棍！电烧电的要命，隔壁是火树连环…"}
```

---

## FXseek 1.0.5

The interface now speaks **two languages**, the app can **check for its own updates**, and audio and
video finally **share one transcript panel**. Along the way we hunted down a menu-bar item whose
colour was being silently thrown away by AppKit, and a "heavy work" check that was far too eager.

### New: English / 简体中文 interface
- A **language switcher** lives in *Settings → Appearance* (first row, next to the theme). Pick
  **简体中文** or **English** and the whole UI follows — sidebar, settings, dialogs, toasts, empty
  states, tooltips.
- Switching is instant and remembered; there is no restart step.
- Your **own metadata is never translated** — artist names, album titles and filenames stay exactly
  as you typed or tagged them.
- Implementation note: instead of threading a lookup through ~193 call sites, a DOM walker translates
  text nodes after render, keeping the original string on each node so switching back is lossless.
  Dynamic sentences (ones with counts or filenames spliced in) go through pattern rules rather than a
  flat dictionary.

### New: Check for updates
- *About → Check for updates* tells you honestly which of three things is true: **a newer version
  exists** (with a Download button and the release notes), **you are already current**, or **the check
  failed**.
- If the check fails, the app says so and reveals a **releases** button that opens the GitHub releases
  page in your browser — it never pretends "you're up to date" when it simply could not reach GitHub.
- Version ordering understands development suffixes, so someone running a `-test` build is not told to
  "update" to an older official release they cannot install.

### New: one transcript panel, audio and video alike
- Video now gets the **same collapsible transcript panel** the audio player has: a **文字 / Transcript**
  button on the control bar slides the panel in beside the picture, showing the full text with
  **原始 / 优化** (raw vs smart-polished), **重新优化**, **重新转写** and **还原原文**.
- Your open/closed choice is remembered. The video is genuinely resized rather than covered, and the
  panel stacks below the picture automatically when the window is too narrow.
- Because both players now share one implementation, the **subtitle overlay refreshes together with the
  panel** — change the transcript and the subtitles follow immediately.

### Fixed: the menu-bar icon turned static
- Whether a menu-bar item gets placed by the system is **random** (roughly one in three attempts). The
  animation starter was nested inside the "retry" branch, so it only ran when the *first* placement had
  **failed** — meaning the lucky, first-try placements were exactly the ones that never animated.
- The animation starter now runs on every path; the retry branch only keeps its log line.

### Fixed: menu-bar items lost their colour
- AppKit **dims every disabled menu item** when it draws it, and an explicitly-set colour cannot
  override that. The "connected" rows were asking for the label colour and getting grey — the bold
  survived, the colour did not.
- Measurement matrix on this machine: disabled + no colour = grey; **disabled + explicit colour = still
  grey**; enabled + explicit colour = full colour; **disabled + custom view = colour honoured**.
- Rows now use a custom view, which renders the requested colour and remains unclickable. This also
  fixed the top "Server: running" line, which had been documented as green since forever but always
  drew grey.

### Fixed: on-demand model loading was too shy about unloading, and too eager about blocking
- The 2 GB model is unloaded after a period of inactivity, but the check asked "is any heavy task
  running?" without asking **where the model actually lives**.
- Tagging and transcription are now classified by whether their endpoint is **on this machine**
  (loopback) or **remote**. Point them at a remote API and they no longer block other heavy work —
  they genuinely do not compete for local memory.
- Unloading is a *separate* question and got its own check: tagging still needs the local model to
  encode text into vectors **even when the description came from a remote API**, so it still holds the
  model resident. Video transcription, which only writes metadata, does not.

### Fixed: clutter removed from the detail panel
- The **原始 / 优化 · 重新优化 · 还原原文** button row is gone from the detail side panel — it was
  noise there. The transcript is still readable and searchable, and a one-line hint points at the
  player's panel where those actions actually live.
- The image viewer's **1:1** button is gone too. "Fit to window" already resets zoom *and* rotation
  *and* panning, which is a superset — and the zoom percentage still shows when you are at 100%.

### New: refresh buttons confirm they are working
- Every button that re-fetches something (**Check for stale records**, **Refresh**, **Fetch list**,
  **Re-detect**, **Check for updates**) spins its icon and throws a soft ripple on click.
- This respects the **Reduce motion** accessibility setting; there is a three-way *Follow system / Full /
  Off* switch under *Settings → Appearance* if you want to override it.

### Install
1. Download `FXseek-1.0.5.dmg`, double-click to mount, and drag `FXseek.app` into *Applications*
2. **On first launch use right-click → Open** (the app is not notarised, so Gatekeeper blocks it once)
3. Requires macOS 13 or newer + Apple Silicon (M-series)
4. The first launch downloads a ~1.8 GB model; after that it works **fully offline**

Free for noncommercial use (PolyForm Noncommercial 1.0.0); **commercial use requires a licence** — see [COMMERCIAL.md](https://github.com/FRr688/FXseek/blob/main/COMMERCIAL.md).

---

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
