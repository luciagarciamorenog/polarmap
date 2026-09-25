"""
Tests de analysis/runner.py: cuentas del coste y flujo de la Batch API,
con HTTP simulado (sin red, sin API).

    cd analysis && python3 test_runner.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis import discover, runner  # noqa: E402

os.environ.setdefault("ANTHROPIC_API_KEY", "k")
checks = []


def check(desc, ok):
    checks.append((desc, bool(ok)))
    print(f"  [{'OK' if ok else 'FALLO'}] {desc}")


print("=== Coste por registro ===")
L = runner.Ledger()
L.record("postura", "claude-sonnet-4-6",
         {"input_tokens": 1_000_000, "output_tokens": 100_000,
          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, batch=False)
check(f"Sonnet 1M entrada + 0,1M salida = 4,50 $ (sale {L.cost(L.records[0]):.2f})",
      abs(L.cost(L.records[0]) - 4.50) < 1e-9)
L.record("postura", "claude-sonnet-4-6",
         {"input_tokens": 0, "output_tokens": 0,
          "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000},
         batch=False)
check(f"caché: escribir 1M = 3,75 $ y leer 1M = 0,30 $ (sale {L.cost(L.records[1]):.2f})",
      abs(L.cost(L.records[1]) - 4.05) < 1e-9)
L.record("asignacion", "claude-haiku-4-5",
         {"input_tokens": 1_000_000, "output_tokens": 0}, batch=True)
check(f"lote: Haiku 1M entrada a mitad de precio = 0,50 $ (sale {L.cost(L.records[2]):.2f})",
      abs(L.cost(L.records[2]) - 0.50) < 1e-9)
s = L.summary()
check(f"resumen: total {s['usd_total']:.2f} $ y lectura de caché "
      f"{s['cache_hit_share']:.0%} del prompt", abs(s["usd_total"] - 9.05) < 1e-9
      and abs(s["cache_hit_share"] - 0.25) < 1e-9)

print("\n=== Modo síncrono: registra el uso real de cada respuesta ===")
runner.LEDGER.reset()
orig = discover._call_api_message
discover._call_api_message = lambda body, key, timeout: {
    "content": [{"type": "text", "text": "hola"}],
    "usage": {"input_tokens": 100, "output_tokens": 20,
              "cache_read_input_tokens": 900}}
try:
    out = runner.run([{"model": "claude-sonnet-4-6"}] * 2, "postura", "sync")
finally:
    discover._call_api_message = orig
check("devuelve un texto por petición", out == ["hola", "hola"])
check("registra 2 llamadas con su caché",
      runner.LEDGER.summary()["by_stage"]["postura · claude-sonnet-4-6"]["cache_read"] == 1800)


print("\n=== Modo lote: enviar, esperar, leer resultados ===")


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload, text

    def json(self):
        return self._p


calls = {"get": 0}


def fake_post(url, headers, json, timeout):
    calls["submitted"] = [r["custom_id"] for r in json["requests"]]
    return Resp(payload={"id": "msgbatch_1", "processing_status": "in_progress"})


def fake_get(url, headers, timeout):
    calls["get"] += 1
    if url.endswith("/msgbatch_1"):
        status = "ended" if calls["get"] >= 2 else "in_progress"
        return Resp(payload={"id": "msgbatch_1", "processing_status": status,
                             "results_url": "https://x/results",
                             "request_counts": {"succeeded": 1, "processing": 1}})
    lines = [  # resultados en cualquier orden, uno fallido
        {"custom_id": "asignacion-1", "result": {"type": "succeeded", "message": {
            "model": "claude-haiku-4-5", "content": [{"type": "text", "text": "B"}],
            "usage": {"input_tokens": 50, "output_tokens": 5}}}},
        {"custom_id": "asignacion-0", "result": {"type": "succeeded", "message": {
            "model": "claude-haiku-4-5", "content": [{"type": "text", "text": "A"}],
            "usage": {"input_tokens": 50, "output_tokens": 5}}}},
        {"custom_id": "asignacion-2", "result": {"type": "errored"}},
    ]
    return Resp(text="\n".join(json.dumps(x) for x in lines))


runner.LEDGER.reset()
orig_post, orig_get, orig_poll = runner.requests.post, runner.requests.get, runner.BATCH_POLL_SECONDS
runner.requests.post, runner.requests.get, runner.BATCH_POLL_SECONDS = fake_post, fake_get, 0
try:
    out = runner.run([{"model": "claude-haiku-4-5"}] * 3, "asignacion", "batch")
finally:
    runner.requests.post, runner.requests.get, runner.BATCH_POLL_SECONDS = orig_post, orig_get, orig_poll
check("ids únicos por petición", calls["submitted"] == ["asignacion-0", "asignacion-1", "asignacion-2"])
check("reordena por custom_id y el fallido vuelve como None", out == ["A", "B", None])
rec = runner.LEDGER.records
check("uso registrado como lote (mitad de precio)", len(rec) == 2 and all(r["batch"] for r in rec))


def post_no_credit(url, headers, json, timeout):
    return Resp(status=400, text='{"error":{"message":"Your credit balance is too low"}}')


runner.requests.post = post_no_credit
try:
    runner.run([{"model": "m"}], "asignacion", "batch")
    check("sin saldo en lote corta la corrida", False)
except discover.FatalAPIError:
    check("sin saldo en lote corta la corrida", True)
finally:
    runner.requests.post = orig_post

fallos = [d for d, ok in checks if not ok]
print("\nRESULTADO:", "TODO PASA" if not fallos else f"FALLOS: {fallos}")
raise SystemExit(1 if fallos else 0)
