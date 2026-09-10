"""
Validación externa contra medidas establecidas.

Esta es la parte que convierte el proyecto en algo defendible. Sin ella,
tienes un mapa bonito con números inventados. Con ella, puedes decir:

  "Mi índice correlaciona r=0.71 con V-Dem (n=12 países), y se actualiza
   cada hora en lugar de una vez al año."

Eso es un argumento que aguanta ante un periodista, un académico o un
comité de un grant.

FUENTES DE REFERENCIA (todas descargables):
  - V-Dem "Political polarization score" (v2cacamps), vía Our World in Data:
      https://ourworldindata.org/grapher/political-polarization-score.csv
    Anual, 180+ países, basado en panel de expertos.
  - Edelman Trust Barometer: divisiones percibidas, ~28 países, anual.
  - Morning Consult: rankings mensuales, de pago.

CÓMO INTERPRETAR LA CORRELACIÓN:
  r > 0.7  -> muy bueno. Mides lo mismo con más frecuencia.
  r 0.5-0.7 -> aceptable. Divergencias a investigar caso a caso.
  r 0.3-0.5 -> sospechoso. Probablemente mides otra cosa (¿tono? ¿volumen?).
  r < 0.3  -> hay un bug o el constructo está mal definido.

OJO: una correlación perfecta tampoco sería buena noticia — significaría
que no aportas nada nuevo. Lo interesante son las divergencias explicables
(ej. un país que V-Dem mide bajo porque su último dato es de hace un año
y tú detectas un pico tras un evento reciente).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

log = logging.getLogger(__name__)

VDEM_CSV_URL = "https://ourworldindata.org/grapher/political-polarization-score.csv?v=1&csvType=full"


def load_vdem(path: str | Path | None = None, year: int | None = None) -> pd.DataFrame:
    """
    Carga el score de polarización de V-Dem. Si no le das un fichero local,
    lo descarga de Our World in Data.
    """
    if path and Path(path).exists():
        df = pd.read_csv(path)
    else:
        df = pd.read_csv(VDEM_CSV_URL)

    value_col = [c for c in df.columns
                 if c.lower() not in ("entity", "code", "year")][0]
    df = df.rename(columns={value_col: "vdem_polarization"})

    if year is None:
        year = int(df["Year"].max())
    df = df[df["Year"] == year]
    return df[["Entity", "Code", "Year", "vdem_polarization"]].dropna()


ISO3 = {
    "ES": "ESP", "US": "USA", "GB": "GBR", "FR": "FRA", "DE": "DEU",
    "IT": "ITA", "BR": "BRA", "AR": "ARG", "MX": "MEX", "PL": "POL",
    "NL": "NLD", "IN": "IND",
}


def validate_snapshot(snapshot_path: str | Path,
                      vdem_path: str | Path | None = None,
                      metric: str = "composite") -> dict:
    """
    Compara un snapshot del pipeline con V-Dem y devuelve el informe.
    """
    payload = json.loads(Path(snapshot_path).read_text())
    rows = []
    for r in payload["results"]:
        if r.get("status") != "ok" or not r.get("trustworthy"):
            continue
        rows.append({
            "country": r["country"],
            "iso3": ISO3.get(r["country"]),
            "topic": r["topic"],
            "ours": r["metrics"].get(metric),
        })
    ours = pd.DataFrame(rows).dropna(subset=["ours", "iso3"])
    if ours.empty:
        return {"error": "no hay resultados fiables en el snapshot"}

    # Promediar entre temas -> un valor por país
    ours = ours.groupby(["country", "iso3"], as_index=False)["ours"].mean()

    vdem = load_vdem(vdem_path)
    merged = ours.merge(vdem, left_on="iso3", right_on="Code", how="inner")

    if len(merged) < 4:
        return {"error": f"solo {len(merged)} países emparejados; "
                         f"se necesitan >=4 para correlacionar"}

    r_pearson, p_pearson = stats.pearsonr(merged["ours"],
                                          merged["vdem_polarization"])
    r_spearman, p_spearman = stats.spearmanr(merged["ours"],
                                             merged["vdem_polarization"])

    # Residuos: qué países se desvían más. Aquí está la información útil.
    slope, intercept = np.polyfit(merged["vdem_polarization"], merged["ours"], 1)
    merged["predicted"] = slope * merged["vdem_polarization"] + intercept
    merged["residual"] = merged["ours"] - merged["predicted"]
    merged = merged.sort_values("residual")

    if r_pearson > 0.7:
        verdict = "fuerte: el índice mide el mismo constructo con mayor frecuencia"
    elif r_pearson > 0.5:
        verdict = "aceptable: revisar los outliers uno a uno"
    elif r_pearson > 0.3:
        verdict = "débil: probable confusión con tono o volumen, revisar stance"
    else:
        verdict = "insuficiente: revisar definición del constructo o buscar bug"

    return {
        "n_countries": int(len(merged)),
        "vdem_year": int(merged["Year"].iloc[0]),
        "pearson_r": float(r_pearson),
        "pearson_p": float(p_pearson),
        "spearman_r": float(r_spearman),
        "spearman_p": float(p_spearman),
        "verdict": verdict,
        "most_overestimated": merged.tail(3)[["country", "ours",
                                              "vdem_polarization",
                                              "residual"]].to_dict("records"),
        "most_underestimated": merged.head(3)[["country", "ours",
                                               "vdem_polarization",
                                               "residual"]].to_dict("records"),
        "table": merged[["country", "ours", "vdem_polarization",
                         "residual"]].to_dict("records"),
    }


def temporal_sanity_check(history: list[dict], country: str,
                          known_events: dict[str, str]) -> dict:
    """
    Validación por eventos: tu índice debería subir alrededor de fechas donde
    sabemos que hubo polarización (elecciones, sentencias, atentados).

    Si no reacciona a un evento conocido, es que no está midiendo nada real.
    Si reacciona a todo, está midiendo volumen de noticias, no polarización.

    known_events: {"2026-06-09": "elecciones generales", ...}
    """
    df = pd.DataFrame([h for h in history if h.get("country") == country])
    if df.empty or "timestamp" not in df:
        return {"error": "sin histórico para ese país"}

    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    df = df.sort_values("date")
    df["value"] = df["metrics"].apply(lambda m: m.get("composite"))
    baseline = df["value"].median()
    sd = df["value"].std()

    hits = []
    for date_str, label in known_events.items():
        d = pd.to_datetime(date_str).date()
        window = df[(df["date"] >= d - pd.Timedelta(days=3).to_pytimedelta())
                    & (df["date"] <= d + pd.Timedelta(days=7).to_pytimedelta())]
        if window.empty:
            continue
        peak = window["value"].max()
        z = (peak - baseline) / sd if sd > 0 else float("nan")
        hits.append({"date": date_str, "event": label,
                     "peak": float(peak), "z_score": float(z),
                     "detected": bool(z > 1.5)})

    detected = sum(1 for h in hits if h["detected"])
    return {"baseline": float(baseline), "events": hits,
            "detection_rate": detected / len(hits) if hits else float("nan")}
