"""
Titular real y entradilla de un artículo, desde las metaetiquetas de la página.

El titular que reconstruimos de la URL llega sin tildes, sin artículos y
cortado. Casi todos los medios publican en el <head> el titular original
(og:title / twitter:title / <title>) y un resumen de 1-2 frases
(og:description / description), visibles incluso detrás del muro de pago.

Reglas de uso:
  - Respeta robots.txt: si el medio no permite leer esa URL, no se pide.
  - Anota la reserva de minería de textos (TDMRep: cabecera o meta
    "tdm-reservation" = 1) para poder excluir esos medios si hace falta.
  - Solo se guardan titular y entradilla, nunca el cuerpo del artículo.
  - Una petición a la vez por medio, con pausa entre peticiones.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, asdict
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = ("polarmap-research/0.1 (academic media-polarization study; "
              "reads page metadata only)")
TIMEOUT = 15
MAX_BYTES = 400_000          # el <head> está al principio; no hace falta más
PAUSE_PER_DOMAIN = 1.0       # segundos entre peticiones al mismo medio

_META = re.compile(r"<meta\s+[^>]*>", re.I | re.S)
_ATTR = re.compile(r'([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')', re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


@dataclass
class Lead:
    url: str
    status: str               # ok | robots_disallowed | http_<code> | error | no_meta
    title: str = ""
    description: str = ""
    title_source: str = ""
    tdm_reserved: bool | None = None
    final_url: str = ""
    error: str = ""

    def to_dict(self):
        return asdict(self)


def _clean(text: str) -> str:
    # Algunos CMS codifican dos veces (&amp;aacute;): desescapar hasta estable.
    text = text or ""
    for _ in range(3):
        un = html.unescape(text)
        if un == text:
            break
        text = un
    return " ".join(text.split())


# " | Canarias7", " - La Gaceta de Salamanca": nombre del medio al final
_SITE_SUFFIX = re.compile(r"\s+[|\-–—]\s+[^|\-–—]{2,40}$")


def strip_site_suffix(title: str, domain: str) -> str:
    """Quita el nombre del medio del final del titular, si lo lleva."""
    m = _SITE_SUFFIX.search(title)
    if not m:
        return title
    tail = re.sub(r"[^a-z0-9]", "", m.group(0).lower())
    host = re.sub(r"[^a-z0-9]", "", domain.lower().removeprefix("www.").split(".")[0])
    # Solo si la cola se parece al medio (evita cortar "Madrid - Barcelona")
    if host and (host[:5] in tail or tail[:5] in host):
        return title[:m.start()].rstrip()
    return title


def parse_head(page: str) -> dict:
    """Metaetiquetas relevantes del HTML (tolerante: no usa un parser)."""
    metas = {}
    for tag in _META.findall(page):
        attrs = {}
        for m in _ATTR.finditer(tag):
            attrs[m.group(1).lower()] = m.group(3) if m.group(3) is not None else m.group(4)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in metas:
            metas[key] = _clean(attrs["content"])
    t = _TITLE.search(page)
    if t:
        metas.setdefault("<title>", _clean(re.sub(r"<[^>]+>", "", t.group(1))))
    return metas


def pick_lead(metas: dict) -> tuple[str, str, str]:
    """(titular, fuente del titular, entradilla)."""
    title, source = "", ""
    for k in ("og:title", "twitter:title", "<title>"):
        if metas.get(k):
            title, source = metas[k], k
            break
    desc = ""
    for k in ("og:description", "twitter:description", "description"):
        if metas.get(k):
            desc = metas[k]
            break
    return title, source, desc


class LeadFetcher:
    def __init__(self, pause: float = PAUSE_PER_DOMAIN):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT,
                                     "Accept-Language": "es,en;q=0.5"})
        self.pause = pause
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last: dict[str, float] = {}

    def _allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=TIMEOUT)
                if r.status_code >= 400:
                    rp = None          # sin robots.txt: permitido
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def _wait(self, netloc: str):
        last = self._last.get(netloc)
        if last is not None:
            delay = self.pause - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
        self._last[netloc] = time.monotonic()

    def fetch(self, url: str) -> Lead:
        if not self._allowed(url):
            return Lead(url, "robots_disallowed")
        self._wait(urlparse(url).netloc)
        try:
            r = self.session.get(url, timeout=TIMEOUT, stream=True,
                                 allow_redirects=True)
            raw = r.raw.read(MAX_BYTES, decode_content=True)
            r.close()
        except requests.RequestException as e:
            return Lead(url, "error", error=type(e).__name__)
        if r.status_code >= 400:
            return Lead(url, f"http_{r.status_code}", final_url=r.url)
        page = raw.decode(r.encoding or "utf-8", errors="replace")
        metas = parse_head(page)
        tdm = r.headers.get("tdm-reservation") or metas.get("tdm-reservation")
        title, source, desc = pick_lead(metas)
        title = strip_site_suffix(title, urlparse(r.url).netloc)
        return Lead(url, "ok" if title else "no_meta", title=title,
                    description=desc, title_source=source,
                    tdm_reserved=(str(tdm).strip() == "1") if tdm else None,
                    final_url=r.url)


# ─────────────────────────────────────────────────────────────
# Caché y enriquecimiento de artículos
# ─────────────────────────────────────────────────────────────

import json  # noqa: E402
from collections import defaultdict  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402

LEADS_CACHE = Path("data/cache/leads.jsonl")


def load_cache(path: Path = LEADS_CACHE) -> dict[str, dict]:
    cache = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                d = json.loads(line)
                cache[d["url"]] = d
            except (json.JSONDecodeError, KeyError):
                continue
    return cache


def fetch_leads(urls: list[str], cache_path: Path = LEADS_CACHE,
                workers: int = 8) -> dict[str, dict]:
    """
    Lead de cada URL, leyendo de caché y pidiendo solo las nuevas. En
    paralelo entre medios, en serie dentro de cada medio. Solo se cachean
    las respuestas definitivas (ok, robots, 4xx), no los errores de red.
    """
    cache = load_cache(cache_path)
    todo = [u for u in dict.fromkeys(urls) if u not in cache]
    if todo:
        fetcher = LeadFetcher()
        by_domain = defaultdict(list)
        for u in todo:
            by_domain[urlparse(u).netloc].append(u)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=workers) as pool, \
                cache_path.open("a") as out:
            for chunk in pool.map(lambda g: [fetcher.fetch(u) for u in g],
                                  by_domain.values()):
                for lead in chunk:
                    d = lead.to_dict()
                    if lead.status != "error":
                        out.write(json.dumps(d, ensure_ascii=False) + "\n")
                    cache[lead.url] = d
    return {u: cache[u] for u in urls if u in cache}
