# Cómo cambiar PolarMap

Todo vive en `~/dev/polarmap`. GitHub es la fuente de verdad: cada vez que
se sube un cambio, Netlify publica la demo sola. El portfolio no se toca:
enlaza a la demo y a GitHub, que siempre están al día.

```
cambio en ~/dev/polarmap  →  ./publicar.sh  →  GitHub  →  Netlify (demo)
                                                   ↑
                         ./diario.sh (cada corrida publica sus datos)
```

## Pasos

1. **Pide el cambio.** A Claude, en el proyecto «polarizacion» con la carpeta
   `~/dev/polarmap` conectada (o en Cursor). Una sola herramienta toca el
   código a la vez.
2. **Míralo antes de publicar.** `python3 web/build_data.py` y después
   `open web/index.html`: se abre la web en tu navegador con los datos que
   tienes en el Mac. Nada de esto es público todavía.
3. **Publícalo.**
   - Si el cambio es de la web, el README o los textos:
     `./publicar.sh "qué has cambiado"`
   - Si cambia lo que calcula el pipeline (prompts, métricas, debates):
     lanza una corrida con `./diario.sh`, que publica sola. Antes, guarda el
     cambio de código con `./publicar.sh "qué has cambiado"`.
4. **Comprueba** https://luciagarciamorenog.github.io/polarmap/ en 1-2 minutos (en incógnito).

## Qué hace `publicar.sh`

Pasa los tests, regenera la web, comprueba que no se sube ninguna clave ni el
`.env`, guarda el cambio con tu mensaje y lo sube a GitHub. Si algo falla, no
publica nada y dice por qué.

## Reglas

- Las claves solo en `.env` (no se sube nunca) o en variables de entorno.
- Las cifras públicas (README, web) salen siempre de los datos de una corrida,
  no se escriben a mano.
- Lo que genera el modelo sin validar se marca como tal en la web.
