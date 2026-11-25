# Legacy

Earlier generations of PolarMap, kept for reference. They are not used by the
current pipeline (`pipeline_discover.py`) and may not run as they are.

| File | What it was |
|---|---|
| `pipeline.py`, `analysis_stance.py`, `ingest_gdelt.py` | First version (Aug 2026): hand-written debate axes and the GDELT DOC 2.0 API, abandoned because GDELT rate-limited it by IP. `analysis_stance.py` also holds early reliability and language-bias checks. |
| `pipeline_gkg.py`, `INSTRUCCIONES_GKG.md` | Second version: GDELT GKG files with fixed axes, before debates were discovered automatically. |
