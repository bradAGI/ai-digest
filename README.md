<p align="center">
  <img src="banner.png" alt="AI Digest" width="100%">
</p>

# AI Digest

**Daily AI news from Reddit, summarized by Gemini, delivered by Inkbox.**

No doomscrolling. No noise. Just the top AI posts, curated and summarized, in your inbox every day.

## How it works

1. **Scrapes** 6 AI subreddits every 6 hours (r/LocalLLaMA, r/MachineLearning, r/artificial, r/singularity, r/ClaudeAI, r/StableDiffusion)
2. **Filters** out memes, reposts, and low-effort content
3. **Summarizes** the top posts with Gemini (cascading fallback: 3-flash-preview → 2.5-flash → 2.0-flash → 2.0-flash-lite)
4. **Emails** a clean, categorized digest to all subscribers via Inkbox

## Subscribe

- **Web:** Visit the landing page and enter your email
- **Email:** Send anything to `aidigest@inkboxmail.com`
- **Unsubscribe:** Reply `stop` to any digest email

## Quick start

```bash
git clone https://github.com/bradAGI/ai-digest.git
cd ai-digest

# Configure
cp .env.example .env
# Edit .env with your keys

# Run
docker compose up
```

The server starts on port 8080 — landing page + subscription API + background digest loop.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `INKBOX_API_KEY` | Inkbox API key | required |
| `INKBOX_IDENTITY` | Inkbox agent handle | `aidigest` |
| `INKBOX_BASE_URL` | Inkbox API URL | `https://inkbox.ai` |
| `GEMINI_API_KEY` | Google AI Studio API key | required |
| `SEED_SUBSCRIBERS` | Comma-separated emails to auto-subscribe | optional |
| `SUBREDDITS` | Comma-separated subreddit names | `LocalLLaMA,MachineLearning,...` |
| `MIN_SCORE` | Minimum post score to include | `50` |
| `DIGEST_INTERVAL` | Seconds between digests | `21600` (6 hours) |

## Architecture

Single file (`digest.py`), five classes:

| Class | What it does |
|---|---|
| `RedditScraper` | Fetches Reddit JSON feeds, filters, dedupes |
| `DigestBuilder` | Gemini summarization with model fallback chain |
| `SubscriberStore` | SQLite subscriber management |
| `InboxHandler` | Subscribe/unsubscribe via email commands |
| `DigestMailer` | Sends formatted HTML digests via Inkbox |

FastAPI serves the landing page (`/`) and subscription API (`/api/subscribe`). Background thread runs the digest loop.

## Stack

- [Inkbox](https://inkbox.ai) — agent email identity (aidigest@inkboxmail.com)
- [Gemini](https://ai.google.dev) — free AI summarization
- [FastAPI](https://fastapi.tiangolo.com) — web server
- Reddit public JSON — no API key needed
