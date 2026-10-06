import gzip
import json
import re
import sys
import zlib

import requests
import feedparser
from newspaper import Article, Config
from bs4 import BeautifulSoup

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
# Tags supported by Telegram Rich Messages (Rich HTML style)
# Source: https://core.telegram.org/bots/API#rich-html-style
# ---------------------------------------------------------------------------
TELEGRAM_ALLOWED_TAGS = {
    # inline text formatting
    "a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "mark", "sub", "sup", "tg-spoiler",
    # block text
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "pre", "footer", "hr",
    "ul", "ol", "li",
    "blockquote", "aside", "cite",
    # media
    "img", "video", "audio", "tg-document",
    "figure", "figcaption",
    # rich media blocks
    "tg-collage", "tg-slideshow", "tg-map",
    # tables
    "table", "tr", "th", "td", "caption",
    # details / summaries
    "details", "summary",
    # math
    "tg-math", "tg-math-block",
    # buttons and interactive (preserved if present)
    "tg-button", "tg-button-row",
    # line break
    "br",
}

# Tags that are block-level and should get a newline after their closing tag
TELEGRAM_BLOCK_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "pre", "footer", "hr",
    "ul", "ol", "li",
    "blockquote", "aside",
    "figure", "figcaption",
    "tg-collage", "tg-slideshow",
    "table", "tr", "caption",
    "details", "summary",
    "tg-math-block",
}

# Attributes preserved per tag (only those meaningful for Telegram Rich HTML)
TELEGRAM_ATTR_WHITELIST = {
    "a":        ["href", "name"],
    "img":      ["src", "alt"],
    "video":    ["src"],
    "audio":    ["src"],
    "tg-document": ["src"],
    "tg-map":   ["lat", "long", "zoom"],
    "ol":       ["start", "type", "reversed"],
    "li":       ["value"],
    "td":       ["colspan", "rowspan", "align", "valign"],
    "th":       ["colspan", "rowspan", "align", "valign"],
    "table":    ["bordered", "striped", "compact"],
    "blockquote": ["expandable"],
    "details":  ["open"],
    "tg-spoiler": [],
    "tg-collage": [],
    "tg-slideshow": [],
    "tg-math":  [],
    "tg-math-block": [],
    "tg-button": ["type", "style", "url", "data", "text", "query",
                  "forward-text", "request-write-access"],
    "tg-button-row": ["align"],
    "tg-time":  ["unix", "format"],
    "tg-emoji": ["emoji-id"],
}

# Void elements: if unsupported, remove completely (no inner content)
VOID_UNSUPPORTED = {
    "iframe", "embed", "source", "track", "picture",
    "script", "style", "noscript", "template",
    "link", "meta", "base",
}

# Lazy-loading attribute names worth checking when src is missing
LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original",
                  "data-original-src", "data-url")
LAZY_SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset")


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
    headers = dict(HEADERS)
    headers["Accept-Encoding"] = "gzip, deflate"

    resp = requests.get(url, headers=headers, timeout=45, allow_redirects=True)
    print(f"[debug] HTTP status: {resp.status_code}")
    print(f"[debug] Final URL: {resp.url}")
    print(f"[debug] Content-Type: {resp.headers.get('Content-Type')}")
    print(f"[debug] Content-Encoding: {resp.headers.get('Content-Encoding')}")
    print(f"[debug] Content-Length: {len(resp.content)}")

    raw = resp.content

    content_encoding = resp.headers.get("Content-Encoding", "")
    if content_encoding and content_encoding.lower() not in ("", "identity"):
        if not raw.lstrip().startswith(b"<"):
            raw = _maybe_decompress(raw, content_encoding)

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
# Feed parsing
# ---------------------------------------------------------------------------
def clean_bytes(raw: bytes) -> bytes:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
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
        val = re.sub(r"<[^>]+>", "", val)
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
# Article extraction helpers
# ---------------------------------------------------------------------------
def build_newspaper_config() -> Config:
    cfg = Config()
    cfg.language = "fa"
    cfg.fetch_images = True
    cfg.keep_article_html = True
    cfg.request_timeout = 30
    cfg.browser_user_agent = HEADERS["User-Agent"]
    return cfg


def _normalize_media_url(url: str) -> str:
    if not url:
        return ""
    return url.split("?")[0].split("#")[0].rstrip("/").strip()


def _media_id(url: str) -> str:
    if not url:
        return ""
    path = url.split("?")[0].split("#")[0].rstrip("/")
    return path.rsplit("/", 1)[-1].strip()


def _pick_thumbnail(article: Article) -> str:
    if getattr(article, "top_image", None):
        return article.top_image.strip()

    meta = getattr(article, "meta_img", None)
    if meta:
        return meta.strip()

    for ns in ("og", "twitter"):
        data = article.meta_data.get(ns, {}) or {}
        for key in ("image", "image:src", "image:url"):
            val = data.get(key)
            if val:
                return str(val).strip()
    return ""


def _resolve_lazy_src(tag) -> str:
    src = tag.get("src")
    if src:
        return src.strip()
    for attr in LAZY_SRC_ATTRS:
        v = tag.get(attr)
        if v:
            return v.strip()
    for attr in LAZY_SRCSET_ATTRS:
        v = tag.get(attr)
        if v:
            return v.split(",")[0].strip().split(" ")[0]
    srcset = tag.get("srcset")
    if srcset:
        return srcset.split(",")[0].strip().split(" ")[0]
    return ""


# ---------------------------------------------------------------------------
# Telegram-Rich-compatible description builder
# ---------------------------------------------------------------------------
def _clean_html_for_telegram(html: str, thumbnail_url: str = "") -> str:
    """Keep only Telegram Rich Message supported tags, remove everything else."""
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Hard removal of script/style/noscript/template
    for bad in soup(["script", "style", "noscript", "template"]):
        bad.decompose()

    thumb_norm = _normalize_media_url(thumbnail_url)
    thumb_id = _media_id(thumbnail_url)

    for tag in soup.find_all(True):
        name = tag.name.lower()

        if tag.parent is None:
            continue

        # Remove unsupported void elements entirely
        if name in VOID_UNSUPPORTED:
            tag.decompose()
            continue

        # Unwrap tags that are not allowed (keep inner content)
        if name not in TELEGRAM_ALLOWED_TAGS:
            tag.unwrap()
            continue

        # ----- img: keep only src + alt, drop the thumbnail -----
        if name == "img":
            src = _resolve_lazy_src(tag)

            if not src:
                tag.decompose()
                continue

            src_norm = _normalize_media_url(src)
            src_id = _media_id(src)

            if thumb_norm and src_norm == thumb_norm:
                tag.decompose()
                continue
            if thumb_id and src_id and src_id == thumb_id:
                tag.decompose()
                continue

            new_attrs = {"src": src}
            alt = tag.get("alt")
            if alt:
                new_attrs["alt"] = alt
            tag.attrs = new_attrs
            continue

        # ----- video / audio / tg-document: keep only src -----
        if name in ("video", "audio", "tg-document"):
            src = _resolve_lazy_src(tag)
            if not src:
                tag.decompose()
                continue
            tag.attrs = {"src": src}
            continue

        # ----- Whitelist attributes for all other allowed tags -----
        allowed = TELEGRAM_ATTR_WHITELIST.get(name, [])
        new_attrs = {}
        for k in allowed:
            v = tag.get(k)
            if v is not None and v != "":
                new_attrs[k] = v
        tag.attrs = new_attrs

    # Serialize with newlines between block elements
    parts = []
    for child in soup.contents:
        rendered = str(child).strip()
        if not rendered:
            continue
        parts.append(rendered)

    html_str = "\n".join(parts)

    # Add a newline after every closing block tag
    block_re = "|".join(sorted(TELEGRAM_BLOCK_TAGS))
    html_str = re.sub(
        rf"</({block_re})>",
        lambda m: m.group(0) + "\n",
        html_str,
    )

    # Collapse excessive blank lines
    html_str = re.sub(r"[ \t]+\n", "\n", html_str)
    html_str = re.sub(r"\n{3,}", "\n\n", html_str)
    return html_str.strip()


def extract_article(link: str):
    """Return (thumbnail_url, telegram-compatible_html_description)."""
    if not link:
        return "", ""
    try:
        article = Article(link, config=build_newspaper_config())
        article.download()
        article.parse()

        thumb = _pick_thumbnail(article)

        html = ""
        try:
            html = article.article_html or ""
        except Exception:
            html = ""

        if not html:
            text = (article.text or "").strip()
            html = "\n".join(
                f"<p>{line}</p>" for line in text.split("\n") if line.strip()
            )

        description = _clean_html_for_telegram(html, thumb)
        return thumb, description
    except Exception as e:
        print(f"[warn] article parse failed for {link}: {e}")
        return "", ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    raw = fetch_feed_bytes(FEED_URL)

    if raw.lstrip()[:1] != b"<":
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

    thumbnail, description = extract_article(target["link"])

    result = {
        "title": target["title"],
        "dc:creator": target["creator"],
        "pubDate": target["pub_date"],
        "link": target["link"],
        "thumbnail": thumbnail,
        "description": description,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[ok] Wrote {OUTPUT_FILE}")
    preview = {
        "title": result["title"][:80],
        "dc:creator": result["dc:creator"][:80],
        "pubDate": result["pubDate"][:80],
        "link": result["link"][:120],
        "thumbnail": result["thumbnail"][:120],
        "description": result["description"][:500],
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
