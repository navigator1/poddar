#!/usr/bin/env python3
"""
Dela en podd-feed i kronologiska delar, äldst först.

Till skillnad från crawl-skripten bygger det här inga nya <item>-element -
originalets XML kopieras rakt av. Beskrivningar, avsnittsnummer, längd,
kapitel och omslagsbilder följer med oförändrade.

  python feed_split.py https://feeds.packetpushers.net/PacketPushersWeeklyPodcast/
  python feed_split.py <url> --chunk 200 --output hn.xml
  python feed_split.py <url> --info          # bara statistik, skriver inga filer

Fungerar på vilken RSS-feed som helst. Börja alltid med --info: säger den
att feeden bara innehåller de senaste hundra avsnitten är den kapad, och
då behövs crawl-metoden istället.
"""

import argparse
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-archive-feed/1.0)"}


def load(source):
    """Hämtar feeden från en URL eller läser en lokal fil."""
    if re.match(r"https?://", source):
        req = urllib.request.Request(source, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")
    with open(source, encoding="utf-8") as f:
        return f.read()


def register_namespaces(raw):
    """
    ElementTree ersätter okända prefix med ns0, ns1 och så vidare om de
    inte registrerats först. Podd-feeds använder gärna ett halvdussin
    (itunes, podcast, content, media...), så vi läser dem ur rotelementet.
    """
    found = {}
    head = raw[:raw.find(">", raw.find("<rss")) + 1] if "<rss" in raw else raw[:4000]
    for prefix, uri in re.findall(r'xmlns:([A-Za-z0-9_-]+)\s*=\s*"([^"]+)"', head):
        ET.register_namespace(prefix, uri)
        found[prefix] = uri
    return found


def item_date(item):
    """pubDate som datetime. Saknas eller trasigt -> None."""
    txt = item.findtext("pubDate")
    if not txt:
        return None
    try:
        d = parsedate_to_datetime(txt)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def write_part(tree, channel, items, path, label):
    """
    Skriver en delfeed. Alla <item> byts ut mot den aktuella gruppen,
    resten av kanalen (titel, bild, kategorier) lämnas orörd.
    """
    for old in channel.findall("item"):
        channel.remove(old)
    for it in items:
        channel.append(it)

    title = channel.find("title")
    if title is not None and label:
        if not hasattr(write_part, "original"):
            write_part.original = title.text or "Podcast"
        title.text = f"{write_part.original} {label}"

    tree.write(path, encoding="utf-8", xml_declaration=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("source", help="Feed-URL eller sökväg till en lokal XML-fil")
    p.add_argument("-o", "--output", default="feed.xml")
    p.add_argument("--chunk", type=int, help="Antal avsnitt per fil")
    p.add_argument("--count", type=int, help="Bara de N äldsta avsnitten")
    p.add_argument("--newest-first", action="store_true")
    p.add_argument("--info", action="store_true", help="Visa statistik, skriv inget")
    args = p.parse_args()

    raw = load(args.source)
    register_namespaces(raw)
    tree = ET.ElementTree(ET.fromstring(raw))
    channel = tree.getroot().find("channel")
    if channel is None:
        sys.exit("Ingen <channel> - är det verkligen en RSS-feed?")

    items = channel.findall("item")
    if not items:
        sys.exit("Feeden innehåller inga avsnitt.")

    dated = [(item_date(i), i) for i in items]
    undated = sum(1 for d, _ in dated if d is None)
    known = [d for d, _ in dated if d]

    print(f"Podd:     {channel.findtext('title', '(namnlös)')}")
    print(f"Avsnitt:  {len(items)}")
    if known:
        print(f"Spann:    {min(known).date()} till {max(known).date()}")
    if undated:
        print(f"Varning:  {undated} avsnitt saknar pubDate")

    with_audio = sum(1 for i in items if i.find("enclosure") is not None)
    if with_audio < len(items):
        print(f"Varning:  {len(items) - with_audio} avsnitt saknar ljudfil")

    if args.info:
        return

    # Sortera. Avsnitt utan datum hamnar sist, i sin ursprungliga ordning.
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    ordered = [i for _, i in sorted(dated, key=lambda t: t[0] or far_future)]
    if args.count:
        ordered = ordered[:args.count]
    if args.newest_first:
        ordered.reverse()

    base, ext = os.path.splitext(args.output)
    ext = ext or ".xml"
    if args.chunk:
        groups = [ordered[i:i + args.chunk] for i in range(0, len(ordered), args.chunk)]
        for n, group in enumerate(groups, 1):
            name = f"{base}-{n:03d}{ext}"
            write_part(tree, channel, group, name, f"[{n}/{len(groups)}]")
            d = [item_date(i) for i in group]
            d = [x for x in d if x]
            span = f"{min(d).date()} till {max(d).date()}" if d else "okänt spann"
            print(f"  {name}: {len(group)} avsnitt, {span}")
    else:
        write_part(tree, channel, ordered, base + ext, "")
        print(f"  {base + ext}: {len(ordered)} avsnitt")


if __name__ == "__main__":
    main()
