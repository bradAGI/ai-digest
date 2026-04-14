#!/usr/bin/env python3
"""AI Digest — Scrapes AI subreddits, summarizes with Claude, emails subscribers."""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from google import genai
from inkbox import Inkbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ai-digest")

DB_PATH = Path(os.environ.get("DB_PATH", "/tmp/ai-digest.db"))
SEEN_FILE = Path("/tmp/ai-digest-seen.json")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Post:
    title: str
    url: str
    score: int
    comments: int
    subreddit: str
    author: str
    selftext: str
    permalink: str


# ---------------------------------------------------------------------------
# SubscriberStore
# ---------------------------------------------------------------------------

class SubscriberStore:
    def __init__(self, db_path: Path = DB_PATH):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                email TEXT PRIMARY KEY,
                frequency TEXT DEFAULT 'daily',
                status TEXT DEFAULT 'active',
                subscribed_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self._conn.commit()

    def add(self, email: str, frequency: str = "daily"):
        self._conn.execute(
            "INSERT OR REPLACE INTO subscribers (email, frequency, status) VALUES (?, ?, 'active')",
            (email.lower(), frequency),
        )
        self._conn.commit()
        log.info(f"Subscriber added: {email} ({frequency})")

    def remove(self, email: str):
        self._conn.execute(
            "UPDATE subscribers SET status = 'inactive' WHERE email = ?",
            (email.lower(),),
        )
        self._conn.commit()
        log.info(f"Subscriber removed: {email}")

    def get_active(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT email FROM subscribers WHERE status = 'active'"
        ).fetchall()
        return [r["email"] for r in rows]

    def is_subscribed(self, email: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM subscribers WHERE email = ?",
            (email.lower(),),
        ).fetchone()
        return row is not None and row["status"] == "active"

    def count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE status = 'active'"
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# RedditScraper
# ---------------------------------------------------------------------------

class RedditScraper:
    HEADERS = {"User-Agent": "ai-digest-bot/1.0"}

    def __init__(self, subreddits: list[str], min_score: int = 50):
        self.subreddits = subreddits
        self.min_score = min_score
        self._seen = self._load_seen()

    def scrape(self) -> list[Post]:
        posts = []
        for sub in self.subreddits:
            posts.extend(self._scrape_subreddit(sub))
        # Dedupe by URL
        seen_urls: set[str] = set()
        unique: list[Post] = []
        for p in posts:
            if p.url not in seen_urls:
                seen_urls.add(p.url)
                unique.append(p)
        # Filter already-sent posts
        new_posts = [p for p in unique if self._post_id(p) not in self._seen]
        new_posts.sort(key=lambda p: p.score, reverse=True)
        log.info(f"Scraped {len(new_posts)} new posts from {len(self.subreddits)} subreddits")
        return new_posts

    def scrape_without_filter(self) -> list[Post]:
        """Scrape posts ignoring the seen file — for generating digest on demand."""
        posts = []
        for sub in self.subreddits:
            posts.extend(self._scrape_subreddit(sub))
        seen_urls: set[str] = set()
        unique: list[Post] = []
        for p in posts:
            if p.url not in seen_urls:
                seen_urls.add(p.url)
                unique.append(p)
        unique.sort(key=lambda p: p.score, reverse=True)
        log.info(f"Scraped {len(unique)} posts (unfiltered) from {len(self.subreddits)} subreddits")
        return unique

    def mark_sent(self, posts: list[Post]):
        for p in posts:
            self._seen.add(self._post_id(p))
        self._save_seen()

    def _scrape_subreddit(self, subreddit: str) -> list[Post]:
        url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=25"
        try:
            resp = httpx.get(url, headers=self.HEADERS, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"Failed to scrape r/{subreddit}: {e}")
            return []

        posts = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            score = d.get("score", 0)
            if score < self.min_score:
                continue
            if d.get("is_video") or d.get("post_hint") == "image":
                continue
            post_url = d.get("url", "")
            if post_url.startswith("/r/") or "reddit.com" in post_url:
                post_url = f"https://www.reddit.com{d.get('permalink', '')}"
            posts.append(Post(
                title=d.get("title", ""),
                url=post_url,
                score=score,
                comments=d.get("num_comments", 0),
                subreddit=subreddit,
                author=d.get("author", ""),
                selftext=(d.get("selftext", "") or "")[:500],
                permalink=f"https://www.reddit.com{d.get('permalink', '')}",
            ))
        return posts

    def _post_id(self, post: Post) -> str:
        return hashlib.md5(post.permalink.encode()).hexdigest()

    def _load_seen(self) -> set[str]:
        if SEEN_FILE.exists():
            return set(json.loads(SEEN_FILE.read_text()))
        return set()

    def _save_seen(self):
        seen_list = list(self._seen)[-1000:]
        SEEN_FILE.write_text(json.dumps(seen_list))


# ---------------------------------------------------------------------------
# DigestBuilder
# ---------------------------------------------------------------------------

class DigestBuilder:
    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY", "")
        self._client = genai.Client(api_key=api_key) if api_key else None

    def build(self, posts: list[Post]) -> str:
        if not posts:
            return ""

        post_text = "\n".join(
            f"{i+1}. [{p.subreddit}] (score:{p.score}, comments:{p.comments}) {p.title}\n"
            f"   URL: {p.url}\n"
            f"   Preview: {p.selftext[:200]}"
            for i, p in enumerate(posts[:30])
        )

        prompt = (
            "You are an AI news editor curating a daily digest for AI engineers and AI developers. "
            "Your audience builds with LLMs, trains models, deploys inference, and ships AI products.\n\n"
            "Given these Reddit posts, create a ranked digest email. Rules:\n"
            "- ONLY include posts relevant to AI/ML engineers: new models, benchmarks, tools, libraries, "
            "frameworks, research papers, APIs, inference optimization, fine-tuning, deployment, hardware. "
            "Skip: memes, philosophical debates, AGI hype without substance, politics, career advice, "
            "basic tutorials, screenshots of chatbot conversations\n"
            "- Rank by importance to a working AI engineer. #1 is the most impactful story.\n"
            "- Use numbered ranking (1, 2, 3...) not categories. The ranking IS the organization.\n"
            "- For each post: numbered title as a link, subreddit + score on next line, "
            "then 1-2 sentence summary explaining why it matters to an engineer\n"
            "- Include 8-12 posts max. Quality over quantity.\n"
            "- Output clean HTML for email. Use <a> for linked titles, <p> for summaries, "
            "<span style='color:#888;font-size:12px'> for metadata. No <h1> or <h2> tags.\n"
            "- Keep it scannable — engineers read this on their phone in 2 minutes\n\n"
            f"Posts:\n{post_text}\n\n"
            "Output the HTML digest body only, no <html> or <body> tags."
        )

        if not self._client:
            log.warning("No GEMINI_API_KEY set, using fallback")
            return self._fallback_html(posts)

        models = ["gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]
        for model in models:
            for attempt in range(6):
                try:
                    log.info(f"Trying {model} (attempt {attempt + 1})")
                    response = self._client.models.generate_content(
                        model=model,
                        contents=prompt,
                    )
                    text = response.text.strip()
                    if text.startswith("```html"):
                        text = text[7:]
                    if text.startswith("```"):
                        text = text[3:]
                    if text.endswith("```"):
                        text = text[:-3]
                    return text.strip()
                except Exception as e:
                    if "429" in str(e):
                        wait = min(2 ** attempt * 5, 300)  # 5s, 10s, 20s, 40s, 80s, 160s
                        log.warning(f"{model} rate limited, retrying in {wait}s...")
                        time.sleep(wait)
                    else:
                        log.error(f"{model} failed: {e}")
                        break  # Try next model

        log.error("All Gemini models exhausted, using fallback")
        return self._fallback_html(posts)

    def _fallback_html(self, posts: list[Post]) -> str:
        items = "\n".join(
            f'<li><a href="{p.url}">{p.title}</a> '
            f'(r/{p.subreddit}, {p.score} pts, {p.comments} comments)</li>'
            for p in posts[:15]
        )
        return f"<h3>Top AI Posts</h3><ul>{items}</ul>"


# ---------------------------------------------------------------------------
# InboxHandler — subscribe/unsubscribe via email
# ---------------------------------------------------------------------------

class InboxHandler:
    SUBSCRIBE_PATTERNS = [
        r"(?i)^subscribe",
        r"(?i)^sign.?me.?up",
        r"(?i)^start",
        r"(?i)^yes",
    ]
    UNSUBSCRIBE_PATTERNS = [
        r"(?i)^unsubscribe",
        r"(?i)^stop",
        r"(?i)^cancel",
        r"(?i)^remove.?me",
    ]

    def __init__(self, identity, store: SubscriberStore):
        self._identity = identity
        self._store = store

    def process_inbox(self):
        for msg in self._identity.iter_unread_emails():
            if msg.direction != "inbound":
                continue
            sender = msg.from_address.lower()
            subject = (msg.subject or "").strip()
            snippet = (msg.snippet or "").strip()
            text = f"{subject} {snippet}".strip()

            self._identity.mark_emails_read([msg.id])

            if any(re.match(p, text) for p in self.UNSUBSCRIBE_PATTERNS):
                self._store.remove(sender)
                self._identity.send_email(
                    to=[sender],
                    subject="Unsubscribed from AI Digest",
                    body_html=(
                        "<p>You've been unsubscribed. No more digests.</p>"
                        "<p>Reply <b>subscribe</b> anytime to rejoin.</p>"
                    ),
                    in_reply_to_message_id=msg.id,
                )
                log.info(f"Unsubscribed: {sender}")

            elif any(re.match(p, text) for p in self.SUBSCRIBE_PATTERNS):
                self._store.add(sender)
                self._identity.send_email(
                    to=[sender],
                    subject="Subscribed to AI Digest!",
                    body_html=(
                        "<p>You're in! You'll get a daily AI news digest curated from Reddit.</p>"
                        "<p>Sources: r/LocalLLaMA, r/MachineLearning, r/artificial, r/singularity, r/ClaudeAI, r/StableDiffusion</p>"
                        "<p>Reply <b>stop</b> anytime to unsubscribe.</p>"
                    ),
                    in_reply_to_message_id=msg.id,
                )
                log.info(f"Subscribed: {sender}")

            else:
                # Unknown command — auto-subscribe and explain
                if not self._store.is_subscribed(sender):
                    self._store.add(sender)
                    self._identity.send_email(
                        to=[sender],
                        subject="Welcome to AI Digest!",
                        body_html=(
                            "<p>You've been subscribed to the daily AI news digest.</p>"
                            "<p>Every day you'll get the top AI posts from Reddit, "
                            "curated and summarized.</p>"
                            "<p>Reply <b>stop</b> anytime to unsubscribe.</p>"
                        ),
                        in_reply_to_message_id=msg.id,
                    )
                    log.info(f"Auto-subscribed: {sender}")


# ---------------------------------------------------------------------------
# DigestMailer
# ---------------------------------------------------------------------------

class DigestMailer:
    def __init__(self, identity):
        self._identity = identity

    def send(self, recipients: list[str], html_body: str):
        today = datetime.now().strftime("%B %d, %Y")
        subject = f"AI Digest — {today}"

        full_html = (
            "<div style='font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto;'>"
            f"<h1 style='border-bottom: 2px solid #0af; padding-bottom: 8px; color: #0af;'>AI Digest</h1>"
            f"<p style='color: #666;'>{today} | {len(recipients)} subscribers</p>"
            f"{html_body}"
            "<hr style='margin-top: 32px;'>"
            "<p style='color: #999; font-size: 12px;'>"
            "Scraped from Reddit. Summarized by Claude. Delivered by Inkbox.<br>"
            "Reply <b>stop</b> to unsubscribe.</p>"
            "</div>"
        )

        for recipient in recipients:
            try:
                self._identity.send_email(
                    to=[recipient],
                    subject=subject,
                    body_html=full_html,
                )
                log.info(f"Digest sent to {recipient}")
            except Exception as e:
                log.error(f"Failed to send to {recipient}: {e}")


# ---------------------------------------------------------------------------
# FastAPI Server + Webhook + Background Digest Timer
# ---------------------------------------------------------------------------

import threading
from contextlib import asynccontextmanager
from pathlib import Path as _Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, EmailStr

# Shared state
_identity = None
_store = None
_scraper = None
_builder = None
_mailer = None
_inkbox_client = None
_latest_digest_html = None  # cached latest digest for new subscribers


class SubscribeRequest(BaseModel):
    email: EmailStr


def _digest_timer(store, scraper, builder, mailer, interval):
    """Background thread: send digests on schedule. No polling."""
    global _latest_digest_html
    while True:
        time.sleep(interval)
        try:
            subscribers = store.get_active()
            posts = scraper.scrape()
            if posts:
                html = builder.build(posts)
                if html:
                    _latest_digest_html = html
                    if subscribers:
                        mailer.send(subscribers, html)
                        log.info(f"Digest sent to {len(subscribers)} subscribers")
                    scraper.mark_sent(posts)
            else:
                log.info("No new posts")
        except Exception as e:
            log.error(f"Digest error: {e}", exc_info=True)


def _send_latest_digest(email: str):
    """Send the latest digest to a new subscriber. Generate fresh if needed."""
    global _latest_digest_html
    if not _latest_digest_html and _scraper and _builder:
        log.info("No cached digest, generating fresh one for new subscriber...")
        posts = _scraper.scrape_without_filter()
        if posts:
            _latest_digest_html = _builder.build(posts)
    if _latest_digest_html and _mailer:
        _mailer.send([email], _latest_digest_html)
        log.info(f"Sent latest digest to new subscriber: {email}")


def _handle_inbound_email(sender: str, subject: str, snippet: str, message_id: str):
    """Process an inbound email via webhook — subscribe/unsubscribe."""
    sender = sender.lower()
    text = f"{subject} {snippet}".strip()

    unsub_patterns = [r"(?i)^stop", r"(?i)^unsubscribe", r"(?i)^cancel", r"(?i)^remove.?me"]
    sub_patterns = [r"(?i)^subscribe", r"(?i)^sign.?me.?up", r"(?i)^start", r"(?i)^yes"]

    if any(re.match(p, text) for p in unsub_patterns):
        _store.remove(sender)
        _identity.send_email(
            to=[sender],
            subject="Unsubscribed from AI Digest",
            body_html=(
                "<p>You've been unsubscribed. No more digests.</p>"
                "<p>Reply <b>subscribe</b> anytime to rejoin.</p>"
            ),
        )
        log.info(f"Webhook unsubscribe: {sender}")
    elif any(re.match(p, text) for p in sub_patterns):
        _store.add(sender)
        _identity.send_email(
            to=[sender],
            subject="Subscribed to AI Digest!",
            body_html=(
                "<p>You're in! You'll get a daily AI news digest curated from Reddit.</p>"
                "<p>Sources: r/LocalLLaMA, r/MachineLearning, r/artificial, r/singularity, r/ClaudeAI, r/StableDiffusion</p>"
                "<p>Reply <b>stop</b> anytime to unsubscribe.</p>"
            ),
        )
        log.info(f"Webhook subscribe: {sender}")
        _send_latest_digest(sender)
    else:
        if not _store.is_subscribed(sender):
            _store.add(sender)
            _identity.send_email(
                to=[sender],
                subject="Welcome to AI Digest!",
                body_html=(
                    "<p>You've been subscribed to the daily AI news digest.</p>"
                    "<p>Every day you'll get the top AI posts from Reddit, curated and summarized.</p>"
                    "<p>Reply <b>stop</b> anytime to unsubscribe.</p>"
                ),
            )
            log.info(f"Webhook auto-subscribe: {sender}")
            _send_latest_digest(sender)


def _register_webhook(identity, base_url, api_key, webhook_url):
    """Register webhook URL on the mailbox so Inkbox POSTs on inbound email."""
    email_address = identity.email_address
    try:
        import httpx as _httpx
        resp = _httpx.patch(
            f"{base_url}/api/v1/mail/mailboxes/{email_address}",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={"webhook_url": webhook_url},
        )
        resp.raise_for_status()
        log.info(f"Webhook registered: {webhook_url}")
    except Exception as e:
        log.error(f"Failed to register webhook: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _identity, _store, _scraper, _builder, _mailer, _inkbox_client, _latest_digest_html

    api_key = os.environ["INKBOX_API_KEY"]
    identity_handle = os.environ.get("INKBOX_IDENTITY", "aidigest")
    base_url = os.environ.get("INKBOX_BASE_URL", "https://inkbox.ai")
    subreddits = [s.strip() for s in os.environ.get("SUBREDDITS", "LocalLLaMA,MachineLearning").split(",")]
    min_score = int(os.environ.get("MIN_SCORE", "50"))
    interval = int(os.environ.get("DIGEST_INTERVAL", "86400"))
    seed_subscribers = [s.strip() for s in os.environ.get("SEED_SUBSCRIBERS", "").split(",") if s.strip()]
    webhook_url = os.environ.get("WEBHOOK_URL", "https://aidigest.fly.dev/webhook/email")

    _inkbox_client = Inkbox(api_key=api_key, base_url=base_url)
    _identity = _inkbox_client.get_identity(identity_handle)
    _store = SubscriberStore()
    _scraper = RedditScraper(subreddits, min_score)
    _builder = DigestBuilder()
    _mailer = DigestMailer(_identity)

    for email in seed_subscribers:
        if not _store.is_subscribed(email):
            _store.add(email)

    # Register webhook with Inkbox
    _register_webhook(_identity, base_url, api_key, webhook_url)

    # Generate initial digest so new subscribers get it immediately
    try:
        posts = _scraper.scrape_without_filter()
        if posts:
            _latest_digest_html = _builder.build(posts)
            log.info(f"Initial digest cached ({len(posts)} posts)")
    except Exception as e:
        log.error(f"Initial digest failed: {e}")

    log.info(f"AI Digest server starting. Digest interval: {interval}s")
    log.info(f"Webhook: {webhook_url}")
    log.info(f"Active subscribers: {_store.count()}")

    # Background thread for scheduled digests only (no polling)
    t = threading.Thread(
        target=_digest_timer,
        args=(_store, _scraper, _builder, _mailer, interval),
        daemon=True,
    )
    t.start()

    yield
    _inkbox_client.close()


app = FastAPI(lifespan=lifespan)

STATIC_DIR = _Path(__file__).parent


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/webhook/email")
async def email_webhook(request: Request):
    """Inkbox POSTs here on every inbound email. No polling needed."""
    body = await request.json()
    sender = body.get("from_address", body.get("from", ""))
    subject = body.get("subject", "")
    snippet = body.get("snippet", body.get("body_text", ""))
    message_id = body.get("id", body.get("message_id", ""))
    direction = body.get("direction", "inbound")

    if direction != "inbound" or not sender:
        return {"ok": True}

    # Mark as read
    try:
        if message_id:
            _identity.mark_emails_read([message_id])
    except Exception:
        pass

    _handle_inbound_email(sender, subject, snippet, message_id)
    return {"ok": True}


@app.post("/api/subscribe")
async def subscribe(req: SubscribeRequest):
    email = req.email.lower()
    if _store.is_subscribed(email):
        return JSONResponse({"status": "already_subscribed", "message": "You're already subscribed!"})

    _store.add(email)

    try:
        _identity.send_email(
            to=[email],
            subject="Welcome to AI Digest!",
            body_html=(
                "<div style='font-family: -apple-system, sans-serif; max-width: 600px;'>"
                "<h1 style='color: #00bbff;'>Welcome to AI Digest</h1>"
                "<p>You'll get a daily curated digest of the top AI posts from Reddit, "
                "summarized by Gemini.</p>"
                "<p><b>Sources:</b> r/LocalLLaMA, r/MachineLearning, r/artificial, "
                "r/singularity, r/ClaudeAI, r/StableDiffusion</p>"
                "<p>Reply <b>stop</b> anytime to unsubscribe.</p>"
                "</div>"
            ),
        )
    except Exception as e:
        log.error(f"Welcome email failed for {email}: {e}")

    log.info(f"Web subscribe: {email}")
    _send_latest_digest(email)
    return JSONResponse({"status": "subscribed", "message": "Subscribed! Check your inbox."})


@app.get("/api/stats")
async def stats():
    return {"subscribers": _store.count()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
