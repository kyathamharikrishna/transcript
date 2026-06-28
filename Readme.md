# TranscribeFlow AI

TranscribeFlow AI is a Flask + Whisper web application for turning audio into timestamped transcripts, summaries, action items, subtitles, downloadable reports, and transcript Q&A.

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Flask](https://img.shields.io/badge/Flask-AI%20SaaS-black.svg)

- **GitHub Repository:** https://github.com/kyathamharikrishna/transcript
- **Render Live App:** https://transcribeflow-ai.onrender.com/

> Render hosts the real Flask backend. The included `render.yaml` uses Docker, installs FFmpeg, starts Gunicorn, and defaults to SQLite demo mode so the app can boot without a separate MySQL service.

## Deploy Live on Render

This repository includes:

- `Dockerfile` — installs Python dependencies and FFmpeg
- `render.yaml` — Render Blueprint for one-click deployment
- `/health` — health-check endpoint for Render
- `DB_BACKEND=sqlite` — demo database mode for live hosting without MySQL

### Render Steps

1. Open [Render](https://render.com/).
2. Click `New` → `Blueprint`.
3. Connect this repository: `kyathamharikrishna/transcript`.
4. Render will detect `render.yaml`.
5. Click `Apply`.
6. Wait for the Docker build to finish.
7. Open the generated Render URL.

Live app URL:

```text
https://transcribeflow-ai.onrender.com/
```

Render free instances can sleep after inactivity. First load after sleep may take a little while, and Whisper processing is CPU-heavy.

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

### Tier 3

- **Audio and transcript stats** — duration, word count, processing time, language, and action counts.
- **Copy-to-clipboard buttons** — copy transcript and summary in one click.
- **Confidence highlights** — low-confidence Whisper words are highlighted so users can review uncertain text.

## Tech Stack

- **Frontend:** HTML, CSS, JavaScript, Font Awesome, responsive glassmorphism UI
- **Backend:** Python, Flask, background threads, OpenAI Whisper
- **Database:** MySQL locally, SQLite demo mode on Render
- **AI/NLP:** Whisper ASR, extractive summarizer, optional Transformers summary, optional Anthropic Claude Q&A
- **Exports:** TXT report, JSON payload, SRT captions

## Project Structure

```text
transcript/
├── app.py
├── qa_engine.py
├── summarizer.py
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
   set ANTHROPIC_API_KEY=your_key_here
   ```

   `ANTHROPIC_API_KEY` is optional. Without it, transcript Q&A uses the local retrieval fallback.

7. Run the application.

   ```bash
   python app.py
   ```

8. Open http://127.0.0.1:5000.

## Database Notes

- `app.py` automatically creates missing tables and adds new columns for upgraded projects.
- Local development uses MySQL by default.
- Render uses SQLite demo mode through `DB_BACKEND=sqlite` in `render.yaml`.
- New passwords are stored with Werkzeug password hashing.
- Existing plaintext passwords are migrated to hashed passwords the next time the user logs in successfully.

## Output Files

- **Transcript TXT:** speaker-labelled timestamped transcript
- **Summary TXT:** concise AI summary
- **Combined Report TXT:** metadata, summary, action items, and transcript
- **JSON:** complete structured payload with stats, files, segments, and action items
- **SRT:** subtitle captions generated from Whisper timestamps

## Environment Options

- `WHISPER_MODEL` — Whisper model name, default `small`
- `WHISPER_FP16` — set `1` to enable FP16 on compatible GPUs
- `DB_BACKEND` — set `sqlite` for Render/demo mode or leave as `mysql` for local MySQL
- `SQLITE_DB_PATH` — SQLite database path when `DB_BACKEND=sqlite`
- `ENABLE_TRANSFORMERS_SUMMARY` — set `1` to enable optional BART summarization
- `ANTHROPIC_API_KEY` — enables Claude-powered transcript Q&A
- `ANTHROPIC_MODEL` — overrides the Claude model used by Q&A

## License

This project is licensed under the MIT License. See `LICENSE`.
