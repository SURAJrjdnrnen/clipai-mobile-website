# ClipAI Mobile Website — MVP v0.1

A mobile-first web app that turns a long video into short vertical clips.

## What works in this starter

- Upload MP4, MOV, M4V, WEBM, MKV videos
- Speech transcription using Faster-Whisper (Hindi/English/auto)
- Transcript-based highlight scoring (MVP heuristic, not a guarantee of virality)
- Creates 9:16, 1080×1920 MP4 clips
- Burns captions into the video
- Adds simple automatic emojis to selected caption lines
- Three caption/effect presets: Punchy, Clean, Neon
- Preview and download clips from a phone browser

## Important limitations in v0.1

- The first version ranks moments using transcript hooks, emotion words, punctuation and speech density. It is an explainable MVP scorer, not a full large-language-model understanding of context, humour or audience retention.
- Reframing uses a centre crop. Face-aware tracking is a planned upgrade.
- Processing needs a server with FFmpeg and enough CPU/RAM. The first run downloads the Whisper model and can take longer.
- Generated files are stored on the server in `outputs/`. Add storage limits and automatic cleanup before public launch.
- Do not upload copyrighted or private videos unless you have permission.

## Run locally

You need Python 3.11+ and FFmpeg installed.

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

For better transcription accuracy, set `WHISPER_MODEL=small` before starting. The default is `base` to reduce server memory use.

## Deploy as a mobile website

This project includes a Dockerfile, so deploy it to a host that supports Docker (for example, a small VPS or a Docker-capable app host):

1. Create a GitHub repository and upload the contents of this folder.
2. On your hosting provider, create a new Web Service from that repository and choose Dockerfile deployment.
3. Set the service port to `8000`.
4. Keep a persistent disk mounted at `/app/outputs` if you want generated clips to survive restarts/redeploys.
5. Open the public HTTPS URL on Android Chrome. Use Chrome menu → “Add to Home screen” to make it feel like an app.

A free hosting tier may not have enough memory/CPU for video processing. Start with short test videos (1–3 minutes).

## API

- `GET /api/health`
- `POST /api/jobs` — multipart form fields: `video`, `language`, `min_seconds`, `max_seconds`, `max_clips`, `caption_style`
- `GET /api/jobs/{job_id}` — processing status and finished clip URLs

## Next upgrade ideas

1. LLM-based semantic highlight selection (optional API key)
2. Face detection and speaker-aware auto reframing
3. Animated word-by-word captions and richer emoji/effect presets
4. User accounts, upload limits, automatic file cleanup and persistent job storage
5. PWA install icon and offline shell
