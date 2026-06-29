# TranscribeFlow AI

TranscribeFlow AI is a Flask + Whisper web application for turning audio into timestamped transcripts, translations, summaries, action items, subtitles, downloadable reports, safe speaker analytics, and transcript Q&A.

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Flask](https://img.shields.io/badge/Flask-AI%20SaaS-black.svg)

- **GitHub Repository:** https://github.com/kyathamharikrishna/transcript
- **Render Live App:** https://transcribeflow-ai.onrender.com/

> Render hosts the real Flask backend with a lightweight API transcription mode. The included `render.yaml` uses Docker, starts Gunicorn, stores records in SQLite, and sends audio to the OpenAI transcription API instead of loading Whisper/Torch on the free instance.

## Deploy Live on Render

This repository includes:

- `Dockerfile` — starts the lightweight Render web service
- `requirements-render.txt` — lightweight Render dependencies without Torch/Whisper
- `render.yaml` — Render Blueprint for one-click deployment
- `/health` — health-check endpoint for Render
- `DB_BACKEND=sqlite` — lightweight live database mode without MySQL
- `TRANSCRIPTION_BACKEND=openai` — Render-safe real transcription flow without Torch/Whisper memory crashes

### Render Steps

1. Open [Render](https://render.com/).
2. Click `New` → `Blueprint`.
3. Connect this repository: `kyathamharikrishna/transcript`.
4. Render will detect `render.yaml`.
5. Add a real secret environment variable `OPENAI_API_KEY` from your OpenAI account. Do not use placeholder values like `your_openai_key`.
6. Click `Apply`.
7. Wait for the Docker build to finish.
8. Open the generated Render URL.

Live app URL:

```text
https://transcribeflow-ai.onrender.com/
```

Render free instances can sleep after inactivity. First load after sleep may take a little while, and Whisper processing is CPU-heavy.

The Render free service uses `TRANSCRIPTION_BACKEND=openai` and `requirements-render.txt` to prevent memory-limit restarts. For local offline Whisper transcription, run with the default `TRANSCRIPTION_BACKEND=whisper`.

If uploads fail with `insufficient_quota`, the code is working but the configured OpenAI account has no available transcription credits. Fix it by adding OpenAI billing/credits, replacing `OPENAI_API_KEY` with a key that has quota, or deploying with the full `requirements.txt` and `TRANSCRIPTION_BACKEND=whisper` on a paid server with enough memory.

## Interview-Ready Features

### Tier 1

- **Timestamped transcript with speaker labels** — every Whisper segment is displayed like `[00:01:23] Speaker 1: ...`.
- **Keyword and action item extractor** — deadline, todo, follow-up, budget, and decision phrases are pulled into a separate action panel.
- **Q&A on transcript** — users can ask questions about their own transcript. If `ANTHROPIC_API_KEY` is configured, Claude answers with retrieved transcript context; otherwise a local retrieval fallback answers from matching transcript sections.

### Tier 2

- **Async processing with progress bar** — uploads return immediately and a background worker updates status while Whisper runs.
- **Transcription history dashboard** — previous transcripts show date, language, duration, word count, action count, and downloads.
- **SRT subtitle export** — timestamped segments are exported as `.srt` captions for creators and video workflows.
- **Language detection and forced language** — Whisper auto-detects language and users can force common languages from the dashboard.
- **Transcript translation** — users can translate Hindi, Telugu, Tamil, Kannada, Malayalam, English, and other supported transcripts into a convenient target language when `OPENAI_API_KEY` is configured.

### Tier 3

- **Audio and transcript stats** — duration, word count, processing time, language, and action counts.
- **Copy-to-clipboard buttons** — copy transcript and summary in one click.
- **Confidence highlights** — low-confidence Whisper words are highlighted so users can review uncertain text.
- **Voice-safe speaker profile** — shows detected language, speaking pace, transcript-mentioned countries, and a clear note that age or nationality is not guessed from voice.

### Frontend Experience

- **Advanced animated UI** — neon glassmorphism, responsive cards, rotating code-orbit visuals, cursor glow, reveal-on-scroll transitions, and real SaaS-style dashboard sections.
- **Production-minded UX** — accessible reduced-motion handling, live progress feedback, polished empty states, download actions, and mobile-responsive layouts.

## Tech Stack

- **Frontend:** HTML, CSS, JavaScript, Font Awesome, responsive glassmorphism UI
- **Backend:** Python, Flask, background threads, OpenAI Whisper
- **Database:** MySQL locally, SQLite live mode on Render
- **AI/NLP:** Whisper ASR, transcript translation, extractive summarizer, optional Transformers summary, optional Anthropic Claude Q&A
- **Exports:** TXT report, JSON payload, SRT captions

## Project Structure

```text
transcript/
├── app.py
├── qa_engine.py
├── summarizer.py
├── translator.py
├── transcription_features.py
├── schema.sql
├── requirements.txt
├── Dockerfile
├── render.yaml
├── docs/
├── static/
│   ├── css/app.css
│   ├── js/app.js
│   └── img/
├── templates/
│   ├── base.html
│   ├── dashboard.html
│   ├── history.html
│   ├── login.html
│   ├── register.html
│   └── result.html
└── uploads/transcriber/
    ├── audio/
    ├── transcript/
    ├── summary/
    ├── json/
    ├── combined/
    └── srt/
```

## Setup

1. Clone the repository.

   ```bash
   git clone https://github.com/kyathamharikrishna/transcript.git
   cd transcript
   ```

2. Create and activate a virtual environment.

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. Install dependencies.

   ```bash
   pip install -r requirements.txt
   ```

4. Install FFmpeg and make sure `ffmpeg` is available in your terminal path. Whisper requires it for audio/video decoding.

5. Create the MySQL database.

   ```sql
   CREATE DATABASE transcribeflow;
   USE transcribeflow;
   SOURCE schema.sql;
   ```

6. Configure environment variables as needed.

   ```bash
   set FLASK_SECRET_KEY=change-this-secret
   set DB_HOST=localhost
   set DB_USER=root
   set DB_PASSWORD=your_mysql_password
   set DB_NAME=transcribeflow
   set WHISPER_MODEL=small
   set OPENAI_API_KEY=your_openai_key
   set OPENAI_TRANSLATION_MODEL=gpt-4o-mini
   set ANTHROPIC_API_KEY=your_key_here
   ```

   `OPENAI_API_KEY` is required for Render transcription and optional transcript translation. `ANTHROPIC_API_KEY` is optional. Without it, transcript Q&A uses the local retrieval fallback.

7. Run the application.

   ```bash
   python app.py
   ```

8. Open http://127.0.0.1:5000.

## Database Notes

- `app.py` automatically creates missing tables and adds new columns for upgraded projects.
- Local development uses MySQL by default.
- Render uses SQLite mode through `DB_BACKEND=sqlite` in `render.yaml`.
- Local development uses real Whisper by default through `TRANSCRIPTION_BACKEND=whisper`.
- Render free uses `TRANSCRIPTION_BACKEND=openai` so login, upload, history, exports, and UI work without memory crashes.
- New passwords are stored with Werkzeug password hashing.
- Existing plaintext passwords are migrated to hashed passwords the next time the user logs in successfully.

## Output Files

- **Transcript TXT:** speaker-labelled timestamped transcript
- **Summary TXT:** concise AI summary
- **Combined Report TXT:** metadata, summary, translation, action items, speaker analytics, and transcript
- **JSON:** complete structured payload with stats, files, segments, translation, speaker profile, insights, and action items
- **SRT:** subtitle captions generated from Whisper timestamps

## Environment Options

- `WHISPER_MODEL` — Whisper model name, default `small`
- `WHISPER_FP16` — set `1` to enable FP16 on compatible GPUs
- `DB_BACKEND` — set `sqlite` for Render live mode or leave as `mysql` for local MySQL
- `SQLITE_DB_PATH` — SQLite database path when `DB_BACKEND=sqlite`
- `TRANSCRIPTION_BACKEND` — use `whisper` for local transcription, `openai` for Render live transcription, or `auto` to choose automatically
- `OPENAI_API_KEY` — required when `TRANSCRIPTION_BACKEND=openai`
- `OPENAI_FALLBACK_TO_WHISPER` — set `1` only on deployments that install Whisper/Torch and have enough memory for local fallback
- `OPENAI_TRANSCRIBE_MODEL` — defaults to `whisper-1` for timestamped API transcription
- `OPENAI_TRANSLATION_MODEL` — defaults to `gpt-4o-mini` for transcript translation
- `ENABLE_TRANSFORMERS_SUMMARY` — set `1` to enable optional BART summarization
- `ANTHROPIC_API_KEY` — enables Claude-powered transcript Q&A
- `ANTHROPIC_MODEL` — overrides the Claude model used by Q&A

## Speaker Analytics Note

The app intentionally does not infer a person's age or country/nationality from voice or accent. Those guesses are unreliable and risky in real products. Instead, TranscribeFlow reports safe, useful signals: detected language, speaking pace, low-confidence words, top keywords, and countries explicitly mentioned in the transcript.

## License

This project is licensed under the MIT License. See `LICENSE`.
