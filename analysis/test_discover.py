"""
Tests del módulo de descubrimiento automático.

Qué se prueba aquí (sin red, sin API):
  1. Filtrado de ruido: etiquetas genéricas se descartan
  2. Extracción de etiquetas: parseo del campo GKG
  3. Agrupación por co-ocurrencia: fusión transitiva
  4. Parseo de respuestas: robusto ante JSON malformado

Ejecutar:
    cd ~/Documents/polarmap
    python3 -m pytest analysis/test_discover.py -v

O directamente:
    cd ~/Documents/polarmap
    python3 analysis/test_discover.py
"""

import sys
from pathlib import Path
from collections import Counter

# Para que funcione tanto con pytest como ejecutado directamente
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.discover import (
    is_noise_theme, extract_themes,
    build_topic_groups, _parse_classify_response,
    TopicGroup, StanceResult,
)

# ─────────────────────────────────────────────────────────────
# 1. FILTRADO DE RUIDO
# ─────────────────────────────────────────────────────────────

def test_noise_themes():
    """Las etiquetas genéricas se filtran, las específicas no."""
    # Estas SÍ son ruido
    assert is_noise_theme("GENERAL_GOVERNMENT") is True
    assert is_noise_theme("POLITICS") is True
    assert is_noise_theme("LEADER") is True
    assert is_noise_theme("TAX_FNCACT_PRESIDENT") is True  # prefijo
    assert is_noise_theme("WB_2024_ANTI_CORRUPTION") is True  # prefijo
    assert is_noise_theme("EPU_POLICY_MONETARY") is True  # prefijo
    # Nuevas que causaron problemas en el primer censo real:
    assert is_noise_theme("AFFECT") is True
    assert is_noise_theme("TOURISM") is True
    assert is_noise_theme("GEN_HOLIDAY") is True       # prefijo GEN_
    assert is_noise_theme("SOC_EXPRESSREGRET") is True  # prefijo SOC_
    assert is_noise_theme("TAX_WORLDINSECTS") is True   # prefijo TAX_WORLD
    assert is_noise_theme("TAX_WORLDFISH") is True
    assert is_noise_theme("ARMEDCONFLICT") is True      # demasiado amplio
    assert is_noise_theme("CORRUPTION") is True         # demasiado amplio

    # Estas NO son ruido (temas reales con debate)
    assert is_noise_theme("IMMIGRATION") is False
    assert is_noise_theme("ENV_CLIMATECHANGE") is False
    assert is_noise_theme("TAX_POLICY") is False
    assert is_noise_theme("HOUSING") is False
    assert is_noise_theme("REFUGEES") is False
    assert is_noise_theme("ABORTION") is False
    assert is_noise_theme("LABOR") is False
    assert is_noise_theme("PROTEST") is False

    print("[OK] filtrado de ruido")


# ─────────────────────────────────────────────────────────────
# 2. EXTRACCIÓN DE ETIQUETAS
# ─────────────────────────────────────────────────────────────

def test_extract_themes():
    """Parseo del campo V2Themes del GKG."""
    # Formato con offsets
    themes = extract_themes("IMMIGRATION,123;REFUGEES,456;LEADER,789")
    assert "IMMIGRATION" in themes
    assert "REFUGEES" in themes
    assert "LEADER" not in themes  # es ruido, se filtra
    assert len(themes) == 2

    # Formato plano (sin offsets)
    themes = extract_themes("TAX_POLICY;HOUSING;POLITICS")
    assert "TAX_POLICY" in themes
    assert "HOUSING" in themes
    assert "POLITICS" not in themes  # ruido

    # Campo vacío
    assert extract_themes("") == []
    assert extract_themes(None) == []

    # Sin duplicados
    themes = extract_themes("IMMIGRATION;IMMIGRATION;IMMIGRATION")
    assert len(themes) == 1

    print("[OK] extracción de etiquetas")


# ─────────────────────────────────────────────────────────────
# 3. AGRUPACIÓN POR CO-OCURRENCIA
# ─────────────────────────────────────────────────────────────

def test_build_groups_basic():
    """Etiquetas que van juntas se fusionan en un grupo."""
    # La fusión ahora exige que AMBAS direcciones superen el 50%.
    # Ejemplo: IMMIGRATION (200) y REFUGEES (150) juntas 110 veces.
    #   → 110/200 = 55%  (dirección IMMIGRATION)
    #   → 110/150 = 73%  (dirección REFUGEES)
    #   → mínimo = 55% > 50% → SÍ se fusionan
    counts = Counter({
        "IMMIGRATION": 200,
        "REFUGEES": 150,
        "TAX_POLICY": 180,
        "TAX_REFORM": 100,
        "HOUSING": 120,
        "TINY_TOPIC": 5,  # demasiado pequeño, se descarta
    })

    cooccur = Counter({
        # Inmigración y refugiados: alta co-ocurrencia en ambas direcciones
        ("IMMIGRATION", "REFUGEES"): 110,  # 110/200=55%, 110/150=73% → fusionar
        # Impuestos: alta en ambas
        ("TAX_POLICY", "TAX_REFORM"): 70,  # 70/180=39%, 70/100=70% → NO (39%<50%)
        # Housing solo coincide poco
        ("HOUSING", "TAX_POLICY"): 10,     # muy bajo → no fusionar
    })

    groups = build_topic_groups(counts, cooccur, min_articles=20)

    # Inmigración y refugiados se fusionan
    immigration_group = None
    for g in groups:
        if "IMMIGRATION" in g.tags:
            immigration_group = g
            break
    assert immigration_group is not None
    assert "REFUGEES" in immigration_group.tags

    # TAX_POLICY y TAX_REFORM NO se fusionan (39% < 50% en una dirección)
    tax_group = None
    for g in groups:
        if "TAX_POLICY" in g.tags:
            tax_group = g
            break
    assert tax_group is not None
    assert "TAX_REFORM" not in tax_group.tags  # NO fusionadas

    # TINY_TOPIC no aparece (menos de 20 artículos)
    all_tags = set()
    for g in groups:
        all_tags.update(g.tags)
    assert "TINY_TOPIC" not in all_tags

    # Ordenados de mayor a menor
    assert groups[0].count >= groups[1].count >= groups[2].count

    print("[OK] agrupación por co-ocurrencia")


def test_build_groups_no_merge():
    """Si no hay co-ocurrencia suficiente, cada tema es su propio grupo."""
    counts = Counter({
        "IMMIGRATION": 100,
        "TAX_POLICY": 80,
        "HOUSING": 60,
    })
    # Poca co-ocurrencia
    cooccur = Counter({
        ("HOUSING", "IMMIGRATION"): 5,  # 5/60 = 8%
        ("HOUSING", "TAX_POLICY"): 3,   # 3/60 = 5%
    })

    groups = build_topic_groups(counts, cooccur, min_articles=20)
    assert len(groups) == 3
    for g in groups:
        assert len(g.tags) == 1  # cada uno solo

    print("[OK] sin fusión cuando co-ocurrencia es baja")


def test_build_groups_empty():
    """Con datos vacíos o insuficientes, devuelve lista vacía."""
    assert build_topic_groups(Counter(), Counter()) == []
    assert build_topic_groups(Counter({"A": 5}), Counter(),
                              min_articles=20) == []

    print("[OK] grupos vacíos con datos insuficientes")


# ─────────────────────────────────────────────────────────────
# 4. PARSEO DE RESPUESTAS DE CLASIFICACIÓN
# ─────────────────────────────────────────────────────────────

def test_parse_response_clean():
    """JSON limpio se parsea correctamente."""
    text = '[{"id":1,"stance":-0.6,"hostility":0.2,"confidence":0.8,"off_topic":false},' \
           '{"id":2,"stance":0.4,"hostility":0.1,"confidence":0.9,"off_topic":false}]'

    results = _parse_classify_response(text, ["titulo 1", "titulo 2"])
    assert len(results) == 2
    assert abs(results[0].stance - (-0.6)) < 0.01
    assert abs(results[1].stance - 0.4) < 0.01
    assert results[0].off_topic is False

    print("[OK] parseo de JSON limpio")


def test_parse_response_markdown():
    """JSON envuelto en markdown se parsea igualmente."""
    text = """Aquí tienes los resultados:

```json
[{"id":1,"stance":-0.3,"hostility":0.5,"confidence":0.7,"off_topic":false}]
```

Espero que te sirva."""

    results = _parse_classify_response(text, ["titulo 1"])
    assert len(results) == 1
    assert abs(results[0].stance - (-0.3)) < 0.01

    print("[OK] parseo con envoltorio markdown")


def test_parse_response_garbage():
    """Respuesta basura devuelve todo off-topic (fail safe)."""
    text = "No entendí la pregunta, lo siento."
    results = _parse_classify_response(text, ["titulo 1", "titulo 2"])
    assert len(results) == 2
    assert all(r.off_topic is True for r in results)

    print("[OK] fail safe con respuesta basura")


def test_parse_response_clamping():
    """Valores fuera de rango se limitan."""
    text = '[{"id":1,"stance":-2.5,"hostility":1.8,"confidence":-0.3,"off_topic":false}]'
    results = _parse_classify_response(text, ["titulo 1"])
    assert results[0].stance == -1.0  # clamped
    assert results[0].hostility == 1.0  # clamped
    assert results[0].confidence == 0.0  # clamped

    print("[OK] clamping de valores fuera de rango")


# ─────────────────────────────────────────────────────────────
# Ejecutar todos los tests
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_noise_themes()
    test_extract_themes()
    test_build_groups_basic()
    test_build_groups_no_merge()
    test_build_groups_empty()
    test_parse_response_clean()
    test_parse_response_markdown()
    test_parse_response_garbage()
    test_parse_response_clamping()
    print()
    print("=== TODOS LOS TESTS PASAN ===")
