# Libro de códigos PolarMap · v3

Unidad de análisis: **titular + entradilla** de una noticia (lo que lee el
modelo). Se abre la noticia solo si con eso no se entiende.

PolarMap mide tres dimensiones por separado y no las combina todavía:

| Dimensión | Qué mide | Campos |
|---|---|---|
| 1. Postura / ideológica | Cómo se reparten los medios entre los bandos de cada debate | `topic`, `stance` |
| 2. Afectiva | Hostilidad del lenguaje y enfrentamiento entre grupos | `hostility`, `us_them` |
| 3. Narrativa / framing | Qué hechos se eligen y cuán conflictiva se presenta la historia | `conflict` (+ el framing ya entra en `stance`) |

Cambios frente a v1: `stance` incluye framing; `hostility` queda solo para el
lenguaje; `conflict` es nuevo; `us_them` tiene una regla explícita.

Cambios v3 (22 nov 2026):
- `why`: antes de puntuar, el modelo escribe en ≤ 15 palabras qué hecho o
  ángulo elige el medio y a qué polo favorece. Razonar primero reduce los
  ceros por prudencia.
- `stance = 0` queda solo para datos sin ángulo o noticias que recogen a los
  dos bandos por igual. Un titular sobrio que elige un hecho que conviene a
  un bando no es 0 (nuevo ejemplo del polo B en vivienda).
- `bloc`: eje fijo Gobierno ↔ oposición, el mismo en todos los debates y
  días. `stance` sigue sin mirar a qué partido perjudica; eso va aquí.
- Varias pasadas (`--stance-passes 3`): cada titular se lee 3 veces en otro
  orden y se promedia; `stance_sd` mide cuánto duda el modelo.

---

## `topic` — ¿de qué debate del día trata?

Uno de los debates del día o `none`. Un titular va a un debate si trata de su
asunto, aunque sea informativo. Si encaja en varios, el más central.

Si es `none`, `none_type`:
- `no_stance` — no admite postura: suceso, servicio, dato, empresa, cultura.
- `political` — política o políticas públicas de otro asunto.
- `nonpolitical_controversy` — controversia con bandos fuera de la política
  (deporte, cultura, consumo, sociedad).
- `other`.

## `stance` — postura en el eje del debate (con framing)

Escala: `-1`, `-0.5`, `0`, `0.5`, `1` (polo A a polo B) o `na` si el titular
es del debate pero no se puede situar en su eje.

Cuenta la postura **explícita** (opinión, cita elegida como titular) **y la
implícita por framing**: qué hecho se elige contar, desde qué ángulo y con qué
palabras.

| Valor | Regla | Ejemplo (vivienda: A intervenir ↔ B mercado) |
|---|---|---|
| ±1 | Defensa explícita de un polo, o framing muy cargado (términos del bando, consigna como titular) | «"Maricarmen somos todas": Tenerife alza la voz contra los desahucios» → −1 |
| ±0.5 | El ángulo o la selección del hecho favorecen un polo sin afirmarlo | «Valladolid contabiliza un desahucio cada dos días» → −0.5 · «Los propietarios tardan 20 meses en recuperar una vivienda okupada» → +0.5 |
| 0 | Solo dato o trámite sin ángulo, o equilibrado (recoge a ambos bandos). El tono sobrio no basta | «La compraventa de viviendas cae un 5,1 % en julio» → 0 |

Señales de framing por polo (se definen con cada debate). Ejemplos del
25-sep-2026:
- Migración, polo A (control): «frustra», «avalancha», «invasión», llegadas
  como cifra récord, delitos. Polo B (acogida): rescates, niños, derechos,
  víctimas de racismo.
- Justicia, polo A (jueces legítimos): revelaciones de casos, quejas del
  Gobierno presentadas como presión. Polo B (lawfare): los argumentos del
  investigado o del Gobierno presentados como razonables.

## `hostility` — hostilidad del lenguaje (dimensión afectiva)

Solo las **palabras** del titular y la entradilla, incluidas las citas que el
medio elige destacar. No la gravedad del hecho (eso es `conflict`).

- `2` — insulto, burla, deshumanización, descalificación moral: «a gorrazos»,
  «invasión», «cloacas», «clasista», «fracaso moral», «cansino».
- `1` — léxico combativo o cargado: «choque», «maniobra», «arremete»,
  «fondo buitre», «acoso», «chantaje», «alza la voz contra».
- `0` — lenguaje neutro, aunque el hecho sea grave.

## `conflict` — conflictividad de la historia (dimensión narrativa)

Cuán conflictiva, negativa o dañina es la historia **tal como se cuenta**,
independientemente de las palabras.

- `2` — violencia, víctimas graves, crisis abierta, confrontación política
  directa (dimisiones exigidas, acusaciones penales cruzadas).
- `1` — disputa, crítica, queja, daño limitado, problema o riesgo.
- `0` — sin conflicto: dato, servicio, logro, agenda.

## `us_them` — «nosotros contra ellos»

Sí cuando el titular presenta **dos partes identificables enfrentadas**, una
frente a otra, sea explícito o por el ángulo. No basta con que haya una
víctima o un culpable sin el otro bando.

- `political` — partidos, Gobierno, oposición, instituciones enfrentadas.
- `social_groups` — colectivos sociales (inquilinos frente a fondos,
  vecinos frente a migrantes...).
- `nonpolitical_rivals` — rivales fuera de la política: clubes, empresas
  frente a clientes, asociaciones.
- `no`.


## `bloc` — Gobierno ↔ oposición (v3)

Eje fijo, igual en todos los debates, días y países: a qué bloque político
nacional deja mejor o peor la noticia.

- `-1` favorece claramente al Gobierno o daña a la oposición.
- `+1` favorece claramente a la oposición o daña al Gobierno.
- `±0.5` de forma leve; `0` equilibrada.
- `null` si no implica a actores políticos nacionales.

Por qué existe: en España la fractura de la prensa es sobre todo de bloques.
`stance` mide el asunto (vivienda, migración) y, a propósito, no mira a qué
partido perjudica; sin `bloc` esa fractura no se veía. Al ser fijo, permite
comparar días y países.

## `why` — motivo antes de puntuar (v3)

Una frase de ≤ 15 palabras: qué hecho, cita o ángulo elige el medio y a qué
polo favorece. Se guarda con cada titular para poder auditar la puntuación.
