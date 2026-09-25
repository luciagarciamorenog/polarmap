"""Tests offline de ingest/leads.py (sin red)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest.leads import parse_head, pick_lead, strip_site_suffix, _clean  # noqa: E402

checks = []

page = """<html><head><title>Otro | Medio</title>
<meta property="og:title" content="Unai Sordo pide &quot;sacar a gorrazos&quot; a los fondos buitre">
<meta name="description" content='Resumen corto'>
<meta property="og:description" content="Pedro S&amp;aacute;nchez respondi&oacute; en Harvard">
<meta name="tdm-reservation" content="1">
</head><body>...</body></html>"""
m = parse_head(page)
title, src, desc = pick_lead(m)
checks.append(("og:title gana a <title>", src == "og:title"))
checks.append(("comillas desescapadas", title == 'Unai Sordo pide "sacar a gorrazos" a los fondos buitre'))
checks.append(("og:description gana a description", desc.startswith("Pedro")))
checks.append(("doble escape resuelto", desc == "Pedro Sánchez respondió en Harvard"))
checks.append(("reserva TDM leída del meta", m.get("tdm-reservation") == "1"))
checks.append(("sin og: cae a <title>", pick_lead(parse_head("<title>Hola mundo</title>"))[:2] == ("Hola mundo", "<title>")))

for raw, dom, want in [
    ("Maricarmen, el sistema fallido | Canarias7", "www.canarias7.es", "Maricarmen, el sistema fallido"),
    ("No solo Ceuta | La Gaceta de Salamanca", "www.lagacetadesalamanca.es", "No solo Ceuta"),
    ("Derbi Oviedo - Gijón", "www.elcomercio.es", "Derbi Oviedo - Gijón"),
    ("Sin sufijo", "elpais.com", "Sin sufijo"),
]:
    got = strip_site_suffix(raw, dom)
    checks.append((f"sufijo: {raw}", got == want))
checks.append(("_clean compacta espacios", _clean("  a \n b ") == "a b"))

for d, ok in checks:
    print(f"  [{'PASA' if ok else 'FALLA'}] {d}")
fallos = [d for d, ok in checks if not ok]
print("RESULTADO:", "TODO PASA" if not fallos else f"FALLOS: {fallos}")
raise SystemExit(1 if fallos else 0)
