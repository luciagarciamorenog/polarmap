"""
Comentarios de YouTube en los vídeos de medios y partidos, con la API de
datos de YouTube (gratuita: 10.000 unidades al día por proyecto).

Coste en unidades de una tirada: 1 por canal (resolver @handle → lista de
subidas), 1 por cada página de vídeos recientes y 1 por cada página de 100
comentarios. Con 40 canales y ~4.000 comentarios son unas 150 unidades.

La clave va en .env como YOUTUBE_API_KEY (nunca en git).

Privacidad: de quien comenta solo se guarda un seudónimo (hash con sal).
El canal y el título del vídeo sí, porque son de medios y partidos.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import requests

from ingest.bluesky import SocialText, pseudonym

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
TIMEOUT = 20


class YouTubeKeyError(RuntimeError):
    """Sin clave o clave inválida: no tiene sentido seguir."""


def _get(path: str, params: dict) -> dict | None:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise YouTubeKeyError("falta YOUTUBE_API_KEY en .env")
    try:
        r = requests.get(f"{API}/{path}", params={**params, "key": key},
                         timeout=TIMEOUT)
    except requests.RequestException as e:
        log.warning("youtube %s: %s", path, e)
        return None
    if r.status_code == 200:
        return r.json()
    reason = ""
    try:
        reason = r.json()["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError):
        pass
    if reason in ("keyInvalid", "accessNotConfigured"):
        raise YouTubeKeyError(f"clave de YouTube no válida ({reason})")
    if reason == "quotaExceeded":
        raise YouTubeKeyError("cuota diaria de YouTube agotada; vuelve mañana")
    # commentsDisabled, videoNotFound...: se salta ese vídeo
    return None


def resolve_channel(handle: str) -> dict | None:
    """@handle → id del canal, nombre y lista de subidas."""
    d = _get("channels", {"part": "contentDetails,snippet",
                          "forHandle": handle if handle.startswith("@") else "@" + handle})
    items = (d or {}).get("items") or []
    if not items:
        return None
    it = items[0]
    return {"id": it["id"], "title": it["snippet"]["title"],
            "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"]}


def recent_videos(uploads: str, start: datetime, end: datetime,
                  max_videos: int = 10) -> list[dict]:
    d = _get("playlistItems", {"part": "contentDetails,snippet",
                               "playlistId": uploads, "maxResults": 50})
    out = []
    for it in (d or {}).get("items", []):
        ts = it.get("contentDetails", {}).get("videoPublishedAt")
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        if start <= t <= end:
            out.append({"id": it["contentDetails"]["videoId"],
                        "title": it["snippet"].get("title", ""),
                        "published": t.isoformat()})
    return out[:max_videos]


def top_comments(video: dict, channel: str, max_n: int = 100) -> list[SocialText]:
    """Comentarios principales del vídeo, los más relevantes primero."""
    d = _get("commentThreads", {"part": "snippet", "videoId": video["id"],
                                "maxResults": min(100, max_n), "order": "relevance",
                                "textFormat": "plainText"})
    rows = []
    for it in (d or {}).get("items", []):
        s = it["snippet"]["topLevelComment"]["snippet"]
        text = (s.get("textOriginal") or s.get("textDisplay") or "").strip()
        if len(text) < 15:
            continue
        rows.append(SocialText(
            layer="youtube_comentarios",
            source=pseudonym((s.get("authorChannelId") or {}).get("value", "")),
            group="publico", text=text[:600], created=s.get("publishedAt", ""),
            url=f"https://www.youtube.com/watch?v={video['id']}",
            parent=f"{channel} · {video['title'][:80]}",
            likes=int(s.get("likeCount") or 0)))
    return rows[:max_n]


def search_channels(query: str, n: int = 3) -> list[dict]:
    """Canales candidatos por nombre (search.list: 100 unidades por búsqueda)."""
    d = _get("search", {"part": "snippet", "q": query, "type": "channel",
                        "maxResults": n, "regionCode": "ES", "relevanceLanguage": "es"})
    ids = [it["snippet"]["channelId"] for it in (d or {}).get("items", [])]
    if not ids:
        return []
    d2 = _get("channels", {"part": "snippet,statistics", "id": ",".join(ids)})
    return [{"handle": it["snippet"].get("customUrl"), "title": it["snippet"]["title"],
             "subscribers": int(it.get("statistics", {}).get("subscriberCount") or 0)}
            for it in (d2 or {}).get("items", [])]


def check_channels(channels: list[dict]) -> list[dict]:
    out = []
    for c in channels:
        info = resolve_channel(c["handle"]) if c.get("handle") else None
        row = {**c, "found": bool(info)}
        if info:
            row["title"] = info["title"]
        else:
            row["candidates"] = search_channels(c["name"])
        out.append(row)
        time.sleep(0.1)
    return out
