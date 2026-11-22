"""
Publicaciones y respuestas de Bluesky, por la API pública (sin clave).

Bluesky publica sus datos abiertos: perfil, publicaciones de una cuenta y
el hilo de respuestas de cada publicación se leen con peticiones HTTP sin
cuenta ni clave (public.api.bsky.app).

Qué se guarda y qué no:
  - De las cuentas de la lista (partidos, políticos, periodistas, medios):
    texto, fecha, URL y nombre de la cuenta. Son cuentas públicas de
    figuras públicas y se pueden nombrar.
  - De las respuestas de usuarios particulares: solo el texto, la fecha y
    un identificador seudónimo (hash con sal) para contar cuántas personas
    distintas hay. Nunca el nombre de usuario. Solo se usan en agregado.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

API = "https://public.api.bsky.app/xrpc"
TIMEOUT = 20
PAUSE = 0.15                 # segundos entre peticiones (muy por debajo del límite)
LANGS = {"es", "ca", "eu", "gl"}


@dataclass
class SocialText:
    layer: str          # bluesky_cuentas | bluesky_respuestas | youtube_comentarios
    source: str         # cuenta de la lista (handle) o canal; seudónimo en respuestas
    group: str          # medio | partido | politico | periodista | publico
    text: str
    created: str
    url: str
    parent: str = ""    # cuenta o canal a la que responde / en cuyo vídeo comenta
    likes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def pseudonym(user_id: str) -> str:
    """Seudónimo estable por corrida: hash con sal, sin poder volver atrás."""
    salt = os.environ.get("POLARMAP_SALT", "polarmap")
    return "u_" + hashlib.sha256((salt + user_id).encode()).hexdigest()[:12]


def _get(method: str, params: dict) -> dict | None:
    for attempt in range(3):
        try:
            r = requests.get(f"{API}/{method}", params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            log.warning("bluesky %s: %s", method, e)
            time.sleep(2 ** attempt)
            continue
        time.sleep(PAUSE)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** (attempt + 1))
            continue
        return None          # 400: cuenta inexistente, post borrado...
    return None


def get_profile(handle: str) -> dict | None:
    return _get("app.bsky.actor.getProfile", {"actor": handle})


def search_actors(query: str, limit: int = 5) -> list[dict]:
    d = _get("app.bsky.actor.searchActors", {"q": query, "limit": limit})
    return (d or {}).get("actors", [])


def _post_url(post: dict) -> str:
    rkey = post.get("uri", "").rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{post.get('author', {}).get('handle', '')}/post/{rkey}"


def _created(post: dict) -> datetime | None:
    s = (post.get("record") or {}).get("createdAt") or post.get("indexedAt")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def account_posts(handle: str, group: str, start: datetime, end: datetime,
                  max_pages: int = 5) -> list[tuple[SocialText, dict]]:
    """
    Publicaciones propias de la cuenta en la ventana (sin reposts ni
    respuestas suyas). Devuelve (texto, post original) para poder pedir
    después sus respuestas.
    """
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"actor": handle, "limit": 100, "filter": "posts_no_replies"}
        if cursor:
            params["cursor"] = cursor
        d = _get("app.bsky.feed.getAuthorFeed", params)
        if not d:
            break
        older = False
        for item in d.get("feed", []):
            if item.get("reason"):            # repost de otra cuenta
                continue
            post = item.get("post", {})
            ts = _created(post)
            if ts is None:
                continue
            if ts < start:
                older = True
                continue
            if ts > end:
                continue
            text = ((post.get("record") or {}).get("text") or "").strip()
            if len(text) < 20:                # «👇», enlaces sueltos
                continue
            out.append((SocialText(
                layer="bluesky_cuentas", source=handle, group=group, text=text,
                created=ts.isoformat(), url=_post_url(post),
                likes=int(post.get("likeCount") or 0)), post))
        cursor = d.get("cursor")
        if older or not cursor:
            break
    return out


def post_replies(post: dict, parent: str, max_n: int = 20) -> list[SocialText]:
    """Respuestas directas a una publicación, las más apoyadas primero."""
    if not post.get("replyCount"):
        return []
    d = _get("app.bsky.feed.getPostThread",
             {"uri": post["uri"], "depth": 1, "parentHeight": 0})
    replies = ((d or {}).get("thread") or {}).get("replies") or []
    rows = []
    for rp in replies:
        p = rp.get("post") or {}
        rec = p.get("record") or {}
        text = (rec.get("text") or "").strip()
        langs = set(rec.get("langs") or [])
        if len(text) < 15 or (langs and not langs & LANGS):
            continue
        ts = _created(p)
        rows.append(SocialText(
            layer="bluesky_respuestas",
            source=pseudonym(p.get("author", {}).get("did", "")),
            group="publico", text=text,
            created=ts.isoformat() if ts else "", url="", parent=parent,
            likes=int(p.get("likeCount") or 0)))
    rows.sort(key=lambda r: -r.likes)
    return rows[:max_n]


def check_accounts(accounts: list[dict]) -> list[dict]:
    """
    Comprueba cada cuenta de la lista: si existe, cuántos seguidores y
    publicaciones tiene. Las que no existen se buscan por nombre y se
    proponen candidatas, para revisarlas a mano antes de usarlas.
    """
    out = []
    for a in accounts:
        prof = get_profile(a["handle"]) if a.get("handle") else None
        row = {**a, "found": bool(prof)}
        if prof:
            row.update(followers=prof.get("followersCount"),
                       posts=prof.get("postsCount"),
                       display=prof.get("displayName"))
        else:
            row["candidates"] = [
                {"handle": x.get("handle"), "display": x.get("displayName"),
                 "followers": x.get("followersCount")}
                for x in search_actors(a["name"], limit=3)]
        out.append(row)
    return out
