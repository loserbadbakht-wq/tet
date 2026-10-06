import gzip
import json
import re
import sys
import zlib

import requests
import feedparser
from newspaper import Article

FEED_URL = "https://www.zoomit.ir/feed/"
KEYWORD = "طاعون"
OUTPUT_FILE = "plague_news.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _maybe_decompress(raw: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower().strip()
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if encoding == "br":
            import brotli
            return brotli.decompress(raw)
        if encoding == "zstd":
            import zstandard as zstd
            return zstd.ZstdDecompressor().decompress(raw)
    except Exception as e:
        print(f"[warn] manual decompress ({encoding}) failed: {e}")
    return raw


def fetch_feed_bytes(url: str) -> bytes:
    # Ask only for encodings we can definitely decode in CI.
    headers = dict(HEADERS)
    headers["Accept-Encoding"] = "gzip, deflate"

    resp = requests.get(url, headers=headers, timeout=45, allow_redirects=True)
    print(f"[debug] HTTP status: {resp.status_code}")
    print(f"[debug] Final URL: {resp.url}")
    print(f"[debug] Content-Type: {resp.headers.get('Content-Type')}")
    print(f"[debug] Content-Encoding: {resp.headers.get('Content-Encoding')}")
    print(f"[debug] Content-Length: {len(resp.content)}")

    raw = resp.content

    # If requests didn't transparently decode, do it ourselves.
    content_encoding = resp.headers.get("Content-Encoding", "")
    if content_encoding and content_encoding.lower() not in ("", "identity"):
        if not raw.lstrip().startswith(b"<"):
            raw = _maybe_decompress(raw, content_encoding)

    # Final fallback: try Brotli if the payload still doesn't look like XML.
    if not raw.lstrip().startswith(b"<"):
        try:
            import brotli
            raw = brotli.decompress(raw)
            print("[debug] brotli decompress succeeded")
        except Exception as e:
            print(f"[debug] brotli fallback failed: {e}")

    print(f"[debug] First 200 bytes after decode: {raw[:200]!r}")
    resp.raise_for_status()
    return raw


# ---------------------------------------------------------------------------
# Cleaning / parsing
# ---------------------------------------------------------------------------
def clean_bytes(raw: bytes) -> bytes:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    # Remove control chars illegal in XML 1.0
    raw = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", raw)
    return raw


def parse_with_feedparser(raw: bytes):
    feed = feedparser.parse(raw)
    print(f"[debug] feedparser bozo={feed.bozo} entries={len(feed.entries)}")
    if feed.bozo:
        print(f"[debug] feedparser bozo_exception: {feed.bozo_exception}")

    items = []
    for e in feed.entries:
        items.append({
            "title": (e.get("title") or "").strip(),
            "creator": (e.get("author") or "").strip(),
            "pub_date": (e.get("published") or e.get("updated") or "").strip(),
            "link": (e.get("link") or "").strip(),
        })
    return items


def parse_with_regex(raw: bytes):
    """Last-resort parser that scans <item> blocks with regex."""
    text = raw.decode("utf-8", errors="replace")
    if "<item" not in text:
        print("[debug] no <item> tags found in text")
        return []

    blocks = re.findall(
        r"<item\b[^>]*>(.*?)</item>", text, flags=re.DOTALL | re.IGNORECASE
    )
    print(f"[debug] regex found {len(blocks)} <item> blocks")

    def pick(chunk: str, tag: str) -> str:
        m = re.search(
            rf"<{re.escape(tag)}\b[^>]*>(.*?)</{re.escape(tag)}>",
            chunk,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not m:
            return ""
        val = m.group(1)
        val = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", val, flags=re.DOTALL)
        val = re.sub(r"<[^>]+>", "", val)         # strip inner tags
        return val.strip()

    items = []
    for chunk in blocks:
        items.append({
            "title": pick(chunk, "title"),
            "creator": pick(chunk, "dc:creator"),
            "pub_date": pick(chunk, "pubDate"),
            "link": pick(chunk, "link"),
        })
    return items


# ---------------------------------------------------------------------------
# Article description
# ---------------------------------------------------------------------------
def extract_description(link: str) -> str:
    if not link:
        return ""
    try:
        article = Article(link, language="fa")
        article.download()
        article.parse()
        text = (article.text or "").strip()
        if len(text) > 2000:
            text = text[:2000] + "..."
        return text
    except Exception as e:
        print(f"[warn] article parse failed for {link}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    raw = fetch_feed_bytes(FEED_URL)

    # Guard: if we got HTML instead of XML (Cloudflare / error page), bail early.
    stripped = raw.lstrip()
    if stripped[:1] != b"<":
        print("[error] Response does not look like an RSS feed.")
        print("[error] Head:", raw[:500])
        sys.exit(1)

    cleaned = clean_bytes(raw)

    items = parse_with_feedparser(cleaned)
    if not items:
        print("[debug] feedparser returned 0 items, trying regex fallback...")
        items = parse_with_regex(cleaned)

    if not items:
        print("[error] Could not extract any items from the feed.")
        sys.exit(1)

    print(f"[debug] sample titles: {[i['title'][:40] for i in items[:5]]}")

    target = next((i for i in items if KEYWORD in i["title"]), None)
    if target is None:
        print(f"[info] No item with '{KEYWORD}' in title. Nothing to write.")
        return

    target["description"] = extract_description(target["link"])

    result = {
        "title": target["title"],
        "dc:creator": target["creator"],
        "pubDate": target["pub_date"],
        "link": target["link"],
        "description": target["description"],
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[ok] Wrote {OUTPUT_FILE}")
    print(json.dumps(
        {k: (v[:80] if isinstance(v, str) else v) for k, v in result.items()},
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
