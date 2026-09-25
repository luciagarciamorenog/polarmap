"""
Llamadas al modelo del piloto. Cada una responde una pregunta concreta y
devuelve datos verificables; lo que no se puede verificar se descarta y se
cuenta, nunca se rellena.

    rank_sets       ideológica: ordena cada conjunto de 5 noticias de la más
                    cercana al polo A a la más cercana al polo B
    code_frames     narrativa: encuadre dominante y conflictividad
    (la afectiva reutiliza analysis.discover.classify_stances v2)
"""

from __future__ import annotations

import json
import os

from analysis import runner
from analysis.discover import _cached_system, _json_array

RANK_MODEL = "claude-sonnet-4-6"
FRAME_MODEL = "claude-haiku-4-5"
SETS_PER_CALL = 8
FRAMES_PER_CALL = 40

# Encuadres genéricos de Semetko y Valkenburg (2000) + «informativo».
# Describen CÓMO se cuenta la historia, no a qué bando favorece.
FRAMES = {
    "conflict": "conflicto: enfrentamiento entre partes, ganadores y perdedores",
    "human_interest": "interés humano: una persona o víctima, emoción, historia personal",
    "economic": "consecuencias económicas: dinero, costes, empleo, mercados",
    "morality": "moral: valores, lo justo o injusto, lo que está bien o mal",
    "responsibility": "responsabilidad: quién tiene la culpa o quién debe resolverlo",
    "informative": "informativo: dato, servicio, agenda o trámite sin encuadre dominante",
}


MODE = os.environ.get("POLARMAP_API_MODE", "batch")   # batch | sync


def rank_sets(sets: list[list[str]], pole_a: str, pole_b: str,
              model: str = RANK_MODEL) -> list[list[int] | None]:
    """
    `sets`: listas de textos (titular. entradilla). Devuelve, por conjunto,
    el orden de índices locales de más A a más B, o None si la respuesta
    no es una permutación válida.
    """
    system = f"""Vas a comparar noticias entre sí dentro de cada conjunto.

EJE DEL DEBATE:
  Polo A: {pole_a}
  Polo B: {pole_b}

En cada conjunto, ordena TODAS las noticias de la más cercana al polo A a la más cercana al polo B.
Cuenta la postura explícita y también la selección de hechos que favorece a un bando. No cuentes el tono ni la forma de contar (eso es otra dimensión). Sitúa cada noticia por lo que dice del asunto, no por a qué partido perjudica.
Si dos noticias están igual de cerca, ordénalas igualmente: cada número aparece una sola vez.

Responde SOLO con un array JSON, un objeto por conjunto:
[{{"set": 1, "order": [3, 1, 5, 2, 4]}}]"""
    chunks = [sets[c:c + SETS_PER_CALL] for c in range(0, len(sets), SETS_PER_CALL)]
    bodies = []
    for chunk in chunks:
        blocks = []
        for si, s in enumerate(chunk, 1):
            lines = "\n".join(f"  {j}. {t}" for j, t in enumerate(s, 1))
            blocks.append(f"CONJUNTO {si}\n{lines}")
        bodies.append({"model": model, "max_tokens": 1200, "temperature": 0,
                       "system": _cached_system(system),
                       "messages": [{"role": "user", "content": "\n\n".join(blocks)}]})
    out: list[list[int] | None] = []
    for chunk, text in zip(chunks, runner.run(bodies, "piloto-ordenacion", MODE)):
        items = _json_array(text or "") or []
        by_set = {it.get("set"): it.get("order") for it in items if isinstance(it, dict)}
        for si, s in enumerate(chunk, 1):
            order = by_set.get(si)
            ok = (isinstance(order, list)
                  and sorted(order) == list(range(1, len(s) + 1)))
            out.append([o - 1 for o in order] if ok else None)
    return out


def code_frames(texts: list[str], model: str = FRAME_MODEL
                ) -> list[dict | None]:
    """Encuadre dominante + conflictividad (0, 0,5, 1) por noticia."""
    legend = "\n".join(f"  {k}: {v}" for k, v in FRAMES.items())
    system = f"""Codifica CÓMO se cuenta cada noticia, no a qué bando favorece.

ENCUADRES (elige el dominante):
{legend}

conflict_level: 0 sin conflicto · 0.5 disputa, crítica o daño limitado · 1 violencia, crisis o confrontación directa.

Responde SOLO con un array JSON:
[{{"id": 1, "frame": "informative", "conflict_level": 0}}]"""
    chunks = [texts[c:c + FRAMES_PER_CALL] for c in range(0, len(texts), FRAMES_PER_CALL)]
    bodies = [{"model": model, "max_tokens": 3000, "temperature": 0,
               "system": _cached_system(system),
               "messages": [{"role": "user", "content": "NOTICIAS:\n" + "\n".join(
                   f"{j}. {t}" for j, t in enumerate(chunk, 1))}]}
              for chunk in chunks]
    out: list[dict | None] = []
    for chunk, text in zip(chunks, runner.run(bodies, "piloto-encuadres", MODE)):
        items = _json_array(text or "") or []
        by_id = {it.get("id"): it for it in items if isinstance(it, dict)}
        for j in range(1, len(chunk) + 1):
            it = by_id.get(j) or {}
            ok = it.get("frame") in FRAMES and it.get("conflict_level") in (0, 0.5, 1)
            out.append({"frame": it["frame"], "conflict": float(it["conflict_level"])}
                       if ok else None)
    return out


def load_or_run(path, fn, *args, **kwargs):
    """Caché en disco: una corrida repetida no vuelve a pagar la API."""
    if path.exists():
        return json.loads(path.read_text())
    result = fn(*args, **kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False))
    return result
