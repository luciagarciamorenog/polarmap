# Libro de códigos PolarMap · v5

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

Cambios v4 (24 nov 2026):
- `speaker` y `speaker_org`: quién habla en cada noticia (siempre). `medio`
  si es el propio medio; si hay una cita, quién la dice y su partido u
  organización. Separa a quien enciende de quien lo amplifica.
- `target`: contra qué va la hostilidad (`ideas`, `personas`, `grupo`).
  Criticar una medida no es lo mismo que ridiculizar a una persona.
- Polos de cada debate: siempre políticas o soluciones, nunca juicios sobre
  un político o su actuación (quién ganó un debate, quién mintió). La campaña
  y la gestión del Gobierno van a un eje fijo «continuidad o cambio de
  Gobierno». El 20-nov el eje del cara a cara («Feijóo ofrece un cambio
  creíble» frente a «Feijóo recurrió a la bronca») dejaba a casi todos los
  medios del mismo lado.
- Webs de partidos e instituciones (pp.es, psoe.es…) ya no cuentan como
  prensa.

---

## `topic` — ¿de qué debate del día trata?

Uno de los debates del día o `none`. Un titular va a un debate si trata de su
asunto, aunque sea informativo. Si encaja en varios, el más central.

Regla de la campaña (v5, 26 nov 2026, decisión de Lucía):
- Toda noticia de las elecciones o de la campaña (29-N, candidatos, mítines,
  encuestas, escaños, mesas, sueldos de diputados, pactos) va a «Continuidad o
  cambio de Gobierno», aunque solo informe y no tome postura.
- Excepción: si la noticia de campaña trata de un asunto con debate propio
  (vivienda, migración…), va a ese debate. «Colau propone expropiar 400.000
  pisos turísticos» → Vivienda; «Vox rescata las vallas contra la
  inmigración» → Política migratoria.

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

## `speaker`, `speaker_org` — quién habla (v4)

Siempre, haya hostilidad o no: para calcular la nota de cada actor hace
falta saber cuántas veces habla en total, no solo cuántas ataca.

- `medio` — nadie habla: es el medio quien cuenta o valora (el enfoque,
  los adjetivos o la consigna del titular).
- Nombre público de la persona citada, nombre y primer apellido
  («Santiago Abascal», «Pedro Sánchez»), o de la organización si habla como
  tal («PP», «Sindicato de Inquilinas»). Si hay varias citas, la más hostil.
- `speaker_org`: siglas del partido u organización de esa persona si se
  conocen con seguridad; `null` si no o si `speaker` es `medio`.

Ejemplo: «Abascal, sobre el debate: "Celebro cuando a un tirano se le pone
frente al espejo"» → `speaker` Santiago Abascal, `speaker_org` Vox. El medio
que lo lleva al titular lo amplifica, pero no lo dice.

## `target` — contra qué va la hostilidad (v4)

Solo si `hostility` > 0; si no, `null`.

- `ideas` — critica una medida, una postura o una gestión.
- `personas` — descalifica, ridiculiza o insulta a personas concretas.
- `grupo` — descalifica a un colectivo entero (migrantes, jueces, okupas,
  los votantes de un partido).

## Terreno común y diferencia de fondo (por debate)

Paso final de cada corrida (`analysis/common_ground.py`), solo en debates con
datos suficientes y al menos 5 titulares claramente inclinados (|postura| ≥ 0,5)
a cada lado. El modelo recibe hasta 20 titulares con entradilla por lado,
repartidos entre medios, y responde:

- **Comparten**: un objetivo, un hecho o un valor que aparece en titulares de
  ambos lados. Nada genérico ("ambos quieren lo mejor"). Si no lo hay: null.
- **Se diferencian**: el núcleo del desacuerdo y su tipo:
  - *objetivos*: quieren cosas distintas o priorizan valores distintos;
  - *medios*: mismo objetivo, distinto camino;
  - *hechos*: no coinciden en qué pasa o por qué;
  - *confianza*: discuten si un actor es de fiar más que la política.

Comprobación automática antes de publicar: los titulares citados existen, hay
al menos uno de cada lado y de al menos dos medios. Se pide dos veces; solo se
publica si coinciden en si hay terreno común y en el tipo de diferencia. En la
web se presenta como síntesis del modelo, no como medición validada.
