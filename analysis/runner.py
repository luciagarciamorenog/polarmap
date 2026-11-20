"""
Ejecuta peticiones a la API de Messages y registra lo que cuestan.

    run(bodies, stage, mode)   → un texto por petición (None si falló)
        mode="sync"   una a una, en el momento
        mode="batch"  todas en un lote de la Batch API (50 % más barato;
                      tarda de minutos a horas, sin nadie esperando)

Cada respuesta deja su `usage` en LEDGER: tokens de entrada, salida,
escritura y lectura de caché, por etapa y modelo. `LEDGER.summary()` los
convierte en dólares con la tabla PRICES.

Una key inválida o sin saldo (FatalAPIError) corta la corrida en ambos
modos; una petición suelta que falla vuelve como None, igual que antes.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

import requests

from analysis import discover
from analysis.discover import API_URL, FatalAPIError, TransientAPIError

log = logging.getLogger(__name__)

BATCH_URL = "https://api.anthropic.com/v1/messages/batches"
BATCH_POLL_SECONDS = 30
BATCH_TIMEOUT_SECONDS = 6 * 3600

# $ por millón de tokens (tabla de precios de la API, septiembre de 2026;
# confirmar en la página de precios antes de presupuestar).
PRICES = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
CACHE_WRITE = 1.25     # escritura de caché (TTL 5 min) = 1,25 × entrada
CACHE_READ = 0.10      # lectura de caché = 0,1 × entrada
BATCH_DISCOUNT = 0.50  # la Batch API factura a la mitad


@dataclass
class Ledger:
    records: list[dict] = field(default_factory=list)

    def record(self, stage: str, model: str, usage: dict | None, batch: bool):
        u = usage or {}
        self.records.append({
            "stage": stage, "model": model, "batch": batch,
            "input": int(u.get("input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
        })

    @staticmethod
    def cost(r: dict) -> float:
        # La API responde con el nombre fechado (claude-haiku-4-5-20251001):
        # se busca por prefijo para no perder el coste de esas llamadas.
        p = PRICES.get(r["model"]) or next(
            (v for k, v in PRICES.items() if r["model"].startswith(k)), None)
        if p is None:
            return float("nan")
        usd = (r["input"] * p["input"]
               + r["cache_write"] * p["input"] * CACHE_WRITE
               + r["cache_read"] * p["input"] * CACHE_READ
               + r["output"] * p["output"]) / 1e6
        return usd * (BATCH_DISCOUNT if r["batch"] else 1.0)

    def summary(self) -> dict:
        by = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0,
                                  "cache_write": 0, "cache_read": 0, "usd": 0.0})
        for r in self.records:
            s = by[f"{r['stage']} · {r['model']}"]
            s["calls"] += 1
            for k in ("input", "output", "cache_write", "cache_read"):
                s[k] += r[k]
            s["usd"] += self.cost(r)
        total = sum(s["usd"] for s in by.values())
        cached = sum(r["cache_read"] for r in self.records)
        prompt = sum(r["input"] + r["cache_write"] + r["cache_read"] for r in self.records)
        return {"by_stage": dict(by), "usd_total": total,
                "cache_hit_share": cached / prompt if prompt else 0.0}

    def reset(self):
        self.records.clear()


LEDGER = Ledger()


def _headers() -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY")
    return {"x-api-key": key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"}


def _text_of(message: dict) -> str:
    return "".join(b.get("text", "") for b in message.get("content", [])
                   if b.get("type", "text") == "text").strip()


def _run_sync(bodies: list[dict], stage: str) -> list[str | None]:
    out = []
    for body in bodies:
        try:
            message = discover._call_api_message(body, os.environ["ANTHROPIC_API_KEY"],
                                                 timeout=120)
        except TransientAPIError as e:
            log.error("  [%s] petición fallida: %s", stage, e)
            out.append(None)
            continue
        LEDGER.record(stage, body["model"], message.get("usage"), batch=False)
        out.append(_text_of(message))
    return out


def _run_batch(bodies: list[dict], stage: str) -> list[str | None]:
    headers = _headers()
    reqs = [{"custom_id": f"{stage}-{i}", "params": b} for i, b in enumerate(bodies)]
    r = requests.post(BATCH_URL, headers=headers, json={"requests": reqs}, timeout=120)
    if r.status_code >= 400:
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientAPIError(f"no se pudo crear el lote: HTTP {r.status_code}")
        raise FatalAPIError(f"HTTP {r.status_code}: {r.text[:300]}")
    batch = r.json()
    log.info("  [%s] lote %s con %d peticiones enviado", stage, batch["id"], len(reqs))

    start = time.monotonic()
    while batch.get("processing_status") != "ended":
        if time.monotonic() - start > BATCH_TIMEOUT_SECONDS:
            raise TransientAPIError(f"el lote {batch['id']} no terminó a tiempo")
        time.sleep(BATCH_POLL_SECONDS)
        try:
            batch = requests.get(f"{BATCH_URL}/{batch['id']}", headers=headers,
                                 timeout=60).json()
        except (requests.RequestException, ValueError):
            continue
        c = batch.get("request_counts", {})
        log.info("  [%s] lote: %s · %s hechas, %s en curso", stage,
                 batch.get("processing_status"), c.get("succeeded"), c.get("processing"))

    res = requests.get(batch["results_url"], headers=headers, timeout=300)
    by_id = {}
    for line in res.text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = item.get("result", {})
        if result.get("type") == "succeeded":
            message = result["message"]
            i = int(item["custom_id"].rsplit("-", 1)[1])
            LEDGER.record(stage, bodies[i]["model"], message.get("usage"), batch=True)
            by_id[item["custom_id"]] = _text_of(message)
        else:
            log.error("  [%s] %s: %s", stage, item.get("custom_id"), result.get("type"))
    return [by_id.get(f"{stage}-{i}") for i in range(len(bodies))]


def run(bodies: list[dict], stage: str, mode: str = "sync") -> list[str | None]:
    if not bodies:
        return []
    if mode == "batch":
        return _run_batch(bodies, stage)
    return _run_sync(bodies, stage)
