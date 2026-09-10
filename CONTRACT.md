# Contrato de datos v1 — mapa polarmap

Congelado para el frontend. Cualquier cambio de forma o de umbral
sube `v`. El mapa no parsea campos que no estén aquí.

Ventana: **24 h UTC**. Ingestión: GKG **Main + Translation**.
Muestreo v1: `every_n=4` (un fichero por hora). Los umbrales de
`n` de abajo están calibrados a ese muestreo.

---

## 1. Países v1 (10)

| ISO | País | Idioma | Por qué está |
|-----|------|--------|----------------|
| US | Estados Unidos | eng | Volumen máximo en GKG Main; estresa el tope de clasificación |
| GB | Reino Unido | eng | Alto volumen inglés, sistema mediático distinto al US |
| ES | España | spa | Ancla hispana europea; ya hay corridas reales |
| MX | México | spa | Mayor volumen hispano en LatAm (.mx + .com) |
| AR | Argentina | spa | Alto volumen .ar; segundo polo hispano |
| CL | Chile | spa | Volumen medio-alto, TLD `.cl` limpio (evitamos `.co`) |
| FR | Francia | fra | Alto volumen en Translation |
| DE | Alemania | deu | Alto volumen en Translation |
| IT | Italia | ita | Alto volumen en Translation |
| BR | Brasil | por | Alto volumen .br; tercer idioma, sin ser ruido tipo IN |

Fuera de v1 a propósito: IN (volumen enorme, inglés sucio), CO
(TLD `.co` colisiona con dominios globales), AU (inglés redundante).

El frontend pinta estos 10 siempre. Si falta un código en el JSON,
se trata como gris (`trust: false`, `color: null`).

---

## 2. Schema `snapshot.json`

Producción: `json.dumps(..., separators=(",", ":"), ensure_ascii=False)`.
Sin `indent`. Flotantes a **3 decimales**. `n_eff` a **entero**.
Nada de `methodology_notes`, nombres de país ni tags GDELT: eso
vive en el cliente según `v`.

```json
{
  "v": 1,
  "ts": "2026-09-10T12:00:00Z",
  "w": 24,
  "countries": {
    "ES": {
      "trust": true,
      "color": 0.218,
      "dom": "mix",
      "n": 536,
      "ne": 498,
      "topics": [
        {
          "l": "Seguridad pública y orden",
          "a": [
            "Reforzar cuerpos de seguridad y endurecer la respuesta policial",
            "Cuestionar excesos institucionales y defender garantías civiles"
          ],
          "n": 176,
          "ne": 164,
          "ideo": 0.261,
          "aff": 0.308,
          "comp": 0.283,
          "trust": true
        }
      ]
    }
  }
}
```

### Campos raíz

| clave | tipo | significado |
|-------|------|-------------|
| `v` | int | Versión del contrato. Ahora `1`. |
| `ts` | string | Fin de ventana, ISO-8601 UTC con `Z`. |
| `w` | int | Horas de la ventana. v1 = `24`. |
| `countries` | object | Clave = ISO-2. Siempre los 10, aunque `trust` sea false. |

### País

| clave | tipo | significado |
|-------|------|-------------|
| `trust` | bool | Si el país se pinta de color. Ver §3. |
| `color` | float\|null | `comp` agregado. `null` si `trust` es false. El mapa **no** usa un número poco fiable. |
| `dom` | `"ideo"`\|`"aff"`\|`"mix"`\|null | Qué dimensión pesa más en el agregado. |
| `n` | int | Suma de `n` de temas **trust** (0 si ninguno). |
| `ne` | int | N efectivo de Kish sobre esas mismas unidades (aprox. suma; ver motor). |
| `topics` | array | Temas analizados, **fiables primero**, luego el resto. Máximo 5. |

### Tema

| clave | tipo | significado |
|-------|------|-------------|
| `l` | string | Etiqueta corta para la ficha (≤ 48 chars). |
| `a` | [string, string] | Polos del eje: `[polo −1, polo +1]`. Orientación congelada por `l` normalizado. |
| `n` | int | Titulares **on-topic** clasificados (no filas GKG crudas). |
| `ne` | int | N efectivo de Kish (`floor`). |
| `ideo` | float\|null | Esteban-Ray α=1.0, anclado. `null` si el tema no es trust. |
| `aff` | float\|null | Polarización afectiva. `null` si no trust. |
| `comp` | float\|null | `sqrt(ideo * aff)`. `null` si no trust. |
| `trust` | bool | Si el tema entra en `color` y se muestra numérico. |

Agregado país (`color`, `dom`, `n`, `ne`): **solo temas con `trust: true`**,
media de `ideo`/`aff`/`comp` ponderada por `n`. `dom`: `ideo` si
ideo > aff×1.2; `aff` si aff > ideo×1.2; si no `mix`.

Si cero temas trust: `trust: false`, `color: null`, `dom: null`,
`n: 0`, `ne: 0`. Los temas no fiables **sí** pueden listarse en la
ficha (sin números, o con `n`/`ne` para explicar el gris).

Fuera del snapshot público: dip test, HHI, dominios, URLs, ejes
internos, methodology notes. Quedan en logs / `data/internal/`.

---

## 3. Criterio `trustworthy`

Las cuentas son **después** de clasificar y tirar `off_topic`.
No son “noticias GKG crudas”.

### Tema (`topics[].trust`)

True solo si se cumplen **todas**:

1. `n >= 80` en 24 h (muestreo `every_n=4`)
2. `ne >= 50` (Kish; evita 5 medios inflando el índice)
3. `n_domains >= 8`
4. `hhi < 0.25`
5. `top_share < 0.40` (ningún dominio > 40 % del tema)

80 no es 100: `metrics.py` usa `min_n=100` para Esteban-Ray en
abstracto, pero con `every_n=4` 80 on-topic ≈ un día real de
medios densos (ES, FR, MX). 30 (default actual de discover) es
demasiado bajo: el snapshot ES pintaba temas con n=33.

### País (pinta color, no gris)

True solo si se cumplen **todas**:

1. Al menos **1** tema con `trust: true`
2. Suma de `n` de esos temas **`>= 100`**
3. Suma de `ne` de esos temas **`>= 70`**

**100 titulares on-topic en 24 h** es el umbral de país que
congela v1. Un solo tema de 80 no pinta el país: hace falta masa
mínima nacional, no un debate fino. Un país con tres temas de 40
(suma 120) sigue **gris**: no se mezclan ejes no fiables para
inventar color.

El frontend: `trust === true` → color por `color` ∈ [0,1];
si no → gris. Nunca interpolar `color` cuando `trust` es false.
