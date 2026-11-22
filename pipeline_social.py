"""
PolarMap · capa social: Bluesky (cuentas y respuestas) y comentarios de YouTube.

Se lanza DESPUÉS de una corrida de prensa y usa sus mismos debates, polos y
ventana de 24 h, para que prensa y redes se midan con la misma regla:

    python3 pipeline_social.py --press-run snapshot_ES_20261126

Primero, una vez, comprobar la lista de cuentas y canales (gratis, sin Claude):

    python3 pipeline_social.py --check

Capas:
  politica_oficial     notas de prensa del Gobierno (La Moncloa) y de los partidos
  bluesky_cuentas      lo que publican partidos, políticos, periodistas y medios
  bluesky_respuestas   lo que les responde el público (seudónimos, solo agregado)
  youtube_comentarios  comentarios en vídeos de medios y partidos (seudónimos)

Coste: leer Bluesky y YouTube es gratis; Claude asigna cada texto a un
debate (Haiku) y puntúa los que tratan de alguno (Sonnet), por lotes.
Salida:
  data/social/<run>_social.jsonl                 un texto por línea (no va a git)
  data/internal/<run>_social_report.json          prensa frente a redes, por debate
  data/internal/<run>_social_usage.json           coste real
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from analysis import runner  # noqa: E402
from analysis.discover import (  # noqa: E402
    ASSIGN_NONE, build_assign_bodies, build_classify_bodies, parse_assign,
    parse_classify,
)
from analysis.ecosystem import layers_report, layers_summary_lines  # noqa: E402
from ingest import bluesky, official, youtube  # noqa: E402
from pipeline_discover import (  # noqa: E402
    INTERNAL_DIR, OUT_DIR, _run_grouped, load_env,
)

log = logging.getLogger("polarmap.social")

ACCOUNTS_PATH = OUT_DIR / "social_accounts.json"
SOCIAL_DIR = OUT_DIR / "social"
MIN_PER_DEBATE = 10

SOCIAL_PREFIX = ("PUBLICACIONES EN REDES SOCIALES (no son noticias: aplica las "
                 "mismas reglas, y la postura es la de quien publica):\n")


def _as_social(body: dict) -> dict:
    """Mismo prompt que la prensa; solo cambia la cabecera del mensaje."""
    msg = body["messages"][0]["content"]
    body["messages"][0]["content"] = SOCIAL_PREFIX + msg.split(":\n", 1)[-1]
    return body


def load_press_run(stem: str, country: str) -> tuple[datetime, list[dict]]:
    """Fin de ventana y debates (con sus polos) de la corrida de prensa."""
    full = json.loads((INTERNAL_DIR / f"{stem}_full.json").read_text())
    end = datetime.fromisoformat(full["ts"].replace("Z", "+00:00"))
    debates = [t["axis"] for t in full["raw_topic_results"].get(country, [])
               if isinstance(t.get("axis"), dict) and t["axis"].get("pole_a")]
    return end, debates


# ─────────────────────────────────────────────────────────────
# Recogida
# ─────────────────────────────────────────────────────────────

def collect(accounts: dict, start: datetime, end: datetime, sources: set[str],
            max_replies_per_post: int, max_replies: int,
            max_comments_per_video: int, max_comments: int) -> list[bluesky.SocialText]:
    texts: list[bluesky.SocialText] = []

    if sources & {"bluesky", "replies"}:
        posts = []
        listed = [a for a in accounts["bluesky"] if a.get("handle")]
        for i, a in enumerate(listed, 1):
            got = bluesky.account_posts(a["handle"], a["group"], start, end)
            posts.extend(got)
            if i % 20 == 0 or i == len(listed):
                log.info("  bluesky: %d/%d cuentas · %d publicaciones", i, len(listed), len(posts))
        if "bluesky" in sources:
            texts += [t for t, _ in posts]
        if "replies" in sources:
            n = 0
            # Primero las publicaciones con más respuestas: ahí está la conversación
            for t, post in sorted(posts, key=lambda x: -(x[1].get("replyCount") or 0)):
                if n >= max_replies:
                    break
                rs = bluesky.post_replies(post, parent=t.source, max_n=max_replies_per_post)
                texts += rs[:max_replies - n]
                n += len(rs[:max_replies - n])
            log.info("  bluesky: %d respuestas", n)

    if "oficial" in sources:
        for src in accounts.get("oficial", []):
            rows, feed = official.read_source(src, start, end)
            texts += rows
            log.info("  oficial: %-24s %3d notas%s", src["name"][:24], len(rows),
                     "" if feed else " (sin canal RSS encontrado)")

    if "youtube" in sources:
        n = 0
        for c in accounts["youtube"]:
            if n >= max_comments:
                break
            info = youtube.resolve_channel(c["handle"])
            if not info:
                log.info("  youtube: canal no encontrado %s", c["handle"])
                continue
            for v in youtube.recent_videos(info["uploads"], start, end):
                if n >= max_comments:
                    break
                cs = youtube.top_comments(v, info["title"], max_n=max_comments_per_video)
                texts += cs[:max_comments - n]
                n += len(cs[:max_comments - n])
        log.info("  youtube: %d comentarios", n)

    # Sin duplicados exactos (la misma consigna pegada 30 veces cuenta una)
    seen, out = set(), []
    for t in texts:
        k = (t.layer, " ".join(t.text.lower().split()))
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


# ─────────────────────────────────────────────────────────────
# Clasificación (mismos debates y codebook que la prensa)
# ─────────────────────────────────────────────────────────────

def classify(texts: list[bluesky.SocialText], debates: list[dict], mode: str) -> list[dict]:
    raw = [" ".join(t.text.split()) for t in texts]   # una línea por texto
    a_bodies = [(_as_social(b), n) for b, n in build_assign_bodies(raw, debates)]
    a_texts = _run_grouped({"all": a_bodies}, "asignacion_redes", mode)["all"]
    assigned = parse_assign(a_texts, a_bodies, len(debates))

    records = []
    for t, k in zip(texts, assigned):
        rec = t.to_dict()
        rec["topic"] = debates[k - 1]["label"] if k > 0 else (None if k == ASSIGN_NONE else "__failed__")
        records.append(rec)
    log.info("  asignación: %s", dict(Counter(r["topic"] for r in records).most_common()))

    groups, idx = {}, {}
    for k, d in enumerate(debates, 1):
        ids = [i for i, a in enumerate(assigned) if a == k]
        if len(ids) < MIN_PER_DEBATE:
            continue
        idx[k] = ids
        groups[k] = [(_as_social(b), chunk) for b, chunk in build_classify_bodies(
            [raw[i] for i in ids], d["pole_a"], d["pole_b"])]
    c_texts = _run_grouped(groups, "postura_redes", mode) if groups else {}
    for k, ids in idx.items():
        for i, r in zip(ids, parse_classify(c_texts[k], groups[k])):
            records[i].update(
                stance=round(r.stance, 3), hostility=round(r.hostility, 3),
                conflict=round(r.conflict, 3) if r.conflict == r.conflict else None,
                us_them=r.us_them, bloc=round(r.bloc, 3) if r.bloc == r.bloc else None,
                why=r.why or None, confidence=round(r.confidence, 3),
                off_topic=r.off_topic, stance_failed=r.failed)
    return records


# ─────────────────────────────────────────────────────────────
# Comprobación de la lista
# ─────────────────────────────────────────────────────────────

def run_check(accounts: dict) -> None:
    log.info("Comprobando %d cuentas de Bluesky…", len(accounts["bluesky"]))
    bs = bluesky.check_accounts(accounts["bluesky"])
    found = [a for a in bs if a["found"]]
    log.info("")
    log.info("BLUESKY: %d de %d encontradas", len(found), len(bs))
    for g in ("medio", "partido", "politico", "periodista"):
        sel = [a for a in bs if a["group"] == g]
        log.info("  %-10s %d/%d", g, sum(a["found"] for a in sel), len(sel))
    log.info("")
    for a in bs:
        if a["found"]:
            log.info("  ✓ %-28s %-30s %7s seguidores", a["name"][:28], a["handle"], a.get("followers"))
    for a in bs:
        if not a["found"]:
            cands = ", ".join(f"{c['handle']} ({c.get('followers')})" for c in a.get("candidates", []))
            log.info("  ✗ %-28s candidatas: %s", a["name"][:28], cands or "ninguna")
    log.info("")
    log.info("Comprobando %d webs oficiales (Gobierno y partidos)…", len(accounts.get("oficial", [])))
    of = official.check_sources(accounts.get("oficial", []))
    log.info("OFICIAL: %d de %d con canal RSS", sum(bool(o["feed_found"]) for o in of), len(of))
    for o in of:
        log.info("  %s %-24s %s · %d notas · última %s", "✓" if o["feed_found"] else "✗",
                 o["name"][:24], o["feed_found"] or o["url"], o["items"], o["last"])
    yt = []
    try:
        yt = youtube.check_channels(accounts["youtube"])
        log.info("")
        log.info("YOUTUBE: %d de %d canales encontrados", sum(c["found"] for c in yt), len(yt))
        for c in yt:
            log.info("  %s %-28s %s", "✓" if c["found"] else "✗", c["name"][:28],
                     c.get("title", c["handle"]))
    except youtube.YouTubeKeyError as e:
        log.info("YOUTUBE: %s", e)
    out = INTERNAL_DIR / "social_accounts_check.json"
    out.write_text(json.dumps({"bluesky": bs, "youtube": yt, "oficial": of},
                              ensure_ascii=False, indent=1, default=str))
    log.info("")
    log.info("Detalle en %s. Revisa las candidatas antes de añadirlas a %s.", out, ACCOUNTS_PATH)


# ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="PolarMap · capa social")
    ap.add_argument("--press-run", help="corrida de prensa, p. ej. snapshot_ES_20261126")
    ap.add_argument("--country", default="ES")
    ap.add_argument("--sources", default="oficial,bluesky,replies,youtube",
                    help="oficial (Gobierno y partidos), bluesky (cuentas), replies (respuestas), youtube")
    ap.add_argument("--max-replies-per-post", type=int, default=20)
    ap.add_argument("--max-replies", type=int, default=3000)
    ap.add_argument("--max-comments-per-video", type=int, default=100)
    ap.add_argument("--max-comments", type=int, default=3000)
    ap.add_argument("--api-mode", choices=("batch", "sync"), default="batch")
    ap.add_argument("--check", action="store_true",
                    help="solo comprobar la lista de cuentas y canales (gratis)")
    ap.add_argument("--dry-run", action="store_true",
                    help="recoger y contar, sin llamar a Claude (gratis)")
    args = ap.parse_args()

    load_env()
    accounts = json.loads(ACCOUNTS_PATH.read_text())
    if args.check:
        run_check(accounts)
        return
    if not args.press_run:
        ap.error("falta --press-run (o usa --check)")

    end, debates = load_press_run(args.press_run, args.country)
    start = end - timedelta(hours=24)
    sources = {s.strip() for s in args.sources.split(",") if s.strip()}
    log.info("Ventana %s → %s · %d debates de la prensa · fuentes: %s",
             start.isoformat(), end.isoformat(), len(debates), ", ".join(sorted(sources)))
    for d in debates:
        log.info("  %s: %s ↔ %s", d["label"], d["pole_a"][:50], d["pole_b"][:50])

    texts = collect(accounts, start, end, sources, args.max_replies_per_post,
                    args.max_replies, args.max_comments_per_video, args.max_comments)
    by_layer = Counter(t.layer for t in texts)
    log.info("Recogidos %d textos: %s", len(texts), dict(by_layer))
    if args.dry_run or not texts:
        log.info("--- DRY RUN: no se llama a Claude --- (coste estimado ~%.2f $)",
                 len(texts) * 0.09 / 1000 + len(texts) * 0.25 * 0.8 / 1000)
        return

    runner.LEDGER.reset()
    stem = args.press_run
    try:
        records = classify(texts, debates, args.api_mode)
    finally:
        usage = runner.LEDGER.summary()
        usage.update(n_texts=len(texts), by_layer=dict(by_layer),
                     usd_per_1000_texts=usage["usd_total"] / max(1, len(texts)) * 1000)
        INTERNAL_DIR.mkdir(parents=True, exist_ok=True)
        (INTERNAL_DIR / f"{stem}_social_usage.json").write_text(
            json.dumps(usage, ensure_ascii=False, indent=2))
        log.info("CONSUMO REAL: %.4f $ (%d textos)", usage["usd_total"], len(texts))

    SOCIAL_DIR.mkdir(parents=True, exist_ok=True)
    (SOCIAL_DIR / f"{stem}_social.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

    press_path = INTERNAL_DIR / f"{stem}_headlines.jsonl"
    press = ([json.loads(l) for l in press_path.read_text().splitlines() if l.strip()]
             if press_path.exists() else [])
    press = [r for r in press if r.get("country") == args.country]
    rep = layers_report(press, records, accounts)
    (INTERNAL_DIR / f"{stem}_social_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    log.info("")
    log.info("PRENSA FRENTE A REDES")
    for line in layers_summary_lines(rep):
        log.info("%s", line)


if __name__ == "__main__":
    main()
