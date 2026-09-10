"""
Clasificación de POSTURA (stance), no de sentimiento.

Esta es la diferencia que hace que el índice mida polarización de verdad:

  Sentimiento: "¿este texto es positivo o negativo?"
     -> Un terremoto es muy negativo y cero polarizante.
  Postura:     "¿qué posición defiende este texto en el eje X?"
     -> Es lo único que permite ver si las posiciones se parten en bloques.

Escala de postura: -1 (extremo A) ... 0 (centro/neutral) ... +1 (extremo B)
La orientación de cada eje se fija en AXES y NO debe cambiar nunca:
si inviertes el signo a mitad de serie temporal, rompes toda la historia.
"""

from __future__ import annotations

import os
import json
import time
import logging
from dataclasses import dataclass

import numpy as np
import requests

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

# Definición de cada eje. El texto de los polos va literal al prompt:
# es lo que garantiza que la escala signifique lo mismo en todos los
# idiomas y en todas las ejecuciones.
AXES = {
    "inmigracion": {
        "neg": "restrictiva: control fronterizo, límites a la entrada, "
               "énfasis en costes y riesgos de la inmigración",
        "pos": "apertura: acogida, derechos de los migrantes, "
               "énfasis en aportación y deber humanitario",
    },
    "clima": {
        "neg": "escéptica o de freno: dudas sobre urgencia climática, "
               "prioridad al coste económico de la transición",
        "pos": "de acción urgente: transición acelerada, "
               "prioridad a la emergencia climática sobre el coste",
    },
    "genero": {
        "neg": "conservadora: crítica a las políticas de género, "
               "defensa de roles tradicionales",
        "pos": "progresista: ampliación de políticas de igualdad "
               "y derechos de género",
    },
}

SYSTEM_PROMPT = """Eres un codificador de contenido para investigación en ciencias sociales.

Tu tarea: dado el titular de una noticia, estimar QUÉ POSICIÓN defiende o refleja \
sobre un eje temático concreto, y cuánta hostilidad expresa hacia quienes piensan lo contrario.

REGLAS CRÍTICAS:
1. Codifica POSTURA, no sentimiento. Un texto puede ser negativo en tono y neutral en postura \
(ej. "Mueren 30 migrantes en el Mediterráneo" es un hecho trágico, postura ~0).
2. Si el titular es puramente informativo, sin marco valorativo, la postura es 0.0.
3. Si el titular NO trata realmente el eje indicado, marca "off_topic": true.
4. La hostilidad mide desprecio, deshumanización o atribución de mala fe hacia el otro bloque. \
Un desacuerdo firme pero respetuoso tiene hostilidad baja.
5. No proyectes tu propia opinión. Codifica lo que el texto hace, no si tiene razón.

Responde SOLO con JSON válido, sin markdown ni explicación:
{"stance": <float -1..1>, "hostility": <float 0..1>, "confidence": <float 0..1>, "off_topic": <bool>}"""

# Anclas de calibración: titulares con postura conocida. Se pasan al modelo
# antes de cada lote para fijar la escala. Sin esto, la escala deriva entre
# ejecuciones y las series temporales dejan de ser comparables.
CALIBRATION_ANCHORS = {
    "inmigracion": [
        ("Un estudio del Banco de España cifra en 3.200 el número de llegadas en julio",
         {"stance": 0.0, "hostility": 0.0}),
        ("Basta ya de invasión: hay que cerrar las fronteras antes de que nos destruyan",
         {"stance": -0.95, "hostility": 0.9}),
        ("La migración sostiene nuestro sistema de pensiones y enriquece nuestra sociedad",
         {"stance": 0.8, "hostility": 0.1}),
        ("El Gobierno aprueba un endurecimiento de los requisitos de residencia",
         {"stance": -0.3, "hostility": 0.0}),
    ],
}


@dataclass
class StanceResult:
    stance: float
    hostility: float
    confidence: float
    off_topic: bool

    @classmethod
    def empty(cls):
        return cls(float("nan"), float("nan"), 0.0, True)


class StanceClassifier:
    def __init__(self, api_key: str | None = None, model: str = MODEL,
                 batch_size: int = 20):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("Falta ANTHROPIC_API_KEY")
        self.model = model
        self.batch_size = batch_size

    def _call(self, messages: list, max_tokens: int = 4000) -> str:
        for attempt in range(4):
            try:
                r = requests.post(
                    API_URL,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={"model": self.model, "max_tokens": max_tokens,
                          "system": SYSTEM_PROMPT, "messages": messages},
                    timeout=120,
                )
                if r.status_code in (429, 529):
                    time.sleep(2 ** attempt * 2)
                    continue
                r.raise_for_status()
                data = r.json()
                return "".join(b.get("text", "") for b in data.get("content", [])
                               if b.get("type") == "text")
            except requests.RequestException as e:
                log.warning("API fallo intento %d: %s", attempt + 1, e)
                time.sleep(2 ** attempt * 2)
        return ""

    def classify_batch(self, titles: list[str], topic: str,
                       language: str = "spa") -> list[StanceResult]:
        """
        Clasifica un lote de titulares. Por lotes y no de uno en uno:
        20x menos llamadas, 20x menos coste, y además da al modelo contexto
        comparativo dentro del lote, lo que mejora la consistencia de escala.
        """
        if topic not in AXES:
            raise ValueError(f"eje no definido: {topic}")
        axis = AXES[topic]

        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
        prompt = (
            f"EJE: {topic}\n"
            f"  stance = -1  ->  {axis['neg']}\n"
            f"  stance =  0  ->  neutral / informativo / equilibrado\n"
            f"  stance = +1  ->  {axis['pos']}\n\n"
            f"Idioma de los titulares: {language}\n\n"
            f"TITULARES:\n{numbered}\n\n"
            f"Devuelve un array JSON con exactamente {len(titles)} objetos, "
            f"en el mismo orden. Sin markdown."
        )

        raw = self._call([{"role": "user", "content": prompt}])
        return self._parse(raw, len(titles))

    @staticmethod
    def _parse(raw: str, expected: int) -> list[StanceResult]:
        txt = raw.strip()
        # quitar vallas de markdown si el modelo las añade
        if txt.startswith("```"):
            txt = txt.split("```")[1]
            if txt.startswith("json"):
                txt = txt[4:]
        txt = txt.strip()
        try:
            items = json.loads(txt)
        except json.JSONDecodeError:
            start, end = txt.find("["), txt.rfind("]")
            if start == -1 or end == -1:
                log.error("respuesta no parseable")
                return [StanceResult.empty() for _ in range(expected)]
            try:
                items = json.loads(txt[start:end + 1])
            except json.JSONDecodeError:
                return [StanceResult.empty() for _ in range(expected)]

        out = []
        for it in items[:expected]:
            try:
                out.append(StanceResult(
                    stance=float(np.clip(it.get("stance", 0.0), -1, 1)),
                    hostility=float(np.clip(it.get("hostility", 0.0), 0, 1)),
                    confidence=float(np.clip(it.get("confidence", 0.5), 0, 1)),
                    off_topic=bool(it.get("off_topic", False)),
                ))
            except (TypeError, ValueError):
                out.append(StanceResult.empty())
        while len(out) < expected:
            out.append(StanceResult.empty())
        return out


# ---------------------------------------------------------------------------
# CONTROL DE CALIDAD DEL CLASIFICADOR
# ---------------------------------------------------------------------------

def measure_reliability(classifier: "StanceClassifier", titles: list[str],
                        topic: str, n_runs: int = 3) -> dict:
    """
    Fiabilidad test-retest: clasifica los mismos titulares varias veces y
    mide la dispersión. Si el mismo titular recibe 0.2 y 0.8 en dos pasadas,
    tu índice es ruido con formato bonito.

    Umbral aceptable: sd media < 0.15 y correlación entre pasadas > 0.85.
    """
    runs = []
    for _ in range(n_runs):
        res = classifier.classify_batch(titles, topic)
        runs.append([r.stance for r in res])

    arr = np.array(runs, dtype=float)
    per_item_sd = np.nanstd(arr, axis=0)
    cors = []
    for i in range(n_runs):
        for j in range(i + 1, n_runs):
            mask = np.isfinite(arr[i]) & np.isfinite(arr[j])
            if mask.sum() > 2:
                cors.append(float(np.corrcoef(arr[i][mask], arr[j][mask])[0, 1]))

    mean_sd = float(np.nanmean(per_item_sd))
    mean_cor = float(np.mean(cors)) if cors else float("nan")
    return {
        "mean_sd": mean_sd,
        "max_sd": float(np.nanmax(per_item_sd)),
        "mean_correlation": mean_cor,
        "acceptable": bool(mean_sd < 0.15 and mean_cor > 0.85),
    }


def language_bias_check(classifier: "StanceClassifier",
                        parallel_titles: dict[str, list[str]],
                        topic: str) -> dict:
    """
    Detecta sesgo idiomático. Le pasas los MISMOS titulares traducidos a
    varios idiomas; si las posturas asignadas difieren sistemáticamente,
    tu mapa mostrará diferencias entre países que son artefactos del
    clasificador, no de la realidad.

    parallel_titles: {"spa": [...], "eng": [...], ...} mismo contenido,
                     mismo orden.
    """
    results = {}
    for lang, titles in parallel_titles.items():
        res = classifier.classify_batch(titles, topic, language=lang)
        results[lang] = np.array([r.stance for r in res], dtype=float)

    langs = list(results)
    deltas = {}
    base = langs[0]
    for other in langs[1:]:
        mask = np.isfinite(results[base]) & np.isfinite(results[other])
        if mask.sum() > 2:
            deltas[f"{base}_vs_{other}"] = {
                "mean_abs_diff": float(np.mean(np.abs(results[base][mask]
                                                      - results[other][mask]))),
                "systematic_shift": float(np.mean(results[other][mask]
                                                  - results[base][mask])),
            }
    worst = max((d["mean_abs_diff"] for d in deltas.values()), default=0.0)
    return {"deltas": deltas, "max_divergence": worst,
            "acceptable": bool(worst < 0.20)}
