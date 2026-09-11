#!/usr/bin/env python3
"""
Komplett Heavy Networking-feed: levande feed + crawlade äldre avsnitt.

Feeden innehåller bara de 250 senaste avsnitten, men de har fullständig
metadata. Resten finns på sajten. Skriptet behåller de 250 som de är och
bygger upp de äldre från avsnittssidorna, sedan slås allt ihop.

Kör i två steg:

  1. python pp_feed.py probe
       Testar vilka sitemaps som finns och vad en avsnittssida innehåller.
       Tar sekunder. Klistra in utskriften innan du kör steg 2.

  2. python pp_feed.py build --chunk 200 --output hn.xml

Fungerar på Packet Pushers andra shower via --show, t.ex. --show network-break.
"""

import argparse
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

SITE = "https://packetpushers.net"
FEED = "https://feeds.packetpushers.net/PacketPushersWeeklyPodcast/"
CACHE = "pp_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-archive-feed/1.0)"}

# Girig match - podtrac- och feedpress-URL:er innehåller .mp3 två gånger.
AUDIO = re.compile(r'https?://[^\s"\'<>]+\.mp3(?:\?[^\s"\'<>]*)?', re.I)

# Titeln: sajten har inte og:title i den form vi först antog, så vi
# provar flera mönster i fallande tillförlitlighet.
META_TITLE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]*'
    r'content=["\']([^"\']+)["\']', re.I)
META_TITLE_REV = re.compile(                     # content före property
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*'
    r'(?:property|name)=["\'](?:og:title|twitter:title)["\']', re.I)
JSONLD_TITLE = re.compile(r'"headline"\s*:\s*"([^"]+)"')
HTML_TITLE = re.compile(r'<title[^>]*>([^<]+)</title>', re.I)
H1_TITLE = re.compile(r'<h1[^>]*>(.{3,200}?)</h1>', re.I | re.S)
PUBTIME = re.compile(
    r'<meta property="article:published_time" content="([^"]*)"', re.I)
JSONLD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')
TIMETAG = re.compile(r'<time[^>]+datetime="([^"]+)"', re.I)
LOC = re.compile(r"<loc>([^<]+)</loc>")


def get(url, timeout=60, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 401):
                raise
            last = e
        except Exception as e:
            last = e
        wait = 2 ** attempt
        print(f"    försök {attempt + 1} gav {last}, väntar {wait}s", file=sys.stderr)
        time.sleep(wait)
    raise last


def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("episodes", {})
            return d
    return {"episodes": {}}


def save_cache(c):
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


def extract_title(html, url):
    """Titel ur sidan. Sista utväg: sluggen omgjord till läsbar text."""
    for rx in (META_TITLE, META_TITLE_REV, JSONLD_TITLE, HTML_TITLE):
        m = rx.search(html)
        if m:
            t = htmllib.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
            # Sajtnamnet hänger ofta med i <title>: "HN840: ... - Packet Pushers"
            t = re.sub(r"\s*[-|–]\s*Packet Pushers\s*$", "", t)
            if t:
                return t
    m = H1_TITLE.search(html)
    if m:
        t = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").capitalize()


def pick_audio(html, url):
    """
    Välj rätt ljudfil bland flera kandidater.

    Avsnittssidorna innehåller fem .mp3-länkar (spelare, nedladdning,
    relaterade avsnitt). Att bara ta den första är en chansning. Filnamnet
    innehåller nästan alltid avsnittskoden ur URL:en - HN840.mp3 för
    .../hn840-how-to-make-a-technology-buying-decision/ - så vi matchar
    på den i första hand.
    """
    found = list(dict.fromkeys(AUDIO.findall(html)))
    if not found:
        return None, 0

    slug = url.rstrip("/").split("/")[-1]
    m = re.match(r"([A-Za-z]+)(\d+)", slug)      # hn341 -> ("hn", "341")

    if m:
        prefix, number = m.group(1).lower(), m.group(2)

        # 1. Filnamnet innehåller hela koden: HN840.mp3
        for u in found:
            if prefix + number in u.rsplit("/", 1)[-1].lower():
                return u, len(found)

        # 2. Bara numret. Avsnitt före rebrandingen vid 423 hette "Show NNN"
        #    och ljudfilen kan heta Show341.mp3 eller PP-341.mp3 trots att
        #    sluggen är hn341. Kräv att siffran inte sitter ihop med andra
        #    siffror, annars matchar 341 även i 1341.
        num = re.compile(rf"(?<!\d){number}(?!\d)")
        for u in found:
            if num.search(u.rsplit("/", 1)[-1]):
                return u, len(found)

    for u in found:                       # annars: föredra en riktig mediavärd
        if "blubrry.com" in u or "packetpushers" in u:
            return u, len(found)
    return found[0], len(found)


def extract_date(html):
    for rx in (PUBTIME, JSONLD_DATE, TIMETAG):
        m = rx.search(html)
        if m and re.match(r"\d{4}-\d{2}-\d{2}", m.group(1)):
            return m.group(1)
    return None


# ---------- steg 1: probe ----------

def cmd_probe(args):
    print("Sitemaps:")
    sitemaps = []
    for path in ("/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml"):
        try:
            body = get(SITE + path, timeout=25)
            subs = LOC.findall(body)
            print(f"  OK    {path}  ({len(subs)} underliggande)")
            for s in subs[:25]:
                mark = "  <-- podcasts" if "podcast" in s.lower() else ""
                print(f"          {s.split('/')[-1]}{mark}")
            sitemaps = subs
            break
        except Exception as e:
            print(f"  FEL   {path}: {e}")

    print("\nAvsnittssida:")
    url = args.url
    if not url:
        # Ta första bästa avsnitt ur den levande feeden
        try:
            feed = ET.fromstring(get(FEED))
            for it in feed.find("channel").findall("item"):
                link = it.findtext("link") or ""
                if args.show in link:
                    url = link
                    break
        except Exception as e:
            print(f"  kunde inte läsa feeden: {e}")
    if not url:
        print("  ingen URL att testa - ange --url")
        return

    print(f"  {url}")
    try:
        html = get(url)
    except Exception as e:
        print(f"  FEL: {e}")
        return

    audio, n = pick_audio(html, url)
    print(f"  ljudfil:  {audio or 'HITTADES INTE'}")
    print(f"  datum:    {extract_date(html) or 'HITTADES INTE'}")
    print(f"  titel:    {extract_title(html, url)}")
    print(f"  {n} unika .mp3-länkar på sidan:")
    for c in dict.fromkeys(AUDIO.findall(html)):
        print(f"      {'-> ' if c == audio else '   '}{c}")
    print("\nRapportera det här innan du kör build.")


# ---------- steg 1b: shows ----------

def all_podcast_urls():
    """Alla /podcasts/-URL:er ur sajtens podcast-sitemaps."""
    body = None
    for path in ("/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml"):
        try:
            body = get(SITE + path)
            break
        except Exception:
            continue
    if body is None:
        sys.exit("Ingen sitemap gick att läsa.")
    subs = [u for u in LOC.findall(body)
            if u.endswith(".xml") and "podcast" in u.lower()]
    urls, seen = [], set()
    for sub in subs:
        try:
            page = get(sub)
        except Exception as e:
            print(f"  {sub}: {e}", file=sys.stderr)
            continue
        for u in LOC.findall(page):
            if "/podcasts/" in u and u not in seen:
                seen.add(u)
                urls.append(u)
        print(f"  {sub.split('/')[-1]}: {len(urls)} totalt", file=sys.stderr)
        time.sleep(0.3)
    return urls


def cmd_shows(args):
    """
    Visar varje show-sökväg och hur många avsnitt den har, med första och
    sista publiceringsåret gissat ur URL:en där det går.

    Behövs eftersom podden bytte namn: avsnitt upp till 422 hette Packet
    Pushers, resten Heavy Networking. Om de gamla ligger under en annan
    sökväg måste build köras med båda.
    """
    urls = all_podcast_urls()
    groups = {}
    for u in urls:
        m = re.search(r"/podcasts/([^/]+)/", u)
        if m:
            groups.setdefault(m.group(1), []).append(u)

    print(f"\n{len(urls)} avsnitts-URL:er, {len(groups)} sökvägar:\n")
    for slug, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(items):5}  /podcasts/{slug}/")
        for ex in items[:2]:
            print(f"         {ex.rstrip('/').split('/')[-1][:70]}")
    print("\nKör build med alla sökvägar som hör till podden, kommaseparerat:")
    print("  python pp_feed.py build --show heavy-networking,weekly-show")


# ---------- steg 2: build ----------

def register_namespaces(raw):
    head = raw[:raw.find(">", raw.find("<rss")) + 1] if "<rss" in raw else raw[:4000]
    for prefix, uri in re.findall(r'xmlns:([A-Za-z0-9_-]+)\s*=\s*"([^"]+)"', head):
        ET.register_namespace(prefix, uri)


def item_date(item):
    txt = item.findtext("pubDate")
    if not txt:
        return None
    try:
        d = parsedate_to_datetime(txt)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def discover(show):
    """Alla avsnitts-URL:er för showen (eller showerna), ur sajtens sitemaps."""
    show = [s.strip() for s in show.split(",") if s.strip()]
    body = None
    for path in ("/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml"):
        try:
            body = get(SITE + path)
            break
        except Exception:
            continue
    if body is None:
        sys.exit("Ingen sitemap gick att läsa - kör 'probe' först.")

    subs = [u for u in LOC.findall(body) if u.endswith(".xml")]
    if not subs:
        subs = [SITE + "/sitemap_index.xml"]

    urls, seen = [], set()
    for sub in subs:
        if "podcast" not in sub.lower() and "post" not in sub.lower():
            continue
        try:
            page = get(sub)
        except Exception as e:
            print(f"  {sub}: {e}", file=sys.stderr)
            continue
        found = [u for u in LOC.findall(page)
                 if any(f"/podcasts/{sh}/" in u for sh in show) and u not in seen]
        urls += found
        seen.update(found)
        if found:
            print(f"  {sub.split('/')[-1]}: +{len(found)}", file=sys.stderr)
        time.sleep(0.3)
    return urls


def make_item(ep):
    """Bygger ett <item> för ett crawlat avsnitt."""
    it = ET.Element("item")
    ET.SubElement(it, "title").text = ep["title"]
    ET.SubElement(it, "link").text = ep["url"]
    g = ET.SubElement(it, "guid"); g.set("isPermaLink", "true"); g.text = ep["url"]
    d = datetime.fromisoformat(ep["date"].replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    ET.SubElement(it, "pubDate").text = format_datetime(d)
    enc = ET.SubElement(it, "enclosure")
    enc.set("url", ep["audio"]); enc.set("length", "0"); enc.set("type", "audio/mpeg")
    return it


def cmd_build(args):
    cache = load_cache()
    eps = cache["episodes"]

    print("Hämtar den levande feeden...", file=sys.stderr)
    raw = get(FEED)
    register_namespaces(raw)
    tree = ET.ElementTree(ET.fromstring(raw))
    channel = tree.getroot().find("channel")
    live = channel.findall("item")
    print(f"{len(live)} avsnitt i feeden.", file=sys.stderr)

    # Spara originalets XML för varje avsnitt som passerar genom feeden.
    # Feeden rymmer bara 250 avsnitt, så när nya publiceras faller gamla
    # ut i andra änden. Utan det här skulle de crawlas om vid nästa körning
    # och tappa beskrivningar, längder och avsnittsnummer.
    rich = cache.setdefault("items", {})
    fresh = 0
    for it in live:
        link = (it.findtext("link") or "").rstrip("/")
        if link and link not in rich:
            fresh += 1
        if link:
            rich[link] = ET.tostring(it, encoding="unicode")
    if fresh:
        print(f"  {fresh} nya avsnitt sparade med full metadata.", file=sys.stderr)
    save_cache(cache)
    live_urls = set(rich)

    print("Läser sitemapen...", file=sys.stderr)
    all_urls = discover(args.show)
    print(f"{len(all_urls)} avsnitt för {args.show} på sajten.", file=sys.stderr)
    if not all_urls:
        sys.exit("Inga träffar - kontrollera sökvägen med 'python pp_feed.py shows'.")

    missing = [u for u in all_urls if u.rstrip("/") not in live_urls]
    todo = [u for u in missing if u not in eps]
    print(f"{len(missing)} saknas i feeden, {len(todo)} kvar att hämta.",
          file=sys.stderr)

    for i, u in enumerate(todo, 1):
        try:
            html = get(u)
        except Exception as e:
            print(f"  hoppar över {u}: {e}", file=sys.stderr)
            eps[u] = None
            continue
        audio, _ = pick_audio(html, u)
        date = extract_date(html)
        if not audio or not date:
            eps[u] = None
        else:
            # extract_title avkodar redan entiteterna - annars kodar
            # ElementTree om dem och &amp; blir &amp;amp; i poddappen.
            eps[u] = {"url": u, "audio": audio, "date": date,
                      "title": extract_title(html, u)}
        if i % 25 == 0:
            save_cache(cache)
            ok = sum(1 for v in eps.values() if v)
            print(f"  {i}/{len(todo)}, {ok} med ljud och datum", file=sys.stderr)
        time.sleep(args.delay)
    save_cache(cache)

    # Ett avsnitt kan ha crawlats som skelett innan det dök upp i feeden.
    # Finns en rik version vinner den, annars blir det dubbletter.
    crawled = [make_item(v) for v in eps.values()
               if v and v["url"].rstrip("/") not in rich]
    unparsed = sum(1 for v in eps.values() if v is None)
    print(f"{len(crawled)} äldre avsnitt byggda, {unparsed} gick inte att tolka.",
          file=sys.stderr)

    # Bygg upp från cachen, inte från den aktuella feeden, så att avsnitt
    # som hunnit falla ur 250-fönstret behåller sin metadata.
    preserved = []
    for xml_text in rich.values():
        try:
            preserved.append(ET.fromstring(xml_text))
        except ET.ParseError:
            continue
    print(f"{len(preserved)} avsnitt med full metadata ur cachen.", file=sys.stderr)

    combined = preserved + crawled
    far = datetime.max.replace(tzinfo=timezone.utc)
    combined.sort(key=lambda i: item_date(i) or far)
    if args.newest_first:
        combined.reverse()

    original_title = channel.findtext("title") or "Podcast"
    base, ext = os.path.splitext(args.output)
    ext = ext or ".xml"

    def write(items, path, label):
        for old in channel.findall("item"):
            channel.remove(old)
        for it in items:
            channel.append(it)
        channel.find("title").text = original_title + (f" {label}" if label else "")
        tree.write(path, encoding="utf-8", xml_declaration=True)

    if args.chunk:
        groups = [combined[i:i + args.chunk]
                  for i in range(0, len(combined), args.chunk)]
        for n, g in enumerate(groups, 1):
            name = f"{base}-{n:03d}{ext}"
            write(g, name, f"[{n}/{len(groups)}]")
            ds = [item_date(i) for i in g]
            ds = [d for d in ds if d]
            span = f"{min(ds).date()} till {max(ds).date()}" if ds else "okänt"
            print(f"  {name}: {len(g)} avsnitt, {span}", file=sys.stderr)
    else:
        write(combined, base + ext, "")
        print(f"  {base + ext}: {len(combined)} avsnitt", file=sys.stderr)

    ds = [item_date(i) for i in combined]
    ds = [d for d in ds if d]
    print(f"Klart: {len(combined)} avsnitt, {min(ds).date()} till {max(ds).date()}",
          file=sys.stderr)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("probe")
    pr.add_argument("--url", help="Avsnitts-URL att testa")
    pr.add_argument("--show", default="heavy-networking")
    pr.set_defaults(func=cmd_probe)

    sh = sub.add_parser("shows")
    sh.set_defaults(func=cmd_shows)

    b = sub.add_parser("build")
    b.add_argument("-o", "--output", default="hn.xml")
    b.add_argument("--chunk", type=int)
    b.add_argument("--delay", type=float, default=0.5)
    b.add_argument("--show", default="heavy-networking",
                   help="En eller flera sökvägar, kommaseparerat")
    b.add_argument("--newest-first", action="store_true")
    b.set_defaults(func=cmd_build)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
