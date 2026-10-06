import json
import re
import requests
from lxml import etree
from newspaper import Article

FEED_URL = "https://www.zoomit.ir/feed/"
KEYWORD = "طاعون"
OUTPUT_FILE = "plague_news.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# XML namespaces used in RSS feeds
NS = {
    "rss": "http://purl.org/rss/1.0/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
}


def fetch_feed_bytes(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.content


def sanitize_xml(raw: bytes) -> bytes:
    # Strip UTF-8 BOM if present
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    # Remove XML declaration so lxml's recovery parser doesn't choke on it
    raw = re.sub(rb"<\?xml[^>]*\?>", b"", raw, count=1)

    # Drop control characters that are illegal in XML 1.0
    raw = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", raw)

    # lxml needs a single root — wrap in a dummy element just in case
    return raw.strip()


def parse_feed(raw: bytes):
    parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
    try:
        root = etree.fromstring(sanitize_xml(raw), parser=parser)
    except etree.XMLSyntaxError as e:
        print(f"XMLSyntaxError: {e}")
        return None
    return root


def find_text(element, paths):
    for path in paths:
        found = element.find(path, namespaces=NS)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


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
        print(f"Failed to extract article from {link}: {e}")
        return ""


def main():
    raw = fetch_feed_bytes(FEED_URL)
    root = parse_feed(raw)
    if root is None:
        raise SystemExit("Could not parse the feed even with recovery mode.")

    # Find all <item> elements (RSS 2.0 layout used by Zoomit)
    items = root.findall(".//item")
    if not items:
        # Fallback for Atom-style feeds
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    if not items:
        raise SystemExit("No items found in feed.")

    target = None
    for item in items:
        title = find_text(item, ["title"])
        if KEYWORD in title:
            target = item
            break

    if target is None:
        print(f"No item found with title containing '{KEYWORD}'")
        return

    title = find_text(target, ["title"])
    creator = find_text(target, ["{http://purl.org/dc/elements/1.1/}creator"])
    pub_date = find_text(target, ["pubDate"])
    link = find_text(target, ["link"])

    description = extract_description(link)

    result = {
        "title": title,
        "dc:creator": creator,
        "pubDate": pub_date,
        "link": link,
        "description": description,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Saved {OUTPUT_FILE}:")
    print(json.dumps({k: v[:80] for k, v in result.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
