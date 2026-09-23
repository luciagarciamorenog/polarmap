"""Valida el parseo del GKG sin necesidad de red."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.gkg_files import (
    slug_to_title, domain_to_country, matches_topic,
    parse_gkg_zip, gkg_timestamps, source_concentration,
    is_soft_section,
)

print("=== slug_to_title ===")
casos = [
    ("https://elpais.com/espana/2026-08-05/el-gobierno-aprueba-la-reforma-migratoria.html",
     "debe extraer titular"),
    ("https://www.eldiario.es/politica/vox-exige-cierre-fronteras-inmediato_1_9876543.html",
     "debe extraer titular con id al final"),
    ("https://abc.es/12345678", "sin slug -> vacio"),
    ("https://lemonde.fr/idees/article/2026/08/05/la-question-migratoire-divise-le-pays_123.html",
     "frances"),
    ("https://elmundo.es/", "raiz -> vacio"),
    ("https://x.es/a-b", "demasiado corto -> vacio"),
]
for url, desc in casos:
    t = slug_to_title(url)
    print(f"  {desc:<38} -> '{t}'")

print()
print("=== domain_to_country ===")
for d in ["elpais.com", "www.eldiario.es", "nytimes.com", "spiegel.de",
          "algo.fr", "desconocido.xyz", "bbc.co.uk", ""]:
    print(f"  {d:<22} -> {domain_to_country(d)}")

print()
print("=== matches_topic ===")
temas = [
    ("TAX_FNCACT;IMMIGRATION;WB_2024_ANTI_CORRUPTION", "inmigracion", True),
    ("ENV_CLIMATECHANGE;NATURAL_DISASTER", "clima", True),
    ("ENV_CLIMATECHANGE", "inmigracion", False),
    ("", "inmigracion", False),
    ("SOC_GENDEREQUALITY;WOMEN", "genero", True),
]
for themes, topic, esperado in temas:
    got = matches_topic(themes, topic)
    ok = "OK" if got == esperado else "FALLA"
    print(f"  [{ok}] '{themes[:40]:<40}' / {topic:<12} -> {got}")

print()
print("=== parse_gkg_zip con fichero sintetico ===")


def fila(date, domain, url, themes, tone):
    """Construye una fila GKG de 27 columnas con los campos que importan."""
    r = [""] * 27
    r[1] = date
    r[3] = domain
    r[4] = url
    r[7] = themes
    r[15] = f"{tone},1.5,2.0,3.5,0,0,0"
    return "\t".join(r)


filas = [
    # utiles: tema + pais + slug
    fila("20260805120000", "elpais.com",
         "https://elpais.com/espana/2026-08-05/el-gobierno-endurece-los-requisitos-de-entrada.html",
         "IMMIGRATION;POLITICS", -3.2),
    fila("20260805120000", "eldiario.es",
         "https://eldiario.es/politica/las-ong-denuncian-devoluciones-en-caliente_1_555.html",
         "IMMIGRATION;HUMAN_RIGHTS", -5.1),
    fila("20260805120000", "abc.es",
         "https://abc.es/espana/llegan-mas-pateras-a-canarias-durante-agosto.html",
         "REFUGEES;BORDER", -2.0),
    # descartado: tema no coincide
    fila("20260805120000", "elmundo.es",
         "https://elmundo.es/deportes/el-madrid-gana-la-liga-otra-vez.html",
         "SPORTS", 4.0),
    # descartado: pais no pedido
    fila("20260805120000", "nytimes.com",
         "https://nytimes.com/2026/08/05/us/border-policy-shift-explained.html",
         "IMMIGRATION", -1.0),
    # descartado: sin slug utilizable
    fila("20260805120000", "larazon.es", "https://larazon.es/98765", "IMMIGRATION", -1.0),
    # fila corrupta
    "solo\tdos",
]

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("20260805120000.gkg.csv", "\n".join(filas))
raw = buf.getvalue()

arts, stats = parse_gkg_zip(raw, "inmigracion", {"ES"})
print(f"  estadisticas: {stats}")
print(f"  articulos recuperados: {len(arts)}")
for a in arts:
    print(f"    [{a.domain:<14}] tono={a.tone:>5}  '{a.title[:55]}'")

print()
print("  ASERCIONES:")
checks = [
    ("recupera 3 articulos espanoles de inmigracion", len(arts) == 3),
    ("descarta deportes", all("madrid" not in a.title for a in arts)),
    ("descarta NYT (pais no pedido)", all(a.country == "ES" for a in arts)),
    ("cuenta el descarte por falta de titular", stats["no_title"] == 1),
    ("no revienta con fila corrupta", stats["rows"] == 7),
    ("extrae el tono", arts[0].tone is not None),
]
for desc, ok in checks:
    print(f"    [{'PASA' if ok else 'FALLA'}] {desc}")

print()
print("=== zip corrupto (no debe reventar) ===")
arts2, stats2 = parse_gkg_zip(b"esto no es un zip", "inmigracion", {"ES"})
print(f"  devuelve {len(arts2)} articulos, sin excepcion -> OK")

print()
print("=== timestamps generados ===")
st = gkg_timestamps(hours_back=6, every_n=4)
print(f"  6h, 1 fichero/hora -> {len(st)} ficheros")
print(f"  primeros: {st[:3]}")
st2 = gkg_timestamps(hours_back=24, every_n=1)
print(f"  24h, todos          -> {len(st2)} ficheros")
mins = {s[10:12] for s in st2}
print(f"  minutos usados: {sorted(mins)} (deben ser 00/15/30/45)")

print()
print("=== concentracion de fuentes ===")
print(f"  {source_concentration(arts)}")

print()
print("=== limpieza de slugs (restos de CMS) ===")
slugs = [
    ("https://www.ideal.es/sociedad/gobierno-prorrogara-ayudas-gasoleo-agricola-final-ano-20260922-ntrc.html",
     "gobierno prorrogara ayudas gasoleo agricola final ano"),
    ("https://www.larazon.es/x/puede-vecino-negarse-pagar-derrama-esto-dice-ley_202609226ab2b42279081b6c7a870df8.html",
     "puede vecino negarse pagar derrama esto dice ley"),
    ("https://www.europapress.es/nacional/noticia-muere-hombre-anos-resulto-herido-incendio-20260923.html",
     "muere hombre anos resulto herido incendio"),
    ("https://www.elconfidencial.com/espana/menores-detenidos-robo-violencia-lorca_1tna-1tps/",
     "menores detenidos robo violencia lorca"),
    ("https://www.abc.es/espana/ofensiva-vox-ley-nietos-exige-pruebas-descendencia-nt.html",
     "ofensiva vox ley nietos exige pruebas descendencia"),
    # palabras normales que se parecen a códigos no se tocan
    ("https://elpais.com/espana/2026-09-23/el-pp-y-vox-pactan-los-presupuestos-de-2027.html",
     "el pp vox pactan los presupuestos de"),
]
for url, esperado in slugs:
    got = slug_to_title(url)
    checks.append((f"slug limpio: {esperado[:40]}", got == esperado))
    print(f"  [{'PASA' if got == esperado else 'FALLA'}] '{got}'")

print()
print("=== secciones blandas ===")
blandas = [
    ("https://www.marca.es/futbol/real-madrid/gana-la-liga.html", True),
    ("https://www.abc.es/gente/cindy-crawford-hijo-nt.html", True),
    ("https://www.elpais.com/deportes/2026-09-23/algo-muy-largo-aqui.html", True),
    ("https://www.elpais.com/espana/2026-09-23/debate-sobre-vivienda.html", False),
    ("https://www.eldiario.es/opinion/la-justicia-lenta-y-desigual.html", False),
    # la palabra en el slug no cuenta, solo en la ruta
    ("https://www.elpais.com/economia/el-motor-de-la-economia-se-frena.html", False),
]
for url, esperado in blandas:
    got = is_soft_section(url)
    checks.append((f"blanda={esperado}: {url[12:50]}", got == esperado))
    print(f"  [{'PASA' if got == esperado else 'FALLA'}] {got!s:<5} {url}")

fallos = [d for d, ok in checks if not ok]
print()
print("RESULTADO:", "TODO PASA" if not fallos else f"FALLOS: {fallos}")
