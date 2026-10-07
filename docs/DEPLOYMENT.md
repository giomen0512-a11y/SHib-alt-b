# Despliegue

Objetivo final: **Codex Cloud → GitHub → Docker → Ubuntu VPS**.

Codex Cloud es una herramienta de desarrollo. El contenedor desplegado será
autosuficiente y no dependerá de Codex, ChatGPT ni OpenAI para ejecutar producción.
En Fase 0 no hay servicio permanente, conexiones externas ni trading.

## Comprobación actual

En la raíz del checkout, con Docker Engine y Compose disponibles:

```sh
docker compose build
docker compose run --rm engine
docker build --target test -t shib-alt-b:test .
docker run --rm --network none shib-alt-b:test
```

El target predeterminado `runtime` instala el proyecto sin dependencias de
desarrollo; `test` instala herramientas y ejecuta la suite básica. El comando
de producción valida la configuración, emite JSON en STOPPED y termina con éxito.
Compose no reinicia procesos ni abre puertos y bloquea red durante la ejecución.
La construcción sí necesita descargar la imagen Python y paquetes desde PyPI.

Si la red de construcción usa una CA corporativa/proxy, proporciona un bundle
de confianza autorizado mediante el secreto temporal `build_ca` de BuildKit:

```sh
docker build --secret id=build_ca,src=/etc/ssl/certs/ca-certificates.crt -t shib-alt-b:runtime .
docker build --secret id=build_ca,src=/etc/ssl/certs/ca-certificates.crt --target test -t shib-alt-b:test .
docker run --rm --network none shib-alt-b:runtime
docker run --rm --network none shib-alt-b:test
```

La ruta debe apuntar al bundle autorizado del host. El montaje solo existe
durante la instalación; no se copia a las capas ni se desactiva TLS. Sin este
secreto se utiliza la confianza normal de la imagen base. En entornos con HOME
de solo lectura, usa `BUILDX_CONFIG=/tmp/shib-alt-b-buildx` al ejecutar builds.

## Pasos posteriores

Cuando las fases correspondientes definan el servicio operativo, se documentarán
su arranque, persistencia, observabilidad, recuperación, límites de recursos y
gestión de secretos en VPS. No se agregan Redis, PostgreSQL ni nginx en Fase 0.

Las herramientas directas de desarrollo y construcción tienen versiones fijadas
en `pyproject.toml`. Las dependencias transitivas y el tag `python:3.12-slim` pueden
actualizarse; no se promete una imagen idéntica byte por byte. Una política de
releases reproducibles con hashes/digests se definirá antes del despliegue operativo.
