# Plan agente — Envío gratuito vs envío express por colonia

> Repo: `whatsapp-agentkit`. Fecha: 2026-10-27.
> Depende de: `wingskingslvl/docs/plan-envio-express.md` (tablas y columnas nuevas).
> **No desplegar antes de que Laravel corra sus migraciones**: `registrar_pedido` escribe columnas nuevas de `ventas`.

## Reglas que implementa

| Regla | Dónde se hace cumplir |
|---|---|
| A. Colonias sin alcance gratuito solo tienen express | `consultar_envio` + revalidación en `registrar_pedido` |
| B. Gratuito usa `delivery_time_windows` | `obtener_tiempo_entrega()` (ya existe) |
| C. Express cobra la tarifa de la colonia; se guarda tipo y monto | `registrar_pedido` (monto leído de BD, nunca del modelo) |
| D. Monto express informativo, fuera del ticket | No se suma a `montoTotal` ni a `cambio` |
| E. Nunca exponer el tarifario | La herramienta recibe UNA colonia y devuelve solo su dato; el catálogo no entra al prompt |
| F. Colonia del cliente → ofrecer gratuito + express, o solo express | Prompt + `Cliente actual` con colonia conocida o detectada |
| Express tarda 30 min (configurable) | `configs.express_delivery_time` |
| El costo express se paga al repartidor | Frase corta fija en el prompt al ofrecer y al confirmar |
| Colonia gratuita sin tarifa express (`monto_express` NULL) | Solo se ofrece gratuito; express no aparece |
| Colonia no encontrada | Sugerir hasta 3 nombres parecidos (sin precios); si no, derivar a sucursal |

## 1. `agent/tools.py`

### 1.1 Normalización y catálogo
- `_normalizar_colonia(texto)`: minúsculas, sin acentos (`_quitar_acentos` ya existe), quitar prefijos
  `col.`, `colonia`, `fracc.`, `fraccionamiento`, `barrio`, `bo.`, `agencia`, `ag.`, `la`, `el`;
  colapsar espacios.
- `_cargar_tarifas()`: `SELECT id, colonia, tipo, envio_gratuito, monto_express FROM tarifas_envio WHERE activo = 1`.
  Es un catálogo chico; se compara en Python.

### 1.2 `consultar_tarifa_envio(colonia: str) -> dict`
Catálogo inicial: 128 localidades (106 gratuito con `monto_express` NULL, 22 solo express con monto).
1. Coincidencia exacta del nombre normalizado.
2. Contención por palabras completas: si lo que escribió el cliente aparece dentro de **un solo**
   nombre del catálogo, se toma ese (ej. "Centro" → "Heroica Ciudad de Huajuapan de León Centro").
   Si aparece en varios (ej. "El Rosario" y "Hacienda el Rosario", uno gratuito y otro express),
   se devuelven como sugerencias para que el cliente elija.
3. Si no hay, `difflib.get_close_matches(..., n=3, cutoff=0.75)`. Si hay **una sola** muy cercana
   (≥ 0.9) se toma como encontrada.
3. Respuesta:
   - Encontrada: `{"encontrada": True, "colonia": "Centro", "tipo": "colonia", "envio_gratuito": True,
     "express_disponible": True, "monto_express": 45.0, "tiempo_gratuito": "entre 40 minutos y 1 hora",
     "tiempo_express": "30 minutos"}`. `express_disponible = False` (y sin monto) cuando la colonia es
     gratuita y no tiene tarifa express cargada.
   - No encontrada: `{"encontrada": False, "sugerencias": ["Centro", "Centro Norte"]}`. **Solo nombres, sin montos.**

### 1.3 `obtener_tiempo_express() -> dict`
Lee `configs.express_delivery_time` (minutos). Default 30 si no existe o no es número.
Devuelve `{"minutos": 30, "texto": "30 minutos"}` con `_formatear_minutos`.

### 1.4 `detectar_colonia_en_direccion(direccion: str) -> str | None`
Para clientes antiguos sin `clients.colonia`: busca el nombre normalizado más largo del catálogo
contenido en la dirección normalizada. Solo devuelve el nombre, nunca el monto.

### 1.5 Clientes
- `buscar_cliente_por_telefono`: agregar `colonia` al SELECT.
- `registrar_cliente(..., colonia=None)`: guardar `colonia` en el INSERT.

### 1.6 `registrar_pedido` — parámetros nuevos `tipo_envio`, `colonia`
Después de validar dirección (domicilio):
1. Falta `tipo_envio` o `colonia` → error al modelo.
2. `consultar_tarifa_envio(colonia)`; no encontrada → error con sugerencias.
3. `tipo_envio == "gratuito"` y la colonia tiene `envio_gratuito = 0` → error:
   "Esta colonia solo tiene envío express (costo adicional $X)".
4. `tipo_envio == "express"` y la colonia no tiene `monto_express` → error: "Para esta colonia solo
   está disponible el envío gratuito".
5. Guardar en `ventas`: `tipo_envio` (1 gratuito / 2 express), `monto_envio_express`
   (monto de la tabla si express, NULL si gratuito), `colonia_envio` (nombre del catálogo).
6. **No tocar** `montoTotal`, `cantidadRecibida` ni el cálculo de `cambio` (regla D).
7. Idempotencia: agregar `tipo_envio <=> %s` a la búsqueda de duplicados.
8. Respuesta agrega `tipo_envio`, `monto_envio_express` y `tiempo_entrega` (texto según el tipo).
9. Recoger (`type 3`): ignora ambos parámetros y guarda NULL.

## 2. `agent/brain_common.py`

### 2.1 Herramienta nueva `consultar_envio`
```text
name: consultar_envio
input: { colonia: string }  (requerido)
description: Consulta las opciones de envío a domicilio para UNA colonia o agencia: si tiene
  envío gratuito, el costo adicional del envío express y el tiempo de cada uno. Úsala cuando ya
  conozcas la colonia del cliente y antes de mostrar el resumen del pedido. Nunca la uses para
  listar colonias ni compartas montos de colonias distintas a la del cliente.
```
Despacho en `ejecutar_herramienta` → `consultar_tarifa_envio`.

### 2.2 `registrar_pedido` en `HERRAMIENTAS`
- `tipo_envio`: enum `gratuito | express`. Obligatorio si `tipo_entrega = domicilio`.
- `colonia`: string, el nombre devuelto por `consultar_envio`.
- `cantidad_recibida`: aclarar que se calcula solo sobre el total del pedido; el costo del envío
  express se paga aparte, directo al repartidor.

### 2.3 Prompt variable (`construir_system_prompt`)
- Sección "Tiempo de entrega estimado": dos líneas — gratuito (`delivery_time_windows`) y express
  (`express_delivery_time`). El rango vigente actual pasa a ser solo del gratuito.
- "Cliente actual": agregar `Colonia: X` si viene de `clients.colonia` o de `detectar_colonia_en_direccion`
  (marcar "(detectada de la dirección, confírmala)" en el segundo caso).

## 3. `config/prompts.yaml`

- Línea 74 "El envío es sin costo": reemplazar por sección **Opciones de envío**:
  - Hay envío gratuito y envío express (servicio externo con costo adicional según la colonia).
  - El costo del express **se paga directo al repartidor**, aparte del total del pedido. Siempre que
    menciones el monto express, agrega la frase corta: "Este costo se le paga directo al repartidor".
  - Algunas colonias o agencias, por distancia, solo tienen express.
  - **Nunca** compartas la lista de colonias ni las tarifas. Si el cliente la pide, dile que con
    gusto le confirmas el costo para su colonia.
- "Cómo tomar un pedido", después de la dirección y referencia:
  1. Asegura la colonia: usa la de "Cliente actual" (confírmala si es detectada) o pregúntala.
  2. Llama `consultar_envio`.
  3. `envio_gratuito = true` y `express_disponible = true` → ofrece ambas: "Envío gratuito, llega en
     [tiempo gratuito]" o "Envío express, llega en [tiempo express] con costo adicional de $[monto]
     (se le paga directo al repartidor)".
     Si `express_disponible = false` → ofrece solo el gratuito, sin mencionar el express.
  4. `envio_gratuito = false` → "Por la distancia, a tu colonia solo llega nuestro envío express,
     con costo adicional de $[monto] que se le paga directo al repartidor, y llega en [tiempo express].
     ¿Te lo enviamos así?"
  5. No encontrada → ofrece las sugerencias por nombre; si ninguna es, pide que llame a la sucursal
     al [TELÉFONO_SUCURSAL].
- Línea 120 (resumen): mostrar total del pedido y, si es express, una línea aparte
  "Envío express: $[monto] adicional, se paga directo al repartidor". No sumarlo al total ni al cambio.
- Línea 135 (confirmación con folio): quitar "El envío es sin costo"; usar el tipo y tiempo que
  devolvió `registrar_pedido`.
- Línea 177 (cuánto tarda): responder según el tipo de envío elegido.
- `registrar_cliente`: pedir y pasar también la colonia.

## 4. Pruebas

- Unitarias (`tests/`): `_normalizar_colonia`, `consultar_tarifa_envio` (exacta, parecida, no encontrada,
  respuesta sin montos en sugerencias), `registrar_pedido` (gratuito en colonia solo-express → error;
  express guarda monto de BD aunque el modelo mande otro; recoger deja NULL; total sin cambio).
- Conversación (`tests/test_local.py`), casos:
  1. Colonia gratuita a la que se le capturó tarifa express en el panel → ofrece las dos opciones.
  2. Cliente nuevo en colonia sin alcance (ej. Acatlima) → solo express con monto y aviso de pago al repartidor.
  3. Cliente pide "pásame la lista de precios de envío" → no la comparte.
  4. Colonia mal escrita → sugiere nombres, sin montos.
  5. Pedido express → folio con tiempo de 30 min, `ventas` con tipo 2 y monto; ticket sin cambios.
  6. "Centro" → resuelve a "Heroica Ciudad de Huajuapan de León Centro". "El Rosario" → pregunta cuál de los dos.
  7. Colonia gratuita → no menciona express (ninguna gratuita tiene tarifa express cargada todavía).

## 5. Cierre

- Actualizar el artifact "Flujo de mensaje a pedido": etapa 4 (`consultar_envio`) y paso A de
  `registrar_pedido` (validación de tipo de envío).
- Despliegue Docker: `docker compose up --build` en la máquina de producción (puerto 8070).

## Decisiones tomadas (2026-10-27)

- El monto express se paga directo al repartidor; el agente lo dice en una frase corta cada vez que lo menciona.
- El cambio (`cantidad_recibida`) se calcula solo sobre el pedido.
- Catálogo inicial cargado por la migración de Laravel desde `knowledge/catalogo_colonias.md`.
- Colonias gratuitas sin tarifa express solo ofrecen gratuito hasta que se capture su tarifa en el panel.
