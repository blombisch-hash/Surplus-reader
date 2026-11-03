#!/usr/bin/env python3
"""
Holt die Artikelliste von surplusmagazin.de und schreibt sie nach data/articles.json.

Gespeichert werden nur Metadaten (Titel, Teaser, Autor, Datum, Bildadresse, Link,
Ressort/Kolumne/Heft) – kein Artikeltext. Die Bilder lädt die App direkt von Surplus.

Aufruf:
  python3 scripts/update.py          # schnell: nur Neues (stündlich)
  python3 scripts/update.py --full   # komplett: ganzes Archiv neu einlesen
"""
import concurrent.futures as cf
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

BASE = "https://www.surplusmagazin.de"
HOST_RE = re.compile(r"^https?://(?:www\.)?surplusmagazin\.de", re.I)
OUT = os.path.join(os.path.dirname(__file__), "..", "app", "data", "articles.json")
UA = "SurplusReader/1.0 (private RSS-style reader for a paying subscriber)"

SECTIONS = {  # Schlüssel in der App -> Pfad auf der Webseite
    "politik": "/thema/politik/",
    "wirtschaft": "/thema/wirtschaft/",
    "finanzen": "/thema/finanzen/",
    "klima": "/thema/klima/",
    "international": "/thema/internationales/",
    "wissenschaft": "/thema/wissenschaft/",
}
PICKS = "/thema/auswahl-der-redaktion/"
COLUMNS_INDEX = "/kolumnen/"
MAGAZIN_INDEX = "/magazin/"

# Seiten, die keine Artikel sind
NON_ARTICLE = re.compile(
    r"^/(?:$|page/|thema/|tag/|kolumne/|kolumnen/|magazin/|autoren?/|author/|about/|abo|"
    r"ueber|impressum|datenschutz|agb|kontakt|newsletter|rss/|sitemap|members/|r/|"
    r"assets/|content/|ghost/|public/|unsubscribe|aktuelles|surplus-\d+-)",
    re.I,
)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(url, tries=3):
    if url.startswith("/"):
        url = BASE + url
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 503):
                time.sleep(5 * (i + 1))
                continue
            log("HTTP", e.code, url)
            return None
        except Exception as e:  # Netzfehler
            log("Fehler", type(e).__name__, url)
            time.sleep(2 * (i + 1))
    return None


def path_of(href):
    """Normalisiert einen Link auf '/slug/' bzw. '/podcast/slug/' – oder None."""
    if not href:
        return None
    href = html.unescape(href.strip())
    if HOST_RE.match(href):
        href = HOST_RE.sub("", href)
    elif href.startswith("http"):
        return None
    href = href.split("#")[0].split("?")[0]
    if not href.startswith("/"):
        return None
    if not href.endswith("/"):
        href += "/"
    if NON_ARTICLE.match(href):
        return None
    parts = [p for p in href.split("/") if p]
    if len(parts) == 1 or (len(parts) == 2 and parts[0] == "podcast"):
        return href
    return None


def links_in(page_html):
    out = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', page_html or ""):
        p = path_of(m.group(1))
        if p and p not in out:
            out.append(p)
    return out


def sitemap_paths():
    idx = fetch("/sitemap.xml") or ""
    maps = [u for u in re.findall(r"<loc>([^<]+)</loc>", idx) if "sitemap-posts" in u] or [BASE + "/sitemap-posts.xml"]
    paths = []
    for sm in maps:
        xml = fetch(sm) or ""
        for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
            p = path_of(loc)
            if p and p not in paths:
                paths.append(p)
    return paths


class Head(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.ld, self._in_ld, self._buf, self.title = {}, [], False, [], ""
        self._in_title = False
        self.h1, self._in_h1, self._h1_done = "", False, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = a.get("property") or a.get("name")
            if k and "content" in a:
                self.meta.setdefault(k, []).append(a["content"])
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._in_ld, self._buf = True, []
        elif tag == "title":
            self._in_title = True
        elif tag == "h1" and not self._h1_done:
            self._in_h1 = True

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            try:
                self.ld.append(json.loads("".join(self._buf)))
            except Exception:
                pass
        elif tag == "title":
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            self._in_h1, self._h1_done = False, True

    def handle_data(self, d):
        if self._in_ld:
            self._buf.append(d)
        elif self._in_title:
            self.title += d
        elif self._in_h1:
            self.h1 += d


def names(x):
    if not x:
        return []
    if isinstance(x, dict):
        x = [x]
    if isinstance(x, str):
        return [x]
    return [a.get("name", "").strip() for a in x if isinstance(a, dict) and a.get("name")]


def parse_article(path, page_html):
    h = Head()
    try:
        h.feed(page_html)
    except Exception:
        pass
    ld = next((x for x in h.ld if isinstance(x, dict) and x.get("@type") in ("Article", "NewsArticle", "BlogPosting")), {})
    m = lambda k: (h.meta.get(k) or [""])[0].strip()
    # Die sichtbare Überschrift (h1) ist der echte Titel; headline/og:title sind teils SEO-Titel.
    title = (" ".join(h.h1.split()) or ld.get("headline") or m("og:title") or h.title.split(" | ")[0] or "").strip()
    if not title:
        return None
    img = ld.get("image")
    if isinstance(img, dict):
        img = img.get("url")
    elif isinstance(img, list):
        img = img[0].get("url") if img and isinstance(img[0], dict) else (img[0] if img else None)
    img = img or m("og:image") or None
    authors = names(ld.get("author")) or [x for x in [m("twitter:data1"), m("author")] if x]
    date = ld.get("datePublished") or m("article:published_time") or ""
    tags = h.meta.get("article:tag") or [t.strip() for t in str(ld.get("keywords") or "").split(",") if t.strip()]
    desc = (ld.get("description") or m("og:description") or m("description") or "").strip()
    a = {
        "t": html.unescape(title),
        "u": path.strip("/") + "/",
        "a": ", ".join(dict.fromkeys(html.unescape(n) for n in authors if n)),
        "d": norm_date(date),
        "x": html.unescape(desc)[:300],
        "g": [html.unescape(t) for t in tags[:6]],
    }
    if img:
        a["i"] = img
    if path.startswith("/podcast/"):
        a["k"] = "podcast"
    elif a["a"] == "Surplus News":
        a["k"] = "news"
    elif "wochenbrief" in path or "Wochenbrief" in tags or desc.startswith("Die wichtigsten Wirtschaftsfragen der Woche"):
        a["k"] = "wochenbrief"
    return a


def norm_date(s):
    if not s:
        return ""
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    except Exception:
        return s[:16] + ("Z" if len(s) >= 16 else "")


def crawl_list(base_path, max_pages, known_stop=None):
    """Sammelt Artikel-Links einer Listen-Seite (mit /page/N/)."""
    found = []
    for n in range(1, max_pages + 1):
        url = base_path if n == 1 else f"{base_path.rstrip('/')}/page/{n}/"
        page = fetch(url)
        if not page:
            break
        new = [p for p in links_in(page) if p not in found]
        if not new:
            break
        found += new
        if known_stop and all(p in known_stop for p in new):
            break
        time.sleep(0.4)
    return found


def main():
    full = "--full" in sys.argv
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    try:
        with open(OUT, encoding="utf-8") as f:
            store = {a["id"]: a for a in json.load(f)["articles"]}
    except Exception:
        store, full = {}, True
    log("Modus:", "komplett" if full else "schnell", "| vorhandene Artikel:", len(store))

    # 1) Alle Artikel-URLs aus der Sitemap
    paths = sitemap_paths()
    log("Sitemap:", len(paths), "URLs")
    pid = lambda p: p.strip("/").replace("/", "~")
    todo = [p for p in paths if full or pid(p) not in store]

    # 2) Metadaten je Artikel holen (parallel, aber höflich)
    def work(p):
        page = fetch(p)
        time.sleep(0.3)
        return p, (parse_article(p, page) if page else None)

    done = 0
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for p, a in ex.map(work, todo):
            done += 1
            if a:
                old = store.get(pid(p), {})
                a["id"] = pid(p)
                if not full:
                    for keep in ("s", "c", "h", "r"):
                        if keep in old:
                            a[keep] = old[keep]
                store[a["id"]] = a
            if done % 100 == 0:
                log(" ", done, "/", len(todo))
    log("Neu/aktualisiert:", len(todo))

    def tag(paths_, field, value, multi=False):
        for p in paths_:
            a = store.get(pid(p))
            if not a:
                continue
            if multi:
                lst = a.setdefault(field, [])
                if value not in lst:
                    lst.append(value)
            else:
                a[field] = value

    known = set(store)
    pages = 200 if full else 2

    # 3) Ressorts – Links, die auf fast allen Ressortseiten auftauchen (Seitenleisten,
    #    Empfehlungen), sind keine Zuordnung und werden ignoriert.
    per_section = {key: crawl_list(path, pages) for key, path in SECTIONS.items()}
    seen = {}
    for got in per_section.values():
        for p in set(got):
            seen[p] = seen.get(p, 0) + 1
    for key, got in per_section.items():
        clean = [p for p in got if seen[p] < 4]
        tag(clean, "s", key, multi=True)
        log("Ressort", key, len(clean))
    tag(crawl_list(PICKS, 10 if full else 1), "r", 1)

    # 4) Kolumnen
    idx = fetch(COLUMNS_INDEX) or ""
    col_paths = sorted(set(re.findall(r'href=["\'](?:https?://(?:www\.)?surplusmagazin\.de)?(/kolumne/[^"\'/]+/?)["\']', idx)))
    per_col = {}
    for cp in col_paths:
        page = fetch(cp) or ""
        h = Head()
        try:
            h.feed(page)
        except Exception:
            pass
        name = ((h.meta.get("og:title") or [""])[0] or h.title).split(" - ")[0].split(" | ")[0].strip()
        per_col[html.unescape(name) or cp.strip("/").split("/")[-1]] = crawl_list(cp, 50 if full else 1)
    cnt = {}
    for got in per_col.values():
        for p in set(got):
            cnt[p] = cnt.get(p, 0) + 1
    for name, got in per_col.items():
        clean = [p for p in got if cnt[p] == 1 or len(per_col) == 1]
        tag(clean, "c", name)
        log("Kolumne", name, len(clean))

    # 5) Hefte
    mag = fetch(MAGAZIN_INDEX) or ""
    issues = sorted(set(re.findall(r'href=["\'](?:https?://(?:www\.)?surplusmagazin\.de)?/(surplus-(\d+)-[^"\'/]+)/?["\']', mag)),
                    key=lambda t: -int(t[1]))
    issue_names, per_issue = {}, {}
    for slug, num in (issues if full else issues[:3]):
        page = fetch("/" + slug + "/") or ""
        h = Head()
        try:
            h.feed(page)
        except Exception:
            pass
        issue_names[num] = html.unescape(((h.meta.get("og:title") or [""])[0] or h.title).split(" | ")[0].strip())
        per_issue[num] = links_in(page)
    cnt = {}
    for got in per_issue.values():
        for p in set(got):
            cnt[p] = cnt.get(p, 0) + 1
    for num, got in per_issue.items():
        clean = [p for p in got if cnt[p] == 1]   # Links auf mehreren Heftseiten = Seitenleiste
        tag(clean, "h", int(num))
        log("Heft", num, len(clean))

    # 6) Schreiben
    arts = sorted(store.values(), key=lambda a: a.get("d") or "", reverse=True)
    try:
        with open(OUT, encoding="utf-8") as f:
            old_meta = json.load(f).get("issues", {})
    except Exception:
        old_meta = {}
    old_meta.update({k: v for k, v in issue_names.items() if v})
    data = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "issues": old_meta, "articles": arts}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log("Gespeichert:", len(arts), "Artikel")


if __name__ == "__main__":
    main()
