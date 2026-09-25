"""
Piloto: titular real + entradilla para los titulares de una muestra gold.

Uso:
    python3 validate/fetch_sample_leads.py data/gold/sample_ES_20260925.json

Escribe data/gold/leads_<muestra>.json (ignorado por git: son extractos de
prensa) y resume cuántos se han podido leer y por qué fallan los demás.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest.leads import LeadFetcher  # noqa: E402


def main(sample_path: str):
    sample = json.loads(Path(sample_path).read_text())
    items = sample["items"]
    fetcher = LeadFetcher()

    # Un hilo por medio: en paralelo entre medios, en serie dentro de cada uno.
    by_domain = defaultdict(list)
    for it in items:
        by_domain[urlparse(it["url"]).netloc].append(it)

    def run(group):
        return [(it["id"], fetcher.fetch(it["url"])) for it in group]

    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for chunk in pool.map(run, by_domain.values()):
            for item_id, lead in chunk:
                results[item_id] = lead

    out = Path(sample_path).with_name(
        Path(sample_path).name.replace("sample_", "leads_"))
    out.write_text(json.dumps(
        {i: results[i].to_dict() for i in sorted(results)},
        ensure_ascii=False, indent=1))

    status = Counter(l.status for l in results.values())
    ok = [l for l in results.values() if l.status == "ok"]
    with_desc = [l for l in ok if len(l.description) >= 40]
    print(f"{len(items)} artículos → {out}")
    print("estado:", dict(status.most_common()))
    print(f"con titular real: {len(ok)} ({len(ok) / len(items):.0%}) · "
          f"con entradilla útil (≥40 car.): {len(with_desc)} "
          f"({len(with_desc) / len(items):.0%})")
    tdm = Counter(l.tdm_reserved for l in results.values())
    print("reserva TDM declarada:", {str(k): v for k, v in tdm.items()})
    fails = Counter(urlparse(l.url).netloc for l in results.values()
                    if l.status != "ok" or len(l.description) < 40)
    if fails:
        print("medios con fallo o sin entradilla:", fails.most_common(15))


if __name__ == "__main__":
    main(sys.argv[1])
