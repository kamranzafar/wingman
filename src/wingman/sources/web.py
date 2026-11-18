"""Reading your blog and web pages into Documents."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from wingman.core import http
from wingman.core.models import Document, LinkCard

log = logging.getLogger(__name__)
Progress = Callable[[str], None]

SKIP_PATH = re.compile(r"/(tag|tags|category|categories|page|feed|rss|author|search|about|contact|privacy)(/|$)|"
                       r"\.(xml|jpg|jpeg|png|gif|svg|pdf|css|js|zip)$", re.I)
FEED_PATHS = ("index.xml", "feed", "rss.xml", "atom.xml", "feed.xml")
MAX_LISTING_PAGES = 30
MIN_POST_CHARS = 600  # shorter pages are listings or stubs


def extract_text(html: str) -> tuple[str, str]:
    """(title, main text) of an HTML page, without navigation and other chrome."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title else ""
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    articles = soup.find_all("article")
    # One <article> = a blog post; several = cards on a landing page, so take the whole <main>.
    main = (articles[0] if len(articles) == 1 else None) or soup.find("main") or soup.body or soup
    return title, re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))


def url_date(url: str) -> str:
    """Post date from URLs like /blog/2026/10/30/slug/ or /2016/08/slug/ ('' if none)."""
    m = re.search(r"/((?:19|20)\d\d)/(\d\d)(?:/(\d\d))?/", url)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3) or '01'}" if m else ""


def post_links(html: str, base: str) -> list[str]:
    """Same-site links on a page that look like posts (not tags, feeds, assets...)."""
    host, base_path = urlparse(base).netloc, urlparse(base).path.rstrip("/")
    links: list[str] = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(base, a["href"]).split("#")[0].split("?")[0]
        p = urlparse(url)
        path = p.path.rstrip("/")
        if p.netloc != host or SKIP_PATH.search(p.path) or not path or path == base_path:
            continue
        if url not in links:
            links.append(url)
    return links


def feed_links(index_html: str, base: str) -> list[str]:
    """Post URLs from the site's RSS/Atom feed (newest first), if it has one."""
    soup = BeautifulSoup(index_html, "html.parser")
    candidates = [urljoin(base, link["href"]) for link in soup.find_all("link", href=True)
                  if any(kind in (link.get("type") or "") for kind in ("rss", "atom"))]
    candidates += [urljoin(base, p) for p in FEED_PATHS]
    for feed_url in dict.fromkeys(candidates):
        try:
            root = ElementTree.fromstring(http.get_text(feed_url).encode())
        except Exception:  # noqa: BLE001 - not a feed / not found
            continue
        links = [e.text.strip() for e in root.iter("link") if e.text and e.text.strip().startswith("http")]
        links += [e.get("href") for e in root.iter("{http://www.w3.org/2005/Atom}link")
                  if e.get("rel", "alternate") == "alternate" and e.get("href")]
        posts = [u for u in dict.fromkeys(links)
                 if u.rstrip("/") != base.rstrip("/") and not SKIP_PATH.search(urlparse(u).path)]
        if posts:
            return posts
    return []


def _listing_pages(blog_url: str, known: list[str], want: int) -> list[str]:
    """Follow /page/N/ listings for posts older than the feed shows."""
    base = blog_url if blog_url.endswith("/") else blog_url + "/"
    found: list[str] = []
    for page in range(2, MAX_LISTING_PAGES + 1):
        if len(known) + len(found) >= want:
            break
        try:
            listing = http.get_text(urljoin(base, f"page/{page}/"))
        except Exception:  # noqa: BLE001 - no more pages
            break
        new = [u for u in post_links(listing, blog_url) if u not in known and u not in found]
        if not new:
            break
        found += new
    return found


def fetch_blog(blog_url: str, max_posts: int, progress: Progress = lambda _: None) -> list[Document]:
    """Your blog posts, newest first: from the feed, then older listing pages."""
    index_html = http.get_text(blog_url)
    links = feed_links(index_html, blog_url) or post_links(index_html, blog_url)
    links += _listing_pages(blog_url, links, max_posts)
    docs: list[Document] = []
    for url in links:
        if len(docs) >= max_posts:
            break
        try:
            title, text = extract_text(http.get_text(url))
        except Exception as e:  # noqa: BLE001 - skip unreadable pages
            log.warning("skip %s: %s", url, e)
            continue
        if len(text) < MIN_POST_CHARS:
            continue
        docs.append(Document(title=title, text=text[:8000], kind="blog", url=url, date=url_date(url)))
        progress(f"read {title or url}")
    if not docs:  # single-page site: the index is the content
        title, text = extract_text(index_html)
        docs.append(Document(title=title, text=text[:20000], kind="blog", url=blog_url))
    return docs


def fetch_page(url: str, kind: str = "page", date: str = "") -> Document:
    """A single page (homepage, project site), read as-is with no link crawling."""
    title, text = extract_text(http.get_text(url))
    return Document(title=title or url, text=text[:20000], kind=kind, url=url, date=date)


def link_card(url: str, html: str) -> LinkCard:
    """Title/description for a link preview, from the page's Open Graph or meta tags."""
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names: str) -> str:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    page_title = soup.title.string.strip() if soup.title and soup.title.string else ""
    title = meta("og:title", "twitter:title") or page_title or url
    return LinkCard(uri=url, title=title[:300], description=meta("og:description", "description")[:1000])


def fetch_article(url: str) -> tuple[Document, LinkCard]:
    """One of your articles, for sharing: its text (for the drafter) and preview card (for the post)."""
    html = http.get_text(url)
    title, text = extract_text(html)
    return Document(title=title or url, text=text, kind="blog", url=url, date=url_date(url)), link_card(url, html)
