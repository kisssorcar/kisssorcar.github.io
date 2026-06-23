#!/usr/bin/env python3
"""Fetch the public profile timeline for @koushik77 and write a static
JSON snapshot to assets/tweets.json.

We hit https://syndication.twitter.com/srv/timeline-profile/... — the same
endpoint that Twitter's official widgets.js script uses internally — and
extract the embedded __NEXT_DATA__ JSON. This requires no API key, no OAuth,
and no login, but cannot be done from a browser because the endpoint emits
no CORS headers. So we run it at build time from a GitHub Actions cron and
commit the resulting JSON to the repo. The static site then loads the JSON
and renders the tweets natively.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCREEN_NAME = "koushik77"
TIMELINE_URL = (
    f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{SCREEN_NAME}"
)
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "assets" / "tweets.json"
MAX_TWEETS = 30

# Only surface tweets related to KISS / KISS Sorcar on the homepage.
# Matched case-insensitively against the full_text. "KISS" is matched as a
# whole word (so "kissing", "kisser", etc. do not slip in by accident).
KISS_KEYWORD_RE = re.compile(
    r"(?<![A-Za-z])kiss(?![A-Za-z])|sorcar|kisssorcar|kiss[_\-]sorcar|fugu",
    re.IGNORECASE,
)


def is_kiss_related(tweet: dict) -> bool:
    """Return True if the tweet (or its quoted tweet) mentions KISS / Sorcar / Fugu."""
    text = tweet.get("full_text") or tweet.get("text") or ""
    if KISS_KEYWORD_RE.search(text):
        return True
    quoted = tweet.get("quoted_status")
    if quoted:
        qtext = quoted.get("full_text") or quoted.get("text") or ""
        if KISS_KEYWORD_RE.search(qtext):
            return True
    return False


def parse_created_at(value: str) -> datetime:
    """Parse Twitter's 'Fri Sep 03 17:49:40 +0000 2021' timestamp."""
    return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


def fetch_html(url: str) -> str:
    """Download the syndication profile HTML page."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_next_data(page_html: str) -> dict:
    """Pull the embedded Next.js __NEXT_DATA__ JSON blob out of the HTML."""
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', page_html, re.S
    )
    if not match:
        raise RuntimeError("__NEXT_DATA__ script tag not found in syndication HTML")
    return json.loads(match.group(1))


def render_text(tweet: dict) -> str:
    """Convert full_text + entities into safe HTML with linkified URLs/mentions/hashtags."""
    text = tweet.get("full_text") or tweet.get("text") or ""
    entities = tweet.get("entities") or {}

    # Expand t.co URLs into the original URL using entities.urls.
    for u in entities.get("urls") or []:
        short = u.get("url")
        display = u.get("display_url") or u.get("expanded_url") or short
        expanded = u.get("expanded_url") or short
        if short and expanded:
            text = text.replace(
                short,
                f'<a href="{html.escape(expanded)}" target="_blank" '
                f'rel="noopener">{html.escape(display)}</a>',
            )

    # Strip the trailing t.co media URL that points back into the tweet itself.
    for m in entities.get("media") or []:
        short = m.get("url")
        if short:
            text = text.replace(short, "")

    # Escape any remaining raw text segments (but not our injected <a> tags).
    parts = re.split(r"(<a [^>]+>[^<]*</a>)", text)
    escaped = []
    for part in parts:
        if part.startswith("<a "):
            escaped.append(part)
        else:
            escaped.append(html.escape(part))
    text = "".join(escaped)

    # Linkify @mentions and #hashtags in the now-escaped segments.
    text = re.sub(
        r"(^|[\s(])@(\w{1,15})",
        r'\1<a href="https://twitter.com/\2" target="_blank" rel="noopener">@\2</a>',
        text,
    )
    text = re.sub(
        r"(^|[\s(])#(\w+)",
        r'\1<a href="https://twitter.com/hashtag/\2" target="_blank" rel="noopener">#\2</a>',
        text,
    )

    # Preserve hard line breaks.
    text = text.replace("\n", "<br>")
    return text.strip()


def tweet_record(tweet: dict, retweeted: bool = False) -> dict:
    """Reduce a raw syndication tweet object to the fields we render."""
    user = tweet.get("user") or {}
    media_items = []
    for m in (tweet.get("entities") or {}).get("media") or []:
        url = m.get("media_url_https") or m.get("media_url")
        if url:
            media_items.append({"url": url, "type": m.get("type", "photo")})

    quoted = None
    if tweet.get("quoted_status"):
        quoted = tweet_record(tweet["quoted_status"])

    return {
        "id": tweet.get("id_str") or str(tweet.get("id")),
        "created_at": tweet.get("created_at"),
        "permalink": "https://twitter.com" + (tweet.get("permalink") or ""),
        "html": render_text(tweet),
        "author": {
            "screen_name": user.get("screen_name"),
            "name": user.get("name"),
            "avatar": user.get("profile_image_url_https"),
            "verified": bool(user.get("verified") or user.get("is_blue_verified")),
        },
        "stats": {
            "favorite": tweet.get("favorite_count", 0),
            "retweet": tweet.get("retweet_count", 0),
            "reply": tweet.get("reply_count", 0),
        },
        "media": media_items,
        "is_retweet": retweeted,
        "quoted": quoted,
    }


def extract_tweets(next_data: dict) -> list[dict]:
    """Walk the timeline entries and produce a list of rendered tweet records."""
    entries = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("timeline", {})
        .get("entries", [])
    )
    out: list[dict] = []
    for entry in entries:
        if entry.get("type") != "tweet":
            continue
        raw = (entry.get("content") or {}).get("tweet")
        if not raw:
            continue
        # Handle native retweets: the tweet has retweeted_status with the original.
        rt = raw.get("retweeted_status")
        source = rt if rt else raw
        # Only keep tweets related to KISS / KISS Sorcar / Fugu.
        if not is_kiss_related(source):
            continue
        if rt:
            rec = tweet_record(rt, retweeted=True)
            rec["retweeter"] = raw.get("user", {}).get("screen_name")
        else:
            rec = tweet_record(raw)
        out.append(rec)

    # Sort by created_at in descending order (newest first).
    out.sort(
        key=lambda r: parse_created_at(r["created_at"]) if r.get("created_at") else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return out[:MAX_TWEETS]


def main() -> int:
    """Fetch tweets and write the JSON snapshot to disk."""
    try:
        page_html = fetch_html(TIMELINE_URL)
        next_data = extract_next_data(page_html)
        tweets = extract_tweets(next_data)
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Failed to fetch tweets: {exc}", file=sys.stderr)
        # Don't blow away an existing good snapshot on a transient failure.
        if OUTPUT_PATH.exists():
            print("Keeping existing tweets.json untouched.", file=sys.stderr)
            return 0
        return 1

    # An empty filtered list is a legitimate outcome (no KISS-related tweets in
    # this snapshot). We still write the file so the UI shows a clean fallback.
    if not tweets:
        print("No KISS/Sorcar-related tweets found in this snapshot.", file=sys.stderr)

    payload = {
        "screen_name": SCREEN_NAME,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "syndication.twitter.com",
        "count": len(tweets),
        "tweets": tweets,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Wrote {len(tweets)} tweets to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
