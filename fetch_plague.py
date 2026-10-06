import gzip
import json
import re
import sys
import zlib

import requests
import feedparser
from newspaper import Article, Config
from bs4 import BeautifulSoup, Tag

FEED_URL = "https://www.zoomit.ir/feed/"
KEYWORD = "طاعون"
OUTPUT_FILE = "plague_news.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/rss+xml,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

TARGET_IMAGE_WIDTH = 1920
TARGET_IMAGE_QUALITY = 75

TELEGRAM_ALLOWED_TAGS = {
    "a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "mark", "sub", "sup", "tg-spoiler",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "pre", "footer", "hr",
    "ul", "ol", "li",
    "blockquote", "aside", "cite",
    "img", "video", "audio", "tg-document",
    "figure", "figcaption",
    "tg-collage", "tg-slideshow", "tg-map",
    "table", "tr", "th", "td", "caption",
    "details", "summary",
    "tg-math", "tg-math-block",
    "tg-button", "tg-button-row",
    "br",
}

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

TELEGRAM_ATTR_WHITELIST = {
    "a":        ["href", "name"],
    "img":      ["src", "alt"],
    "video":    ["src", "poster"],
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
    "tg-spoiler": [], "tg-collage": [], "tg-slideshow": [],
    "tg-math": [], "tg-math-block": [],
    "tg-button": ["type", "style", "url", "data", "text", "query",
                  "forward-text", "request-write-access"],
    "tg-button-row": ["align"],
}

VOID_UNSUPPORTED = {
    "script", "style", "noscript", "template",
    "link", "meta", "base",
}

LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original",
                  "data-original-src", "data-url", "data-image")
LAZY_SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset")

PLACEHOLDER_PATTERNS = (
    "data:image", "placeholder", "blank.gif", "1x1.", "loading.gif",
    "/lazy.", "lazy.png", "spacer.gif", "transparent.png",
)

QUOTE_HINTS = (
    "quote", "blockquote", "pullquote", "pull-quote",
    "highlight", "callout", "standout",
    "نقل", "نقلقول", "نقل-قول", "گویه", "تاکید", "برجسته",
)
QUOTE_PAIRS = (
    ("«", "»"), ("‹", "›"), ("“", "”"), ("„", "“"),
    ('"', '"'), ("'", "'"),
)
MIN_STANDALONE_QUOTE_LEN = 15

JUNK_TAGS = {
    "nav", "aside", "form", "button",
    "input", "select", "textarea", "label", "fieldset",
}

JUNK_CLASS_PATTERNS = (
    "comment", "related", "recommend", "promo", "advert", "banner",
    "breadcrumb", "table-of-content", "tableofcontent", "toc-",
    "share", "social", "bookmark", "like-button", "rating", "rate-",
    "newsletter", "subscribe", "byline", "bio",
    "sidebar", "menu", "toolbar", "pagination", "pager",
    "tag-list", "tags-list",
    "cookie", "consent", "popup", "modal", "overlay",
)

JUNK_TEXT = (
    "کپی لینک",
    "تبلیغات",
    "بیشتر بخوانید",
    "مقاله رو دوست داشتی؟",
    "نظرت چیه؟",
    "ارسال نظر",
    "بوکمارک",
    "اشتراک‌گذاری",
    "دنبال کردن",
    "به محتوای این مطلب چه امتیازی میدی؟",
    "نظر شما برامون مهمه",
    "در حال مطالعه لیست مطالعاتی هستی",
    "راهنمای بیماری‌ها و مشکلات پزشکی",
    "مشاهده همه ویدئو‌ها",
)

RELATED_HINTS = (
    "مطالعه '", "مطالعه",
    "دقیقه قبل", "ساعت قبل",
    "روز قبل", "هفته قبل",
)


# ---------------------------------------------------------------------------
# HTTP
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


def _request(url: str) -> bytes:
    headers = dict(HEADERS)
    headers["Accept-Encoding"] = "gzip, deflate"
    resp = requests.get(url, headers=headers, timeout=45, allow_redirects=True)
    print(f"[debug] GET {url} -> {resp.status_code} "
          f"({resp.headers.get('Content-Type')}, "
          f"enc={resp.headers.get('Content-Encoding')}, "
          f"{len(resp.content)}B)")
    raw = resp.content
    enc = resp.headers.get("Content-Encoding", "")
    if enc and enc.lower() not in ("", "identity") and not raw.lstrip().startswith(b"<"):
        raw = _maybe_decompress(raw, enc)
    if not raw.lstrip().startswith(b"<"):
        try:
            import brotli
            raw = brotli.decompress(raw)
        except Exception:
            pass
    resp.raise_for_status()
    return raw


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------
def clean_bytes(raw: bytes) -> bytes:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", raw)


def parse_with_feedparser(raw: bytes):
    feed = feedparser.parse(raw)
    print(f"[debug] feedparser bozo={feed.bozo} entries={len(feed.entries)}")
    if feed.bozo:
        print(f"[debug] bozo_exception: {feed.bozo_exception}")
    return [
        {
            "title": (e.get("title") or "").strip(),
            "creator": (e.get("author") or "").strip(),
            "pub_date": (e.get("published") or e.get("updated") or "").strip(),
            "link": (e.get("link") or "").strip(),
        }
        for e in feed.entries
    ]


def parse_with_regex(raw: bytes):
    text = raw.decode("utf-8", errors="replace")
    blocks = re.findall(r"<item\b[^>]*>(.*?)</item>", text,
                        flags=re.DOTALL | re.IGNORECASE)
    print(f"[debug] regex found {len(blocks)} <item> blocks")

    def pick(chunk, tag):
        m = re.search(rf"<{re.escape(tag)}\b[^>]*>(.*?)</{re.escape(tag)}>",
                      chunk, flags=re.DOTALL | re.IGNORECASE)
        if not m:
            return ""
        val = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.DOTALL)
        return re.sub(r"<[^>]+>", "", val).strip()

    return [
        {
            "title": pick(c, "title"),
            "creator": pick(c, "dc:creator"),
            "pub_date": pick(c, "pubDate"),
            "link": pick(c, "link"),
        }
        for c in blocks
    ]


# ---------------------------------------------------------------------------
# Article body extraction
# ---------------------------------------------------------------------------
def build_newspaper_config() -> Config:
    cfg = Config()
    cfg.language = "fa"
    cfg.fetch_images = True
    cfg.keep_article_html = True
    cfg.request_timeout = 30
    cfg.browser_user_agent = HEADERS["User-Agent"]
    return cfg


PRIORITY_KEYS = ("body", "content", "html", "articleBody", "text",
                 "story", "article", "post", "richText", "description")


def _longest_html_in_json(obj, depth: int = 0, max_depth: int = 25) -> str:
    if depth > max_depth:
        return ""
    if isinstance(obj, str):
        if len(obj) >= 200 and "<p" in obj and "</p>" in obj:
            return obj
        return ""
    best = ""
    if isinstance(obj, dict):
        for key in PRIORITY_KEYS:
            if key in obj:
                c = _longest_html_in_json(obj[key], depth + 1, max_depth)
                if len(c) > len(best):
                    best = c
        for k, v in obj.items():
            if k in PRIORITY_KEYS:
                continue
            c = _longest_html_in_json(v, depth + 1, max_depth)
            if len(c) > len(best):
                best = c
    elif isinstance(obj, list):
        for v in obj:
            c = _longest_html_in_json(v, depth + 1, max_depth)
            if len(c) > len(best):
                best = c
    return best


def _extract_schema_article_body(soup) -> str:
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("@type") in ("NewsArticle", "Article", "BlogPosting"):
                body = node.get("articleBody")
                if isinstance(body, str) and len(body) > 200:
                    return body
    return ""


def _score_container(el: Tag) -> int:
    paragraphs = el.find_all("p")
    text_len = len(el.get_text(" ", strip=True))
    media_count = len(el.find_all(["img", "figure", "video"]))
    score = len(paragraphs) * 200 + text_len + media_count * 300
    text = el.get_text(" ", strip=True)
    for junk in ("تبلیغات", "بیشتر بخوانید", "کپی لینک", "ارسال نظر"):
        if junk in text:
            score -= 5000
    return score


def _best_dom_container(soup) -> str:
    selectors = [
        '[data-testid="article-body"]',
        '[class*="articleBody"]',
        '[class*="article-body"]',
        '[class*="articleContent"]',
        '[class*="article-content"]',
        '[class*="story-body"]',
        "article",
        "main",
    ]
    best, best_score = None, 0
    for sel in selectors:
        for el in soup.select(sel):
            score = _score_container(el)
            if score > best_score:
                best, best_score = el, score
    if best is not None and best_score > 200:
        return str(best)
    return ""


def _extract_next_data(soup) -> str:
    nd = soup.find("script", id="__NEXT_DATA__")
    if not nd or not nd.string:
        return ""
    try:
        data = json.loads(nd.string)
    except Exception as e:
        print(f"[warn] __NEXT_DATA__ parse failed: {e}")
        return ""
    return _longest_html_in_json(data)


def _extract_next_f(soup) -> str:
    chunks = []
    for s in soup.find_all("script"):
        txt = s.string or ""
        if "__next_f" not in txt:
            continue
        for m in re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
                             txt, flags=re.DOTALL):
            raw = m.group(1)
            try:
                decoded = bytes(raw, "utf-8").decode("unicode_escape")
            except Exception:
                decoded = raw
            chunks.append(decoded)
    if not chunks:
        return ""
    blob = "".join(chunks)
    matches = re.findall(r"(?:<p\b[^>]*>.*?</p>\s*){3,}", blob, flags=re.DOTALL)
    if not matches:
        return ""
    return max(matches, key=len)


def extract_article_body(link: str):
    if not link:
        return "", ""

    try:
        page_html = _request(link).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[warn] page fetch failed: {e}")
        return "", ""

    soup = BeautifulSoup(page_html, "html.parser")

    thumb = ""
    for prop in ("og:image", "twitter:image", "twitter:image:src"):
        m = (soup.find("meta", attrs={"property": prop})
             or soup.find("meta", attrs={"name": prop}))
        if m and m.get("content"):
            thumb = m["content"].strip()
            break

    candidates = []

    nd_html = _extract_next_data(soup)
    if nd_html:
        candidates.append(("next_data", nd_html))
        print(f"[debug] __NEXT_DATA__ -> {len(nd_html)} chars")

    nf_html = _extract_next_f(soup)
    if nf_html:
        candidates.append(("next_f", nf_html))
        print(f"[debug] __next_f -> {len(nf_html)} chars")

    dom_html = _best_dom_container(soup)
    if dom_html:
        candidates.append(("dom", dom_html))
        print(f"[debug] DOM container -> {len(dom_html)} chars")

    try:
        a = Article(link, config=build_newspaper_config())
        a.set_html(page_html)
        a.parse()
        if not thumb and getattr(a, "top_image", None):
            thumb = a.top_image.strip()
        if a.article_html:
            candidates.append(("newspaper", a.article_html))
            print(f"[debug] newspaper -> {len(a.article_html)} chars")
    except Exception as e:
        print(f"[warn] newspaper failed: {e}")

    if not candidates:
        schema_text = _extract_schema_article_body(soup)
        if schema_text:
            html = "\n".join(
                f"<p>{line}</p>"
                for line in schema_text.split("\n") if line.strip()
            )
            return thumb, html
        return thumb, ""

    best = max(candidates, key=lambda c: len(c[1]))
    print(f"[debug] chosen: {best[0]} ({len(best[1])} chars); "
          f"all={[(n, len(h)) for n, h in candidates]}")
    return thumb, best[1]


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def _normalize_media_url(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/").strip() if url else ""


def _media_id(url: str) -> str:
    if not url:
        return ""
    path = url.split("?")[0].split("#")[0].rstrip("/")
    return path.rsplit("/", 1)[-1].strip()


def _is_placeholder(url: str) -> bool:
    if not url:
        return True
    low = url.lower()
    return any(p in low for p in PLACEHOLDER_PATTERNS)


def _upgrade_image_size(url: str) -> str:
    if not url or "api2.zoomit.ir/media" not in url:
        return url
    if re.search(r"[?&]w=\d+", url):
        url = re.sub(r"([?&])w=\d+", rf"\g<1>w={TARGET_IMAGE_WIDTH}", url)
    else:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}w={TARGET_IMAGE_WIDTH}"
    if re.search(r"[?&]q=\d+", url):
        url = re.sub(r"([?&])q=\d+", rf"\g<1>q={TARGET_IMAGE_QUALITY}", url)
    else:
        url = f"{url}&q={TARGET_IMAGE_QUALITY}"
    return url


def _pick_largest_from_srcset(srcset: str) -> str:
    best_url, best_w = "", -1
    for part in (srcset or "").split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split()
        url = pieces[0]
        w = 0
        if len(pieces) > 1:
            m = re.match(r"(\d+)w", pieces[1])
            if m:
                w = int(m.group(1))
        if w > best_w:
            best_w, best_url = w, url
    return best_url


def _resolve_lazy_src(tag) -> str:
    for attr in LAZY_SRC_ATTRS:
        v = tag.get(attr)
        if v:
            v = v.strip()
            if v and not _is_placeholder(v):
                return _upgrade_image_size(v)
    for attr in ("srcset",) + LAZY_SRCSET_ATTRS:
        v = tag.get(attr)
        if v:
            best = _pick_largest_from_srcset(v)
            if best and not _is_placeholder(best):
                return _upgrade_image_size(best)
    src = (tag.get("src") or "").strip()
    if src and not _is_placeholder(src):
        return _upgrade_image_size(src)
    for attr in LAZY_SRC_ATTRS:
        v = tag.get(attr)
        if v and v.strip():
            return _upgrade_image_size(v.strip())
    return ""


# ---------------------------------------------------------------------------
# Quote handling
# ---------------------------------------------------------------------------
def _is_quote_element(tag: Tag) -> bool:
    if not isinstance(tag, Tag):
        return False
    if tag.name == "blockquote":
        return True
    classes = " ".join(tag.get("class") or []).lower()
    eid = (tag.get("id") or "").lower()
    role = (tag.get("role") or "").lower()
    data_blob = " ".join(
        f"{k}={v}" for k, v in tag.attrs.items()
        if k.startswith("data-") and isinstance(v, str)
    ).lower()
    blob = f"{classes} {eid} {role} {data_blob}"
    return any(h in blob for h in QUOTE_HINTS)


def _looks_like_standalone_quote(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < MIN_STANDALONE_QUOTE_LEN:
        return False
    for opener, closer in QUOTE_PAIRS:
        if t.startswith(opener) and t.endswith(closer):
            inner = t[len(opener):-len(closer)].strip()
            if opener not in inner and closer not in inner:
                return True
    return False


def _promote_quotes(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(True):
        if tag.name in ("html", "body"):
            continue
        if _is_quote_element(tag) and tag.name != "blockquote":
            tag.name = "blockquote"
            tag.attrs = {k: v for k, v in tag.attrs.items() if k == "expandable"}
    for p in soup.find_all("p"):
        if p.find("blockquote"):
            continue
        if _looks_like_standalone_quote(p.get_text(" ", strip=True)):
            p.wrap(soup.new_tag("blockquote"))


def _promote_repeated_pull_quotes(soup: BeautifulSoup) -> None:
    """Zoomit shows pull-quotes twice: once as a standalone <p> and
    again inline inside a longer paragraph. Detect and wrap the
    standalone one in <blockquote>."""
    paragraphs = [(p, p.get_text(" ", strip=True))
                  for p in soup.find_all("p")]

    for i, (p, text) in enumerate(paragraphs):
        if not text or len(text) < 30 or len(text) > 400:
            continue
        if p.find_parent("blockquote"):
            continue

        for j, (other, other_text) in enumerate(paragraphs):
            if i == j:
                continue
            if text in other_text and len(other_text) > len(text) + 20:
                bq = soup.new_tag("blockquote")
                p.wrap(bq)
                print(f"[debug] pull-quote detected: {text[:60]}...")
                break


# ---------------------------------------------------------------------------
# Junk pruning
# ---------------------------------------------------------------------------
def _has_junk_attr(tag) -> bool:
    if not isinstance(tag, Tag):
        return False
    blob = " ".join(
        filter(None, [
            " ".join(tag.get("class") or []),
            tag.get("id") or "",
            tag.get("role") or "",
            " ".join(f"{k}={v}" for k, v in tag.attrs.items()
                     if k.startswith("data-") and isinstance(v, str)),
        ])
    ).lower()
    return any(p.lower() in blob for p in JUNK_CLASS_PATTERNS)


def _contains_media(tag) -> bool:
    if not isinstance(tag, Tag):
        return False
    return tag.find(["img", "video", "audio", "figure", "picture"]) is not None


def _looks_like_related_card(tag) -> bool:
    if not isinstance(tag, Tag):
        return False
    if tag.name not in ("li", "div", "section", "article", "a"):
        return False
    if tag.find("p"):
        return False
    imgs = tag.find_all("img")
    if len(imgs) != 1:
        return False
    if not tag.find("a", href=True):
        return False
    text = tag.get_text(" ", strip=True)
    if len(text) > 400:
        return False
    return any(h in text for h in RELATED_HINTS)


def _prune_junk(soup: BeautifulSoup) -> None:
    before = len(soup.get_text(" ", strip=True))

    for tag in soup.find_all(JUNK_TAGS):
        if _contains_media(tag):
            tag.unwrap()
        else:
            tag.decompose()

    for tag in soup.find_all(_has_junk_attr):
        if _contains_media(tag):
            tag.unwrap()
        else:
            tag.decompose()

    for tag in soup.find_all(["li", "div", "section", "article", "a"]):
        if tag.parent is None:
            continue
        if _looks_like_related_card(tag):
            tag.decompose()

    for tag in soup.find_all(["p", "div", "span", "li", "h2", "h3"]):
        if tag.parent is None:
            continue
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        if len(text) <= 60 and any(j in text for j in JUNK_TEXT):
            tag.decompose()

    for tag in soup.find_all(True):
        if tag.parent is None:
            continue
        if tag.name in ("div", "span", "section", "article"):
            if not tag.get_text(strip=True) and not _contains_media(tag):
                tag.decompose()

    after = len(soup.get_text(" ", strip=True))
    print(f"[debug] prune: {before} -> {after} chars "
          f"(removed {before - after})")


# ---------------------------------------------------------------------------
# Media conversion
# ---------------------------------------------------------------------------
def _convert_media(tag, soup) -> None:
    name = tag.name.lower()

    if name == "img":
        src = _resolve_lazy_src(tag)
        if not src:
            tag.decompose()
            return
        new = {"src": src}
        alt = tag.get("alt")
        if alt:
            new["alt"] = alt.strip()
        tag.attrs = new
        return

    if name in ("video", "audio"):
        src = _resolve_lazy_src(tag)
        if not src:
            source_child = tag.find("source")
            if source_child is not None:
                src = _resolve_lazy_src(source_child)
        if not src:
            tag.decompose()
            return
        new = {"src": _upgrade_image_size(src) if name == "video" else src}
        if name == "video":
            poster = tag.get("poster")
            if poster and not _is_placeholder(poster):
                new["poster"] = _upgrade_image_size(poster.strip())
        tag.attrs = new
        for s in tag.find_all("source"):
            s.decompose()
        return

    if name == "iframe":
        src = (tag.get("src") or tag.get("data-src") or "").strip()
        if not src:
            tag.decompose()
            return
        a = soup.new_tag("a", href=src)
        a.string = f"🔗 {src}"
        tag.replace_with(a)
        return

    if name == "picture":
        img = tag.find("img")
        if img is not None:
            _convert_media(img, soup)
            tag.replace_with(img)
        else:
            src = None
            for s in tag.find_all("source"):
                src = _resolve_lazy_src(s)
                if src:
                    break
            if src:
                new_img = soup.new_tag("img", src=src)
                tag.replace_with(new_img)
            else:
                tag.decompose()
        return

    if name in ("source", "track"):
        tag.decompose()
        return


def _unescape_url_entities(html_fragment: str) -> str:
    def fix(m):
        attr, value = m.group(1), m.group(2)
        value = value.replace("&amp;", "&").replace("&#38;", "&")
        return f'{attr}="{value}"'
    return re.sub(r'(href|src|poster)="([^"]*)"', fix, html_fragment)


# ---------------------------------------------------------------------------
# Final cleaning for Telegram
# ---------------------------------------------------------------------------
def _clean_html_for_telegram(html: str, thumbnail_url: str = "") -> str:
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for bad in soup(["script", "style", "noscript", "template"]):
        bad.decompose()

    _promote_quotes(soup)
    _promote_repeated_pull_quotes(soup)
    _prune_junk(soup)

    title_h1 = soup.find("h1")
    if title_h1 is not None:
        title_h1.decompose()

    thumb_norm = _normalize_media_url(thumbnail_url)
    thumb_id = _media_id(thumbnail_url)

    for tag in list(soup.find_all(["img", "video", "audio", "iframe",
                                   "picture", "source", "track"])):
        if tag.parent is None:
            continue
        _convert_media(tag, soup)

    for tag in soup.find_all(True):
        name = tag.name.lower()
        if tag.parent is None:
            continue

        if name in VOID_UNSUPPORTED:
            tag.decompose()
            continue

        if name not in TELEGRAM_ALLOWED_TAGS:
            tag.unwrap()
            continue

        if name == "a":
            href = (tag.get("href") or "").strip()
            if not href or href.startswith("#"):
                tag.unwrap()
                continue
            new = {"href": href}
            if tag.get("name"):
                new["name"] = tag["name"]
            tag.attrs = new
            continue

        if name == "img":
            src = tag.get("src") or ""
            if not src:
                tag.decompose()
                continue
            if thumb_norm and _normalize_media_url(src) == thumb_norm:
                tag.decompose()
                continue
            if thumb_id and _media_id(src) == thumb_id:
                tag.decompose()
                continue
            if tag.find_parent("figure") is None:
                alt = (tag.get("alt") or "").strip()
                if len(alt) > 40:
                    tag.decompose()
                    continue
            continue

        if name in ("video", "audio", "tg-document"):
            allowed = TELEGRAM_ATTR_WHITELIST.get(name, [])
            tag.attrs = {k: tag[k] for k in allowed
                         if k in tag.attrs and tag[k] not in (None, "")}
            continue

        allowed = TELEGRAM_ATTR_WHITELIST.get(name, [])
        tag.attrs = {k: tag[k] for k in allowed
                     if k in tag.attrs and tag[k] not in (None, "")}

    # Remove any empty container that pruning left behind
    for tag in soup.find_all(["ul", "ol", "blockquote", "figure",
                              "p", "li", "h1", "h2", "h3", "h4"]):
        if tag.parent is None:
            continue
        if not tag.get_text(strip=True) and not tag.find(["img", "video", "audio"]):
            tag.decompose()

    parts = [str(c).strip() for c in soup.contents if str(c).strip()]
    parts = [_unescape_url_entities(p) for p in parts]
    html_str = "\n".join(parts)

    block_re = "|".join(sorted(TELEGRAM_BLOCK_TAGS))
    html_str = re.sub(rf"</({block_re})>", lambda m: m.group(0) + "\n", html_str)
    html_str = re.sub(r"[ \t]+\n", "\n", html_str)
    html_str = re.sub(r"\n{3,}", "\n\n", html_str)
    return html_str.strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    raw = _request(FEED_URL)
    if raw.lstrip()[:1] != b"<":
        print("[error] Feed response does not look like RSS.")
        sys.exit(1)

    cleaned = clean_bytes(raw)
    items = parse_with_feedparser(cleaned) or parse_with_regex(cleaned)
    if not items:
        print("[error] No items parsed.")
        sys.exit(1)

    print(f"[debug] sample titles: {[i['title'][:40] for i in items[:5]]}")

    target = next((i for i in items if KEYWORD in i["title"]), None)
    if target is None:
        print(f"[info] No item with '{KEYWORD}' in title.")
        return

    thumbnail, body_html = extract_article_body(target["link"])
    description = _clean_html_for_telegram(body_html, thumbnail)

    final_soup = BeautifulSoup(description, "html.parser")
    imgs = final_soup.find_all("img")
    bqs = final_soup.find_all("blockquote")
    print(f"[debug] final media: "
          f"img={len(imgs)}, "
          f"video={len(final_soup.find_all('video'))}, "
          f"audio={len(final_soup.find_all('audio'))}, "
          f"figcaption={len(final_soup.find_all('figcaption'))}, "
          f"blockquote={len(bqs)}")
    for i, bq in enumerate(bqs):
        print(f"[debug]   blockquote[{i}]: {bq.get_text(' ', strip=True)[:80]}")

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

    print(f"[ok] Wrote {OUTPUT_FILE}  "
          f"(description: {len(description)} chars)")


if __name__ == "__main__":
    main()
