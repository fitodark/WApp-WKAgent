# agent/tools.py — Herramientas del agente Wings Kings
# Generado por AgentKit

import os
import json
import yaml
import secrets
import logging
import aiomysql
from datetime import datetime

logger = logging.getLogger("agentkit")


def _db_config() -> dict:
    """Lee la configuración de MariaDB desde variables de entorno."""
    return {
        "host": os.getenv("CLIENTS_DB_HOST", "localhost"),
        "port": int(os.getenv("CLIENTS_DB_PORT", 3307)),
        "user": os.getenv("CLIENTS_DB_USER", "root"),
        "password": os.getenv("CLIENTS_DB_PASSWORD", ""),
        "db": os.getenv("CLIENTS_DB_NAME", "wings_kings_prod_120526_1"),
    }


async def buscar_cliente_por_telefono(telefono: str) -> dict | None:
    """
    Busca un cliente en la base de datos de Wings Kings por número de teléfono.

    Args:
        telefono: Número de teléfono del cliente (puede incluir código de país)

    Returns:
        Dict con datos del cliente si existe y está activo, None si no se encuentra
    """
    # Normalizar: quedarse solo con los últimos 10 dígitos para comparar
    digitos = "".join(filter(str.isdigit, telefono))
    sufijo = digitos[-10:] if len(digitos) >= 10 else digitos

    try:
        conn = await aiomysql.connect(**_db_config())
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, name, address, reference, phone, active "
                "FROM clients "
                "WHERE phone LIKE %s AND active = 1 "
                "LIMIT 1",
                (f"%{sufijo}",),
            )
            row = await cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error al consultar cliente en MariaDB: {e}")
        return None


async def cliente_registrado(telefono: str) -> bool:
    """Retorna True si el número de teléfono pertenece a un cliente registrado y activo."""
    cliente = await buscar_cliente_por_telefono(telefono)
    return cliente is not None


_TIPO_ETIQUETA = {
    1: "🍺 Bebidas",
    2: "🍗 Alitas y Boneless",
    3: "🍔 Cocina General",
}

_DETALLE_VACIO = {".", "..", "...", "...."}


async def obtener_menu_desde_db() -> str:
    """
    Obtiene el menú activo (active=1, type in 1,2,3) y lo formatea para el system prompt.

    Marca cada producto según delivery_available (tinyint(1), default 0):
      - delivery_available = 1 → disponible para domicilio Y local
      - delivery_available = 0 → solo consumo en el local
        (incluye TODAS las bebidas alcohólicas, que nunca se envían a domicilio)
    """
    try:
        conn = await aiomysql.connect(**_db_config())
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, name, detail, price, type, delivery_available FROM products "
                "WHERE active = 1 AND type IN (1, 2, 3) "
                "ORDER BY type, id"
            )
            rows = await cur.fetchall()
        conn.close()

        por_tipo: dict[int, list] = {1: [], 2: [], 3: []}
        for row in rows:
            por_tipo[row["type"]].append(row)

        secciones = []
        for tipo, productos in por_tipo.items():
            if not productos:
                continue
            lineas = [f"\n### {_TIPO_ETIQUETA[tipo]}"]
            for p in productos:
                nombre = p["name"]
                detalle = p["detail"] if p["detail"] and p["detail"].strip() not in _DETALLE_VACIO else ""
                precio = float(p["price"])
                disponible_domicilio = p.get("delivery_available") == 1
                etiqueta = "🚗 domicilio+local" if disponible_domicilio else "🏠 SOLO EN LOCAL"
                precio_fmt = f"${precio:.2f}".rstrip("0").rstrip(".")
                lineas.append(
                    f"- [#{p['id']}] {nombre}{f' ({detalle})' if detalle else ''}: {precio_fmt} [{etiqueta}]"
                )
            secciones.append("\n".join(lineas))

        encabezado = (
            "Leyenda de disponibilidad:\n"
            "- [🚗 domicilio+local] → se puede pedir a domicilio y en el local\n"
            "- [🏠 SOLO EN LOCAL] → NO se envía a domicilio, solo consumo en establecimiento\n"
            "El código [#N] al inicio de cada producto es su identificador interno (producto_id).\n"
            "Úsalo SOLO para registrar el pedido con la herramienta registrar_pedido.\n"
            "NUNCA muestres ni menciones estos códigos [#N] al cliente.\n"
        )

        return encabezado + "\n".join(secciones) if secciones else "Menú no disponible en este momento."

    except Exception as e:
        logger.error(f"Error al obtener menú desde DB: {e}")
        return "Menú no disponible en este momento."


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atención y si el negocio está abierto ahora."""
    ahora = datetime.now()
    dia = ahora.weekday()   # 0=Lunes … 6=Domingo
    hora = ahora.hour
    minuto = ahora.minute

    # "12:00 AM" = medianoche = hora 0:00 del día siguiente.
    # El negocio cierra EN medianoche, así que el último minuto válido es 23:59.
    # hora == 0 ya es el nuevo día → cerrado.

    # Lunes a Sábado (0-5): 4:00 PM – 12:00 AM (medianoche)
    if dia <= 5:
        esta_abierto = hora >= 16  # 16:00 hasta 23:59 inclusive
        horario = "Lunes a Sábado 4:00 PM – 12:00 AM"
    # Domingo (6): 1:00 PM – 12:00 AM (medianoche)
    else:
        esta_abierto = hora >= 13  # 13:00 hasta 23:59 inclusive
        horario = "Domingos 1:00 PM – 12:00 AM"

    return {
        "horario": horario,
        "esta_abierto": esta_abierto,
        "hora_actual": ahora.strftime("%I:%M %p"),
        "dia_actual": ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"][dia],
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos de /knowledge.
    Retorna el contenido más relevante encontrado.
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


def calcular_total_pedido(items: list[dict]) -> float:
    """
    Calcula el total de un pedido.

    Args:
        items: Lista de dicts con 'precio' y 'cantidad'
               Ej: [{"nombre": "Alitas 10 pz", "precio": 125.0, "cantidad": 2}]

    Returns:
        Total del pedido sin envío
    """
    return sum(item.get("precio", 0) * item.get("cantidad", 1) for item in items)


def formatear_pedido(items: list[dict], es_domicilio: bool = False) -> str:
    """
    Genera un resumen formateado del pedido para confirmar con el cliente.
    El envío a domicilio es sin costo adicional.
    """
    if not items:
        return "No hay productos en el pedido."

    lineas = ["🧾 *Resumen de tu pedido:*"]
    for item in items:
        nombre = item.get("nombre", "Producto")
        precio = item.get("precio", 0)
        cantidad = item.get("cantidad", 1)
        subtotal = precio * cantidad
        lineas.append(f"• {cantidad}x {nombre} — ${subtotal:.0f}")

    total = calcular_total_pedido(items)
    lineas.append(f"\n✅ *Total: ${total:.0f}*")

    if es_domicilio:
        lineas.append("🚗 Envío sin costo")

    return "\n".join(lineas)


def obtener_promociones_vigentes() -> str:
    """Retorna las promociones vigentes según el día de la semana."""
    dia = datetime.now().weekday()  # 0=Lunes, 6=Domingo
    nombres_dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    dia_nombre = nombres_dias[dia]

    promos = []

    if dia == 1:  # Martes
        promos.append("🍔 *Promo Martes:* Hamburguesas 2x$150")
    if dia == 2:  # Miércoles
        promos.append("🍗 *Promo Miércoles Alitas:*")
        promos.append("  • 8 boneless $76 | 10 boneless $95")
        promos.append("  • 15 boneless $142.50 | 20 boneless $190")
        promos.append("  • 30 boneless $285 | 8 pz alitas $76")
        promos.append("  • 25 pz $225 | 50 pz $475")
        promos.append("  • Litros de cócteles 50% desc: $45")
    if dia == 4:  # Viernes
        promos.append("🍺 *Promo Viernes:* Cervezas 3x2 — $180")

    # Promociones permanentes
    promos.append("🍺 *Siempre:* Tarro chico barril 2x1 — $30")
    promos.append("🍺 *Siempre:* Promo Megas (3 Megas) — $220")
    promos.append("🍺 *Siempre:* Cubeta de Medias — $210")

    if not promos:
        return f"Hoy ({dia_nombre}) no hay promociones especiales, pero siempre tenemos precios accesibles 🍗"

    return f"🔥 *Promociones de hoy ({dia_nombre}):*\n" + "\n".join(promos)


# ════════════════════════════════════════════════════════════
# Catálogos de configuración (tabla configs: valores constantes)
# ════════════════════════════════════════════════════════════

async def obtener_catalogo_config(clave: str) -> list[str]:
    """
    Devuelve la lista de valores de la tabla configs para una clave dada,
    en orden. Varias filas pueden compartir la misma 'key' (ej: 'pieces',
    'flavors', 'local_phone_number'); el orden define el índice 0-based que
    usa el POS como 'key' dentro del JSON de desglose.
    """
    try:
        conn = await aiomysql.connect(**_db_config())
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT value FROM configs WHERE `key` = %s ORDER BY id", (clave,)
            )
            valores = [r["value"] for r in await cur.fetchall()]
        conn.close()
        return valores
    except Exception as e:
        logger.error(f"Error al leer configs[{clave}]: {e}")
        return []


async def obtener_telefono_sucursal() -> str:
    """Teléfono(s) del establecimiento desde configs (puede haber varios)."""
    valores = await obtener_catalogo_config("local_phone_number")
    return " / ".join(v for v in valores if v) if valores else "nuestra sucursal"


def _indice_en_catalogo(valor: str, catalogo: list[str]) -> int | None:
    """Índice 0-based de un valor dentro de su catálogo (match sin distinguir mayúsculas)."""
    objetivo = (valor or "").strip().lower()
    for i, v in enumerate(catalogo):
        if v.strip().lower() == objetivo:
            return i
    return None


def construir_desglose_descripcion(
    desglose: list[dict], catalogo_piezas: list[str], catalogo_sabores: list[str]
) -> tuple[str | None, str | None]:
    """
    Construye el JSON que va en ventasproductos.descripcion para productos type 2
    (alitas/boneless), con el formato que imprime la cocina:

        [[{"key":"","value":"Cantidad"},{"key":"","value":"Sabor"}],
         [{"key":"<idx_piezas>","value":"5 Piezas"},{"key":"<idx_sabor>","value":"Habanero"}], ...]

    El 'key' de cada celda es el índice 0-based del valor en su catálogo de configs.

    Returns:
        (json_str, None) si todo bien, o (None, mensaje_error) si un sabor/presentación
        no existe en los catálogos.
    """
    filas = [[{"key": "", "value": "Cantidad"}, {"key": "", "value": "Sabor"}]]
    for entrada in desglose:
        piezas = str(entrada.get("piezas", "")).strip()
        sabor = str(entrada.get("sabor", "")).strip()
        idx_p = _indice_en_catalogo(piezas, catalogo_piezas)
        idx_s = _indice_en_catalogo(sabor, catalogo_sabores)
        if idx_p is None:
            return None, f"Presentación no válida: '{piezas}'. Opciones: {', '.join(catalogo_piezas)}"
        if idx_s is None:
            return None, f"Sabor no válido: '{sabor}'. Opciones: {', '.join(catalogo_sabores)}"
        filas.append([
            {"key": str(idx_p), "value": catalogo_piezas[idx_p]},
            {"key": str(idx_s), "value": catalogo_sabores[idx_s]},
        ])
    # Separadores compactos para que coincida con el formato exacto del POS
    return json.dumps(filas, ensure_ascii=False, separators=(",", ":")), None


# ════════════════════════════════════════════════════════════
# Registro de pedidos en la base de datos del POS (ventas + ventasproductos)
# ════════════════════════════════════════════════════════════

_NOMBRE_USUARIO_BOT = "WhatsApp Bot"


async def obtener_id_usuario_bot() -> int:
    """
    Devuelve el id del usuario 'WhatsApp Bot' en la tabla users.

    Lo busca SIEMPRE por nombre (no por id fijo): al migrar a otra base de
    producción el id puede cambiar, pero el nombre se mantiene. Si no existe,
    lo crea con un password aleatorio (el bot nunca inicia sesión).
    """
    conn = await aiomysql.connect(**_db_config())
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id FROM users WHERE name = %s LIMIT 1", (_NOMBRE_USUARIO_BOT,)
            )
            row = await cur.fetchone()
            if row:
                return int(row["id"])

            ahora = datetime.now()
            await cur.execute(
                "INSERT INTO users (name, email, password, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, 1, %s, %s)",
                (_NOMBRE_USUARIO_BOT, "bot@whatsapp.local", secrets.token_hex(32), ahora, ahora),
            )
            await conn.commit()
            logger.info(f"Usuario '{_NOMBRE_USUARIO_BOT}' creado con id {cur.lastrowid}")
            return int(cur.lastrowid)
    finally:
        conn.close()


async def registrar_pedido(
    items: list[dict],
    tipo_entrega: str,
    cantidad_recibida: float,
    client_id: int | None = None,
    direccion: str | None = None,
) -> dict:
    """
    Inserta un pedido CONFIRMADO en ventas + ventasproductos y devuelve el folio.

    El folio es el campo ventaId (auto_increment) de la tabla ventas. Una vez
    generado NO se puede modificar el pedido (regla de negocio).

    Args:
        items: lista de líneas del pedido, cada una:
            {"producto_id": int, "cantidad": int, "descripcion": str|None}
            'descripcion' es opcional (sabores/notas de esa línea).
        tipo_entrega: "domicilio" → ventas.type=2 | "recoger" → ventas.type=3
        cantidad_recibida: monto con el que paga el cliente (para calcular el cambio)
        client_id: id del cliente en la tabla clients, o None si no está registrado
        direccion: dirección de entrega (informativa)

    Returns:
        {"ok": True, "folio": int, "total": float, "cambio": float}
        o {"ok": False, "error": "..."}
    """
    if not items:
        return {"ok": False, "error": "El pedido no tiene productos."}

    # type: 2 = domicilio, 3 = pasa a recoger
    tipo_venta = 3 if tipo_entrega == "recoger" else 2

    # El usuario del POS bajo el que queda registrado el pedido
    id_bot = await obtener_id_usuario_bot()

    # Catálogos para el desglose de sabores (productos type 2)
    catalogo_piezas = await obtener_catalogo_config("pieces")
    catalogo_sabores = await obtener_catalogo_config("flavors")

    conn = await aiomysql.connect(**_db_config())
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # 1) Traer precio y tipo REALES desde la DB (no confiar en lo que diga el modelo)
            ids = [int(it["producto_id"]) for it in items if it.get("producto_id") is not None]
            if not ids:
                return {"ok": False, "error": "No se especificaron productos válidos."}
            placeholders = ",".join(["%s"] * len(ids))
            await cur.execute(
                f"SELECT id, price, type FROM products WHERE id IN ({placeholders}) AND active = 1",
                ids,
            )
            info = {int(r["id"]): {"price": float(r["price"]), "type": r["type"]} for r in await cur.fetchall()}

            faltantes = sorted({i for i in ids if i not in info})
            if faltantes:
                return {"ok": False, "error": f"Productos no encontrados o inactivos: {faltantes}"}

            # 2) Calcular totales y armar las líneas (con desglose de sabores si es type 2)
            lineas = []
            monto_total = 0.0
            cantidad_productos = 0
            for it in items:
                pid = int(it["producto_id"])
                cant = int(it.get("cantidad", 0))
                if cant <= 0:
                    continue

                # Productos type 2 (alitas/boneless) requieren desglose de sabores en descripcion
                if info[pid]["type"] == 2:
                    desglose = it.get("desglose")
                    if not desglose:
                        return {"ok": False, "error": f"El producto {pid} (alitas/boneless) requiere el desglose de sabores."}
                    descripcion, err = construir_desglose_descripcion(desglose, catalogo_piezas, catalogo_sabores)
                    if err:
                        return {"ok": False, "error": err}
                else:
                    descripcion = it.get("descripcion")

                subtotal = round(info[pid]["price"] * cant, 2)
                monto_total += subtotal
                cantidad_productos += cant
                lineas.append((pid, cant, subtotal, descripcion))

            if not lineas:
                return {"ok": False, "error": "El pedido no tiene cantidades válidas."}

            monto_total = round(monto_total, 2)
            ahora = datetime.now()

            # 3) Cabecera del pedido (estatus=1 → Pedido Abierto, visible en comandas)
            await cur.execute(
                "INSERT INTO ventas "
                "(IdUsuario, client_id, montoTotal, montoTotalDescuento, montoSubtotal, "
                " montoIva, cantidadRecibida, cantidadProductos, type, estatus, activo, "
                " apply_discount, payment_type, created_at, updated_at) "
                "VALUES (%s, %s, %s, NULL, 0, 0, %s, %s, %s, 1, 1, 0, 1, %s, %s)",
                (id_bot, client_id, monto_total, cantidad_recibida, cantidad_productos,
                 tipo_venta, ahora, ahora),
            )
            folio = int(cur.lastrowid)

            # 4) Detalle del pedido (delete_flag=0 → línea vigente)
            for orden, (pid, cant, subtotal, desc) in enumerate(lineas, start=1):
                await cur.execute(
                    "INSERT INTO ventasproductos "
                    "(IdProducto, IdVenta, cantidad, montoVenta, descripcion, `order`, "
                    " estatus, delete_flag, id_user_create, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 1, 0, %s, %s, %s)",
                    (pid, folio, cant, subtotal, desc, orden, id_bot, ahora, ahora),
                )

        await conn.commit()
        cambio = round(float(cantidad_recibida) - monto_total, 2)
        logger.info(f"Pedido registrado — folio={folio} total={monto_total} type={tipo_venta}")
        return {
            "ok": True,
            "folio": folio,
            "total": monto_total,
            "cambio": cambio if cambio > 0 else 0.0,
        }

    except Exception as e:
        await conn.rollback()
        logger.error(f"Error al registrar pedido: {e}")
        return {"ok": False, "error": "No se pudo registrar el pedido por un problema técnico."}
    finally:
        conn.close()
