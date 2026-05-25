# audio-transcriber-obsidian-notes

Voice note → Gemini transcription → Obsidian vault → (optional) static-site
publish, all wired through a Windows tray app and a global hotkey.

Press `Win+Alt+Space`, talk, press `Win+Alt+Space` again. A cleaned transcript
and a verbatim copy land in your Obsidian vault, a bullet is appended to today's
Daily Note, and any note you tag with `#publish` gets cleaned again, copied
into your website repo, and pushed to git.

> **Platform:** Windows (uses a system-tray app + Task Scheduler). The
> transcription core is cross-platform, but the tray/auto-start glue is
> Windows-only. PRs to support macOS/Linux tray backends welcome.

---

## Pipeline summary

1. Transcription via **Gemini** (`gemini-3-flash-preview`, falls back to
   `gemini-2.5-flash`). Both model IDs are configurable.
2. Global hotkey: **`Win+Alt+Space`** (configurable) — no PowerToys remap needed.
3. Toggle pattern — first press starts, second press stops.
4. Audio: **FLAC, 16 kHz, mono, 16-bit**.
5. Two markdown files per recording:
   - `<slug>.md` — cleaned (fillers/stutters out, punctuation in, no rephrasing).
   - `<slug>.raw.md` — verbatim transcript with every "um".
6. Daily-note bullet auto-appended under `## Voice Memos`.
7. Tagging a note with `#publish` triggers a 60-second poll → LLM cleanup pass
   in your voice → draft → static-site copy → git commit + push.
8. Tray app auto-starts on Windows login via Task Scheduler.

---

## Requirements

- **Windows 10/11**
- **Python 3.11** (the installer prefers a pyenv-win 3.11.x build but falls
  back to `py -3.11` or whatever `python` is on `PATH`)
- A **Google AI Studio API key** — free at <https://aistudio.google.com/apikey>
- An **Obsidian vault** (or any folder where you want the markdown written)

---

## Quick start

```powershell
# 1. Clone
git clone https://github.com/MattB543/audio-transcriber-obsidian-notes.git
cd audio-transcriber-obsidian-notes

# 2. Configure (copy the example and edit it)
copy .env.example .env
notepad .env      # set GEMINI_API_KEY and NOTES_VAULT_ROOT at minimum

# 3. Install deps + register the auto-start tray task
.\scripts\install.ps1

# 4. Start it now (or just log out / back in)
Start-ScheduledTask -TaskName "notes-pipeline-tray"
```

Look for the gray circle in the system tray. Press `Win+Alt+Space` — it turns
red and beeps. Press again — it turns gray, beeps, and starts transcribing.

If PowerShell's execution policy blocks the installer:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

---

## Configuration

Everything machine-specific lives in a **`.env`** file in the repo root (it is
gitignored). Copy `.env.example` to `.env` and edit. Only `GEMINI_API_KEY` is
strictly required. Highlights:

| Variable | Required | Default | What it is |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | — | Google AI Studio key. |
| `NOTES_VAULT_ROOT` | recommended | `~/ObsidianVault` | Your Obsidian vault root. |
| `NOTES_AUDIO_SUBDIR` | | `Audio` | Subfolder for audio + transcripts (emoji names like `🎙 Audio` are fine). |
| `NOTES_TRANSCRIPT_SUBDIR` | | `transcriptions` | Subfolder for transcript markdown. |
| `NOTES_DAILY_SUBDIR` | | `Daily Notes` | Subfolder for daily notes. |
| `NOTES_SITE_ROOT` | optional | _(disabled)_ | Static-site repo to publish `#publish` notes into. Leave unset to disable publishing. |
| `NOTES_SITE_NOTES_SUBDIR` | | `src/pages/notes` | Where note markdown is dropped inside the site repo. |
| `NOTES_HOTKEY` | | `<cmd>+<alt>+<space>` | pynput hotkey combo (`<cmd>` = Win key). |
| `GEMINI_MODEL_PRIMARY` / `GEMINI_MODEL_FALLBACK` | | see `.env.example` | Override the models. |

See `.env.example` for the full list (audio sample rate, watcher interval, etc.).

All derived paths live in `config.py` — but you shouldn't need to edit code;
set env vars instead.

---

## How to use

1. Press `Win+Alt+Space` anywhere — the tray icon turns red and beeps.
2. Talk.
3. Press `Win+Alt+Space` again — the icon goes back to gray, you hear a lower
   beep, and the file gets shipped to Gemini in the background.
4. A few seconds later your vault gains:
   - `<timestamp>_<slug>.md` — cleaned transcript.
   - `<timestamp>_<slug>.raw.md` — verbatim sibling.
   - A new bullet under `## Voice Memos` in today's Daily Note.

To **publish** a note to your site, set `NOTES_SITE_ROOT` and add `#publish` to
the note's frontmatter `tags:` list or anywhere in the body. Within 60 s the
watcher picks it up, runs the publish-cleanup LLM pass, drops a draft in
`drafts/<slug>.md`, copies to `<site>/src/pages/notes/<slug>.md`, and pushes to
`origin/main`. (Publishing relies on your existing git credentials for that
repo — nothing is stored here.)

### Try it without a microphone

A short sample clip ships in `example-audio/test.flac`. Transcribe it directly:

```powershell
python -m transcribe.transcribe .\example-audio\test.flac
```

### Retry a failed transcription

If a recording's transcription failed (expired key, network blip, etc.), the
audio FLAC is still in your audio folder. Re-run by hand with:

```powershell
python -m transcribe.transcribe "<path-to>.flac"
```

### Migrate legacy transcripts

To rename and add frontmatter to pre-existing notes:

```powershell
python -m obsidian.migrate_legacy            # dry-run — shows what would change
python -m obsidian.migrate_legacy --apply    # apply
```

---

## Uninstall

```powershell
.\scripts\uninstall.ps1
```

Removes the scheduled task and kills any running tray process. Does **not**
touch pip packages, your Obsidian vault, transcripts, audio files, or `.env`.

---

## Logs

```
.logs\tray.log
```

Rotating, 1 MB per file × 5 backups. Tail it while developing:

```powershell
Get-Content .\.logs\tray.log -Wait
```

---

## Troubleshooting

**"GEMINI_API_KEY not set" / "API key expired"**
Get a fresh key at <https://aistudio.google.com/apikey> and put it in your
`.env` as `GEMINI_API_KEY=AIza...`.

**Microphone permission denied**
Settings → Privacy & security → Microphone → make sure "Let desktop apps
access your microphone" is on.

**Hotkey doesn't fire**
Another app may be hogging `Win+Alt+Space` (PowerToys, gaming overlays). Quit
suspects, then `Stop-ScheduledTask` + `Start-ScheduledTask` to reload the
listener. The fallback is the tray menu — right-click the icon for manual
start/stop. You can also change the combo via `NOTES_HOTKEY` in `.env`.

**Tray icon doesn't show up after login**
1. Open Task Scheduler → look for `notes-pipeline-tray`.
2. Right-click → Run. Watch `.logs\tray.log`.
3. Windows sometimes hides new tray icons behind the up-arrow until you drag
   them out.

**`pip install` fails on `pywin32`**
Run from an elevated PowerShell once:
`python -m pip install --upgrade pywin32` then retry `install.ps1`.

**Moved the repo to a new folder?**
Re-run `.\scripts\install.ps1`. The scheduled task is registered with the
folder's current path, so re-running re-points it. The hotkey and your vault
paths are unaffected by the move.

---

## Architecture

```
                 +--------------------+
   Win+Alt+Space |    pynput global   |
   ------------> |     hotkey hook    |
                 +---------+----------+
                           |
                           v
+-------------+    +-------+--------+    +------------------+
| pystray     |<---| TrayApp        |--->| AudioRecorder    |
| icon + menu |    | (recorder/tray)|    | (sounddevice ->  |
+-------------+    +-------+--------+    |  FLAC + sidecar) |
                           |             +---------+--------+
                           |                       |
              recording stops, dispatch in bg      |
                           |                       v
                           |              <vault>\<audio>\*.flac
                           v
                 +-----------------------+
                 | transcribe.transcribe |  (Gemini)
                 +---------+-------------+
                           |
                           v
                 +---------+----------+
                 | obsidian.writer    |  <- cleaned + raw markdown
                 | obsidian.daily_note|  <- bullet under ## Voice Memos
                 +---------+----------+
                           |
                           v   (#publish tag detected)
                 +---------+----------+
                 | publisher.watcher  |  (60s poll)
                 | publisher.publish  |  (LLM voice cleanup)
                 +---------+----------+
                           |
                           v
                 <site>\src\pages\notes\<slug>.md
                           |
                           v
                 git commit + push
```

Each block lives in its own package. `recorder/`, `transcribe/`, `obsidian/`,
and `publisher/` are independent — break one, the others keep working.

---

## Project layout

```
audio-transcriber-obsidian-notes/
├── README.md
├── SPEC.md                 (component contracts / design notes)
├── config.py               (reads .env; paths, hotkey, model ids)
├── .env.example            (copy to .env and fill in)
├── requirements.txt
├── recorder/               (tray, hotkey, audio capture)
├── transcribe/             (Gemini wrapper: raw + cleaned)
├── obsidian/               (markdown writer, daily-note appender, legacy migrator)
├── publisher/              (watcher + publish pipeline)
├── example-audio/          (a short sample clip for a no-mic test)
├── scripts/
│   ├── install.ps1         (pip install + register Task Scheduler task)
│   ├── uninstall.ps1       (remove task)
│   ├── run_tray.bat        (Task Scheduler entrypoint wrapper)
│   └── notes-pipeline-tray.xml  (fallback Task Scheduler XML)
└── tests/
```

---

## License

GPL-3.0 — see [LICENSE](LICENSE).
