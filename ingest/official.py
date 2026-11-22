"""
Comunicación política oficial: notas de prensa del Gobierno y de los partidos.

Se leen sus canales RSS/Atom (titular y resumen, como con la prensa; nunca
el texto completo). Si no conocemos la dirección del canal, se busca en la
propia web: casi todas lo anuncian en la cabecera
(<link rel="alternate" type="application/rss+xml">) y las hechas con
WordPress lo tienen en /feed/.

Se respeta robots.txt. La Moncloa permite reutilizar su información citando
la fuente: «Origen de los datos: sitio web de lamoncloa.gob.es. Ministerio
de la Presidencia.»
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

import requests

from ingest.bluesky import SocialText

log = logging.getLogger(__name__)

USER_AGENT = ("polarmap-research/0.1 (academic media-polarization study; "
              "reads RSS titles and summaries only)")
TIMEOUT = 20
_LINK = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r'([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')
_TAG = re.compile(r"<[^>]+>")
_robots: dict[str, RobotFileParser | None] = {}


def _allowed(url: str) -> bool:
    host = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if host not in _robots:
        rp = RobotFileParser()
        try:
            r = requests.get(host + "/robots.txt", timeout=TIMEOUT,
                             headers={"User-Agent": USER_AGENT})
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            _robots[host] = rp
        except requests.RequestException:
            _robots[host] = None          # sin robots legible: se permite
    rp = _robots[host]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def _get(url: str) -> requests.Response | None:
    if not _allowed(url):
        log.info("  robots.txt no permite %s", url)
        return None
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as e:
        log.info("  no se pudo leer %s: %s", url, e)
        return None
    return r if r.status_code == 200 else None


def _is_feed(text: str) -> bool:
    head = text[:600].lower()
    return "<rss" in head or "<feed" in head or "<rdf:rdf" in head


def discover_feed(url: str) -> str | None:
    """Dirección del canal RSS/Atom de una web, o None."""
    r = _get(url)
    if r is not None and _is_feed(r.text):
        return url
    if r is not None:
        for tag in _LINK.findall(r.text[:200_000]):
            attrs = {m.group(1).lower(): (m.group(3) or m.group(4) or "")
                     for m in _ATTR.finditer(tag)}
            if attrs.get("rel", "").lower() == "alternate" and \
                    attrs.get("type", "").lower() in ("application/rss+xml", "application/atom+xml"):
                return urljoin(url, html.unescape(attrs.get("href", "")))
    wp = url.rstrip("/") + "/feed/"
    r2 = _get(wp)
    if r2 is not None and _is_feed(r2.text):
        return wp
    return None


def _clean(s: str | None, n: int = 400) -> str:
    s = html.unescape(_TAG.sub(" ", s or ""))
    return " ".join(s.split())[:n]


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)                       # RSS (RFC 822)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom (ISO)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(xml_text: str) -> list[dict]:
    """Entradas de un canal RSS 2.0, RSS 1.0 o Atom: título, resumen, fecha, enlace."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        f = {c.tag.rsplit("}", 1)[-1]: c for c in el}
        link = f.get("link")
        href = (link.get("href") or link.text) if link is not None else ""
        summary = f.get("description") if f.get("description") is not None else (
            f.get("summary") if f.get("summary") is not None else f.get("content"))
        date = None
        for k in ("pubDate", "published", "updated", "date"):
            if f.get(k) is not None:
                date = _date(f[k].text)
                if date:
                    break
        out.append({"title": _clean(f["title"].text if f.get("title") is not None else ""),
                    "summary": _clean(summary.text if summary is not None else ""),
                    "date": date, "link": (href or "").strip()})
    return out


def read_source(source: dict, start: datetime, end: datetime) -> tuple[list[SocialText], str | None]:
    """Notas de una fuente oficial en la ventana. Devuelve (textos, canal usado)."""
    feed = source.get("feed") or discover_feed(source["url"])
    if not feed:
        return [], None
    r = _get(feed)
    if r is None:
        return [], feed
    rows = []
    for it in parse_feed(r.content.decode(r.encoding or "utf-8", errors="replace")):
        if not it["date"] or not (start <= it["date"] <= end) or not it["title"]:
            continue
        text = it["title"] + (". " + it["summary"] if it["summary"] else "")
        rows.append(SocialText(
            layer="politica_oficial", source=source["name"], group=source["group"],
            text=text, created=it["date"].isoformat(), url=it["link"]))
    return rows, feed


def check_sources(sources: list[dict]) -> list[dict]:
    """Para cada fuente: canal encontrado y cuántas notas trae (sin filtrar fecha)."""
    out = []
    for s in sources:
        feed = s.get("feed") or discover_feed(s["url"])
        n, last = 0, None
        if feed:
            r = _get(feed)
            items = parse_feed(r.content.decode(r.encoding or "utf-8", errors="replace")) if r else []
            n = len(items)
            dates = [i["date"] for i in items if i["date"]]
            last = max(dates).isoformat() if dates else None
        out.append({**s, "feed_found": feed, "items": n, "last": last})
    return out
