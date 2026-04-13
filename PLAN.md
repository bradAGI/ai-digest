# AI Digest — Daily AI News via Email

## What it is

Scrapes AI subreddits (LocalLLaMA, MachineLearning, etc.), uses Claude to filter and rank the best posts, emails you a clean daily digest.

## Flow

```
Cron (daily or every few hours)
  → Scrape Reddit JSON feeds (no API key needed)
  → Filter: remove memes, low-effort, duplicates
  → Claude ranks and summarizes top 10-15 posts
  → Email digest to you via Inkbox
```

## Sources

| Subreddit | Why |
|---|---|
| r/LocalLLaMA | Open source models, quantization, hardware |
| r/MachineLearning | Papers, research, industry news |
| r/artificial | General AI news |
| r/singularity | AGI hype, big announcements |
| r/ChatGPT | Product launches, use cases |
| r/ClaudeAI | Anthropic news |
| r/StableDiffusion | Image gen, ComfyUI |

## Architecture: `digest.py`

Three classes. ~150 lines.

### `RedditScraper`
- Fetches `https://www.reddit.com/r/{subreddit}/hot.json?limit=25` for each sub
- No API key needed — Reddit public JSON endpoints
- Extracts: title, url, score, num_comments, author, selftext preview
- Dedupes across subreddits (same link posted in multiple subs)
- Filters: score > 50, not a meme/image-only post

### `DigestBuilder`
- Takes raw posts from all subreddits
- Sends to Claude: "Rank these by importance for an AI engineer. Summarize each in 1-2 sentences. Group by category."
- Categories: Models & Releases, Research & Papers, Tools & Libraries, Industry News, Hardware
- Returns formatted HTML digest

### `DigestMailer`
- Inkbox email client (reuse pattern)
- Sends formatted HTML digest to configured recipients
- Tracks what was sent (avoid re-sending same posts)

## Email Output

```
Subject: AI Digest — April 13, 2026

🔥 Models & Releases
1. Llama 4 Scout beats GPT-4o on coding benchmarks (r/LocalLLaMA, 2.4k upvotes)
   New 17B model runs on consumer GPUs. GGUF quantizations already available.

2. Mistral releases Codestral 2 (r/MachineLearning, 1.8k upvotes)
   Open-weight code model, Apache 2.0 license. Outperforms Claude on HumanEval.

📄 Research & Papers
3. "Attention is All You Need" authors release follow-up (r/MachineLearning, 3.1k)
   Proposes replacement for softmax attention. 40% faster inference.

🛠 Tools & Libraries
4. New llama.cpp release adds speculative decoding (r/LocalLLaMA, 890)
   2x inference speed on supported models.

...
```

## Files

```
ai-digest/
├── digest.py          # all logic
├── Dockerfile
├── docker-compose.yml
├── requirements.txt   # inkbox, anthropic, httpx
├── .env               # INKBOX_API_KEY, ANTHROPIC_API_KEY, RECIPIENTS
└── .gitignore
```

## .env

```
INKBOX_API_KEY=...
INKBOX_IDENTITY=brad
INKBOX_BASE_URL=https://inkbox.ai
ANTHROPIC_API_KEY=...
RECIPIENTS=bradegan@gmail.com
SUBREDDITS=LocalLLaMA,MachineLearning,artificial,singularity,ClaudeAI
MIN_SCORE=50
DIGEST_INTERVAL=21600  # 6 hours in seconds
```

## Build time: ~45 min

| Step | Time | What |
|------|------|------|
| 1 | 5 min | Scaffolding |
| 2 | 15 min | RedditScraper — fetch + filter |
| 3 | 15 min | DigestBuilder — Claude ranking + formatting |
| 4 | 10 min | DigestMailer + main loop |

## Stretch

- Add Hacker News (news.ycombinator.com/best JSON)
- Add Twitter/X via Nitter RSS
- Add ArXiv recent papers in cs.AI
- Let recipients reply "more like #3" to tune future digests
