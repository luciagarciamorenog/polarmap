# polarmap — índice de polarización en tiempo casi real

Pipeline para medir polarización del discurso público por país y tema, con
métricas defendibles y validación contra fuentes establecidas.

## Puesta en marcha

```bash
pip install numpy scipy pandas requests diptest
cp .env.example .env   # y pega tu key completa en .env

# Validar que las métricas funcionan (no necesita red ni API key)
cd analysis && python test_metrics.py

# Ejecutar el pipeline sobre datos reales
python pipeline.py --countries ES,US,FR,DE,IT --topics inmigracion --hours 168

# Validar contra V-Dem
python -c "from validate.ground_truth import validate_snapshot; \
           import json; print(json.dumps(validate_snapshot('data/snapshot.json'), indent=2))"
```

## Las tres decisiones que hacen que esto sea preciso

### 1. Mide postura, no sentimiento

El error dominante en este tipo de proyectos es usar análisis de sentimiento.
Un titular sobre un terremoto es muy negativo y cero polarizante. El pipeline
clasifica **posición en un eje temático** (-1 a +1), que es lo único que
permite ver si las opiniones se parten en bloques.

### 2. Distingue dispersión de polarización

Verificado en `analysis/test_metrics.py`:

| escenario | dispersión | polarización ideológica |
|---|---|---|
| consenso total | 0.05 | **0.00** |
| campana ancha | 0.35 | **0.06** |
| distribución uniforme | 0.59 | **0.12** |
| dos bloques enfrentados | 0.85 | **0.98** |

La distribución uniforme tiene dispersión altísima y polarización baja: hay
pluralidad de opiniones, no dos bandos. Un índice basado en varianza los
confundiría. Este usa **Esteban-Ray** (α=1.0), el índice canónico de la
literatura económica, calibrado con dos anclas: 0 = consenso compacto,
1 = dos bloques gaussianos en ±0.85.

Se confirma con el **test de Hartigan** (H0: unimodalidad). Si p > 0.05 no
hay dos bandos, por mucho tono negativo que haya.

### 3. Separa las tres dimensiones

- **Ideológica** — ¿hay dos bloques? (Esteban-Ray + dip test)
- **Afectiva** — ¿se odian? (hostilidad condicionada a la posición)
- **Compuesta** — media *geométrica* de ambas, no aritmética: dos bloques que
  no se odian no son el mismo fenómeno que dos bloques que se odian, y la
  media geométrica hace caer el compuesto cuando una dimensión es ~0.

La polarización afectiva no es "cuánta toxicidad hay". Es cuánta toxicidad
hay **específicamente en los extremos y dirigida al otro bloque**. Hostilidad
plana en todo el espectro es crispación general, y el índice la puntúa más
bajo (0.39 vs 0.67 en el test).

## Controles anti-falso-positivo

| control | qué evita |
|---|---|
| `effective_sample_size` (N de Kish) | Que 5 cuentas coordinadas disparen un país. 10.000 docs donde 5 cuentas generan el 90% → N efectivo = 5. |
| `source_concentration` (HHI) | Que midas la línea editorial de dos periódicos y lo llames "el discurso de España". HHI > 0.25 → dato marcado como no fiable. |
| `min_n` = 100 | Índices calculados sobre 12 artículos. |
| `off_topic` en el clasificador | Titulares que salen en la búsqueda pero no tratan el eje. |
| `language_bias_check` | Que países no anglófonos parezcan falsamente tranquilos por peor rendimiento del clasificador. |
| `measure_reliability` | Que el mismo titular reciba 0.2 y 0.8 en dos pasadas. Umbral: sd < 0.15, r > 0.85. |

**Regla de oro para el mapa: lo que no sea `trustworthy` se pinta en gris, no
en color.** Mostrar un número poco fiable con color fuerte es peor que no
mostrar nada — y es exactamente lo que destruiría tu credibilidad, que es tu
único activo real en un proyecto sobre polarización.

## Validación

`validate/ground_truth.py` correlaciona tu índice contra V-Dem (descarga
directa desde Our World in Data). Interpretación:

- r > 0.7 → mides lo mismo, más a menudo. Ese es el argumento de venta.
- r 0.3–0.5 → probablemente estás midiendo tono o volumen. Hay un problema.
- r ≈ 1.0 → tampoco es buena noticia: no aportas nada nuevo.

Lo valioso son las **divergencias explicables**: un país donde V-Dem va un año
por detrás y tú detectas un pico tras un evento reciente.

`temporal_sanity_check` comprueba que el índice reacciona a eventos conocidos
(elecciones, sentencias). Si no reacciona a nada, no mide. Si reacciona a
todo, mide volumen de noticias.

## Limitaciones que hay que publicar en la web

Ser explícita sobre esto no debilita el proyecto: lo blinda.

1. **GDELT cubre medios, no conversación social.** Mides discurso publicado,
   que es un proxy fuerte pero parcial de lo que piensa la gente. La capa de
   redes es una extensión posterior, no está en v0.1.
2. **Cobertura desigual.** Países con menos medios digitalizados tendrán N
   bajo y saldrán en gris.
3. **Sesgo idiomático residual.** Corregido parcialmente con
   `language_bias_check`, no eliminado.
4. **Los ejes son elecciones editoriales.** Definir el polo "-1" de
   inmigración como restrictivo es una decisión tuya, documentada en
   `AXES`. No cambies nunca la orientación de un eje: rompería toda la serie
   histórica.
5. **No mide causalidad.** Un pico no dice si la polarización causó el evento
   o al revés.

## Siguiente paso

Ejecuta `pipeline.py` sobre 5 países y 1 tema (unos 15–20 min y ~2–3€ de API).
Luego corre la validación contra V-Dem. Ese número de correlación decide si
sigues con la arquitectura actual o hay que revisar la capa de stance antes
de tocar el frontend.
