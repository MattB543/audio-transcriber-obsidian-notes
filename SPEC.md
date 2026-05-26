# notes-pipeline SPEC

Voice note capture → transcription → Obsidian → (optional) personal-site publish.

This document is the source of truth for component contracts. Individual component agents read it and implement against it.

## User decisions (final)

1. **Transcription**: Gemini 3 Flash Preview via `google-genai` Python SDK (model id: try `gemini-3-flash-preview` first; fall back to `gemini-2.5-flash` if not available)
2. **Hotkey**: `Win+Alt+Space` (global, works without PowerToys remap)
3. **Pattern**: Toggle — first press starts, second press stops
4. **Audio format**: FLAC, 16 kHz, mono, 16-bit
5. **Obsidian format**: Two files per recording
   - `<slug>.md` — CLEANED (stutters, fillers, false starts removed; punctuation+paragraphs added; **NO content changes, NO rephrasing, NO summarization**)
   - `<slug>.raw.md` — VERBATIM transcript
6. **Daily Notes**: auto-append a bullet under `## Voice Memos` heading in `Daily Notes/YYYY-MM-DD.md`. Create the daily note if missing.
7. **Publishing** (opt-in; disabled unless `NOTES_SITE_ROOT` is set — when unset, the tray does not start the publisher watcher): trigger on `#publish` tag in Obsidian frontmatter OR body. Watcher polls every 60s for newly-tagged notes. Cleaned transcript → **deterministic** reformat into the site's note format (no LLM call at publish time) → draft in `drafts/<slug>.md` → copy to `<site>/src/pages/notes/<slug>.md` → git commit + push → static host auto-deploys. The only LLM use in the pipeline is the Gemini transcript cleanup at recording time.
8. **Startup**: tray app auto-starts on Windows login via Task Scheduler
9. **Migration**: rename-only migration of pre-existing legacy transcripts (add YAML frontmatter, rename to ISO format, preserve raw text)
10. **Location**: all code lives at the repo root (`NP_ROOT`)

## Paths

All paths are resolved in `config.py` from environment variables (see
`.env.example`). The defaults below show the *shape*; override via `.env`.

```
# Code lives here (repo root)
NP_ROOT        = <repo root>

# Obsidian vault                      (env: NOTES_VAULT_ROOT)
VAULT_ROOT     = <vault root>
AUDIO_DIR      = <vault>/<NOTES_AUDIO_SUBDIR>            # default "Audio"
TRANSCRIPT_DIR = <audio>/<NOTES_TRANSCRIPT_SUBDIR>       # default "transcriptions"
DAILY_DIR      = <vault>/<NOTES_DAILY_SUBDIR>            # default "Daily Notes"

# Static site (optional)              (env: NOTES_SITE_ROOT)
SITE_ROOT          = <site root>
SITE_NOTES_DIR     = <site>/<NOTES_SITE_NOTES_SUBDIR>      # default "src/pages/notes"
SITE_CONSUMING_DIR = <site>/<NOTES_SITE_CONSUMING_SUBDIR>  # default "src/pages/consuming"

# Web clippings (optional)            (env: NOTES_CLIPPINGS_VAULT_SUBDIR)
CLIPPINGS_DIR        = <vault>/<NOTES_CLIPPINGS_VAULT_SUBDIR>   # default "Clippings" (never auto-created)
CLIPPING_PREVIEW_CHARS = <int>                                 # env NOTES_CLIPPING_PREVIEW_CHARS, default 1000

# Env file
ENV_FILE       = <repo root>/.env                        # contains GEMINI_API_KEY
```

## Folder structure to build

```
notes-pipeline/
├── SPEC.md                    # this file
├── README.md                  # user-facing docs
├── requirements.txt
├── .gitignore
├── config.py                  # central config (paths, hotkey, model id)
├── recorder/
│   ├── __init__.py
│   ├── tray.py                # main entrypoint: tray app + hotkey + mic
│   └── recorder.py            # sounddevice → FLAC writer
├── transcribe/
│   ├── __init__.py
│   ├── gemini_client.py       # wraps google-genai, handles file upload
│   ├── transcribe.py          # raw transcript
│   └── cleanup.py             # stutter/filler removal (no content changes)
├── obsidian/
│   ├── __init__.py
│   ├── writer.py              # write <slug>.md + <slug>.raw.md with YAML
│   ├── daily_note.py          # append to Daily Notes under ## Voice Memos
│   └── migrate_legacy.py      # one-shot rename + add frontmatter
├── publisher/
│   ├── __init__.py
│   ├── watcher.py             # polls TRANSCRIPT_DIR for #publish tag
│   └── publish.py             # deterministic reformat → drafts/ → site → git push
├── drafts/                    # staged drafts before publish
├── scripts/
│   ├── install.ps1            # pip install + Task Scheduler register
│   ├── run_tray.bat           # wrapper for Task Scheduler to call
│   └── uninstall.ps1
└── tests/
    ├── sample_audio/          # small test FLAC files
    ├── test_recorder.py
    ├── test_transcribe.py
    ├── test_obsidian.py
    └── test_publisher.py
```

## Filename conventions

- Audio: `<YYYY-MM-DD_HHMMSS>.flac` — plain ISO timestamp, sortable
- Obsidian transcripts (new, after slug generated): `<YYYY-MM-DD_HHMMSS>_<slug>.md` and `<YYYY-MM-DD_HHMMSS>_<slug>.raw.md`
- If slug generation fails, fall back to `<YYYY-MM-DD_HHMMSS>.md` / `.raw.md`
- Personal-site notes: `<slug-only>.md` (no date prefix; `created` frontmatter carries date)

## Contracts (Python type hints + data shapes)

### `recorder` → saves to disk, returns

```python
class RecordingResult(TypedDict):
    audio_path: Path        # .flac file
    sidecar_path: Path      # .json sidecar
    timestamp: str          # "2026-04-24_143208"
    duration_sec: float
    sample_rate: int        # 16000
    channels: int           # 1
```

Sidecar JSON schema:

```json
{
  "filename": "2026-04-24_143208.flac",
  "recorded_at": "2026-04-24T14:32:08-04:00",
  "duration_sec": 187.4,
  "sample_rate": 16000,
  "channels": 1,
  "codec": "flac",
  "device": "Microphone (Realtek Audio)",
  "hostname": "<your-hostname>",
  "hotkey": "win+alt+space",
  "pipeline_version": "1.0"
}
```

### `transcribe.transcribe(audio_path) -> TranscriptResult`

```python
class TranscriptResult(TypedDict):
    raw: str              # verbatim transcript with every um/uh
    cleaned: str          # stutters removed, punctuation added, NO content changes
    slug: str             # LLM-generated kebab-case, 3-5 words, for filename
    title: str            # human-readable title, Title Case, <60 chars
    tags: list[str]       # 2-4 content tags (lowercase, kebab-case, no `#`)
    duration_sec: float   # from audio probe
    model_used: str       # e.g. "gemini-3-flash-preview"
```

### `obsidian.writer.write_transcript(result, recording)`

Writes TWO files into `TRANSCRIPT_DIR`:

**`<timestamp>_<slug>.md`** (cleaned):
```markdown
---
title: "<title>"
aliases: ["<slug title>"]
date: 2026-04-24
time: "14:32:08"
duration: "3:07"
audio: "[[🎙 Audio/2026-04-24_143208.flac]]"
raw_transcript: "[[🎙 Audio/transcriptions/2026-04-24_143208_<slug>.raw]]"
source: voice-memo
tags: [voice-memo, <tag1>, <tag2>]
status: captured
model: <model_used>
---
![[🎙 Audio/2026-04-24_143208.flac]]

## Transcript
<cleaned transcript>
```

**`<timestamp>_<slug>.raw.md`** (verbatim):
```markdown
---
title: "<title> (raw transcript)"
date: 2026-04-24
time: "14:32:08"
duration: "3:07"
audio: "[[🎙 Audio/2026-04-24_143208.flac]]"
cleaned_transcript: "[[🎙 Audio/transcriptions/2026-04-24_143208_<slug>]]"
source: voice-memo-raw
tags: [voice-memo-raw]
---
## Transcript (verbatim)
<raw transcript>
```

### `obsidian.daily_note.append_memo_link(timestamp, slug, title, duration)`

Ensures `Daily Notes/YYYY-MM-DD.md` exists (create with minimal frontmatter if missing), then appends under `## Voice Memos` heading (create heading if missing):

```markdown
- 14:32 [[🎙 Audio/transcriptions/2026-04-24_143208_<slug>|<title>]] (3:07)
```

### Publisher watcher

Polls `TRANSCRIPT_DIR/*.md` AND `CLIPPINGS_DIR/*.md` (if the clippings dir
exists) every 60s. For any file where:
- YAML `tags` list contains `publish`, OR
- File body contains `#publish` as a tag

...AND the file has not yet been published (or its content hash changed; check
the local ledger `notes-pipeline/.published.json`), trigger the publish
pipeline. Sources under `CLIPPINGS_DIR` route to `publish_clipping` (and use the
clipping content hash, title + comment + body); everything else routes to
`publish_note` (title + transcript hash). The ledger and blocked ledger are
keyed by absolute source path and hold both kinds. Update ledger on success. If
`git push` fails, roll back and leave the file unmarked (or block it on an
unsafe-to-auto-recover state) and log.

### Publish pipeline

Publishing is opt-in: the watcher only runs when `NOTES_SITE_ROOT` is set. The
pipeline is **deterministic** — it does NOT call any LLM. (The Gemini LLM is
used only for transcript cleanup at transcription time.)

1. Read cleaned transcript markdown
2. Deterministically reformat the cleaned transcript into the site's note format (strip/rewrite frontmatter into Astro frontmatter, drop the audio embed, etc.) — no Gemini/LLM call
3. Write result to `drafts/<slug>.md` with Astro frontmatter
4. Check the site repo's git working tree is clean
5. Copy draft to `<site>/src/pages/notes/<slug>.md`
6. `git add src/pages/notes/<slug>.md && git commit -m "note: <title>" && git push origin main`
7. Record success in `.published.json` with timestamp + slug + site commit sha

### Clipping → /consuming flow (opt-in, deterministic, no LLM)

A PARALLEL flow to note publishing, also gated on `NOTES_SITE_ROOT`. The watcher
additionally scans `CLIPPINGS_DIR` (if it exists) each tick for `#publish`-tagged
Obsidian web clippings and routes them to `publisher.publish.publish_clipping`.

1. Parse the clipping frontmatter (`title`, `source` [URL], `author` [list of
   `[[wikilink]]`s or a string], `created` [date], `description`, and the
   comment field named by `NOTES_CLIPPING_COMMENT_FIELD`, default `comment`).
   A non-empty `title` is required.
2. Slug: Title-Case-hyphen (e.g. `AI-Tools-for-Existential-Security-EA-Forum`)
   via `_consuming_slugify`, collision-resolved against `SITE_CONSUMING_DIR`.
3. Build a deterministic `/consuming/<Slug>` page: frontmatter
   (`source: clipping`, `layout`, `backLink/backText`, `created`, `description`),
   then **your commentary** under `## My commentary` (omitted entirely when
   there's no comment), a `**Source:**` link + `By <authors> · clipped <date>`
   byline + a mirror disclaimer, then a **truncated preview** of the clipped
   body. Bodies longer than `CLIPPING_PREVIEW_CHARS` (env
   `NOTES_CLIPPING_PREVIEW_CHARS`, default `1000`) are cut at a clean
   paragraph/sentence/word boundary (never mid-word), rendered as normal
   markdown, then followed by a self-contained fade-out `<div>` (inline styles,
   gradient to `var(--color-bg)`) holding a "Read the full version at the
   source →" link. Bodies at/under the limit are shown in full with no fade/CTA.
   If truncated but no source URL was recorded, the fade `<div>` shows a plain
   "Full text not mirrored" note instead of a link.
4. Update the consuming `index.md`: insert/replace a
   `- **[Title](/consuming/Slug)** - description` bullet, grouped `## YYYY` →
   `### Month` (years + months descending), idempotent on republish, preserving
   the intro paragraph(s) and trailing structure comment.
5. Stage BOTH the page and the index, commit `consuming: <title>`, push once.
6. Content hash = `sha256(title + comment + body)` (separate from the
   transcript hash) so editing the comment OR the body republishes in place.
   Mirror the `published_at` writeback into the source clipping.

## Cleanup prompt (for `transcribe.cleanup.py`)

```
You will receive a verbatim voice-to-text transcript. Produce a CLEANED version with these strict rules:

REMOVE:
- Filler words used as vocal tics: "um", "uh", "like" (only when used as filler, not meaningful)
- Stutters: "I-I-I was going" → "I was going"
- Word repetitions from mispeaking: "I was, I was saying" → "I was saying"
- Abandoned false starts that were immediately restarted with different words: "I went to— I drove to the store" → "I drove to the store"

ADD:
- Punctuation (commas, periods, question marks, quotation marks)
- Capitalization at sentence starts
- Paragraph breaks at natural topic shifts or long pauses

DO NOT:
- Rephrase or "improve" wording
- Correct casual grammar that is intentional ("gonna", "kinda", "I seen")
- Summarize or shorten
- Reorder sentences or ideas
- Add interpretation, headers, bullet points
- Change vocabulary
- Fix factual errors the speaker made

The output should be the same words the speaker intended, just without disfluencies. Preserve the speaker's exact voice, tone, and meaning.

Return ONLY the cleaned transcript text. No headers, no commentary.
```

## Publishing (for `publisher/publish.py`)

The publish step is **deterministic** — it does not call any LLM. It reformats the already-cleaned transcript markdown into the site's note format (Astro frontmatter), writes a draft, copies it into the site repo, and commits/pushes. There is no publish-time prompt or style-reference note.

## Environment variables

Loaded from `<repo root>/.env` (see `.env.example` for the full list):
- `GEMINI_API_KEY` — Google AI Studio key (pipeline fails gracefully with a clear error if missing/expired)

## Running

```powershell
# Install
cd path\to\audio-transcriber-obsidian-notes
.\scripts\install.ps1    # pip install + register Task Scheduler task

# Manual run (for testing)
.\scripts\run_tray.bat

# Migrate legacy transcripts
python -m obsidian.migrate_legacy

# Uninstall
.\scripts\uninstall.ps1  # removes Task Scheduler task
```
