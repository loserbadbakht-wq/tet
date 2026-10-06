import json
import feedparser
from newspaper import Article

FEED_URL = "https://www.zoomit.ir/feed/"
KEYWORD = "طاعون"
OUTPUT_FILE = "plague_news.json"

def main():
    feed = feedparser.parse(FEED_URL)
    if feed.bozo:
        print(f"Feed parsing error: {feed.bozo_exception}")
        return

    # Find the latest item whose title contains the keyword
    target = None
    for entry in feed.entries:
        if KEYWORD in entry.get("title", ""):
            target = entry
            break

    if not target:
        print(f"No item found with title containing '{KEYWORD}'")
        return

    # Extract required fields
    title = target.get("title", "")
    creator = target.get("author", "")          # feedparser maps dc:creator to .author
    pub_date = target.get("published", "")
    link = target.get("link", "")

    # Fetch article content and build description
    description = ""
    if link:
        try:
            article = Article(link, language="fa")
            article.download()
            article.parse()
            description = article.text.strip()
            # Limit description length (optional)
            if len(description) > 2000:
                description = description[:2000] + "..."
        except Exception as e:
            print(f"Failed to extract article: {e}")
            description = ""

    result = {
        "title": title,
        "dc:creator": creator,
        "pubDate": pub_date,
        "link": link,
        "description": description,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Saved {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
