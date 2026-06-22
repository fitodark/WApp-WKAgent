# agent/tools.py — Herramientas del agente Wings Kings
# Generado por AgentKit

import os
import re
import json
import yaml
import secrets
import logging
import aiomysql
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("agentkit")

# Zona horaria del negocio (Huajuapan de León, Oaxaca). Oaxaca ya no observa
# horario de verano, así que un offset fijo UTC-6 es correcto; usamos ZoneInfo
# si está disponible (Linux/prod) y caemos al offset fijo si no (Windows sin tzdata).
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Mexico_City")
except Exception:
    _TZ = timezone(timedelta(hours=-6))


def _ahora_local() -> datetime:
    """Hora actual en la zona horaria del negocio (México)."""
    return datetime.now(_TZ)


def _db_config() -> dict:
    """Lee la configuración de MariaDB desde variables de entorno."""
    return {
        "host": os.getenv("CLIENTS_DB_HOST", "localhost"),
        "port": int(os.getenv("CLIENTS_DB_PORT", 3307)),
        "user": os.getenv("CLIENTS_DB_USER", "root"),
        "password": os.getenv("CLIENTS_DB_PASSWORD", ""),
        "db": os.getenv("CLIENTS_DB_NAME", "wings_kings_prod_120526_1"),
    }


async def _numeros_propios() -> set[str]:
    """
    Últimos 10 dígitos de los números PROPIOS del negocio (no son clientes).
    Evita que, si la resolución del @lid falla y OpenWA devuelve el número del
    establecimiento, una venta se cuelgue del cliente "Establecimiento".
    """
    propios = set()
    for v in await obtener_catalogo_config("local_phone_number"):
        d = "".join(filter(str.isdigit, v or ""))
        if len(d) >= 10:
            propios.add(d[-10:])
    return propios


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

    # Un número propio del negocio NUNCA es un cliente (no casar con "Establecimiento")
    if not sufijo or sufijo in await _numeros_propios():
        return None

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

    Marca cada producto según promotion_type (clasificación de canal del POS,
    catálogo configs 'products_promotion_type', mantenido desde el POS Laravel):
      - promotion_type = 2 (Domicilio) → disponible para domicilio Y local
      - promotion_type = 1 (General)   → solo consumo en el local
        (incluye TODAS las bebidas alcohólicas y los combos/promos)
    """
    try:
        conn = await aiomysql.connect(**_db_config())
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, name, detail, price, type, promotion_type FROM products "
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
                disponible_domicilio = p.get("promotion_type") == 2
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


# ── Horario de atención (configurable desde la tabla configs) ──────────────

_DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

# Horario por defecto (fallback si configs['business_hours'] no tiene ese día).
# Minutos desde medianoche; 24:00 = 1440 = cierre en medianoche. None = cerrado.
_HORARIO_DEFAULT = {
    0: (16 * 60, 24 * 60),  # Lunes
    1: (16 * 60, 24 * 60),  # Martes
    2: (16 * 60, 24 * 60),  # Miércoles
    3: (16 * 60, 24 * 60),  # Jueves
    4: (16 * 60, 24 * 60),  # Viernes
    5: (16 * 60, 24 * 60),  # Sábado
    6: (13 * 60, 24 * 60),  # Domingo
}


def _quitar_acentos(texto: str) -> str:
    return texto.translate(str.maketrans("áéíóúüÁÉÍÓÚÜ", "aeiouuAEIOUU"))


def _weekday_de_nombre(nombre: str) -> int | None:
    """Mapea 'Lunes'..'Domingo' (con o sin acento, cualquier caso) a 0..6."""
    objetivo = _quitar_acentos(nombre).strip().lower()
    for i, d in enumerate(_DIAS):
        if _quitar_acentos(d).lower() == objetivo:
            return i
    return None


def _fmt_minutos(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


async def _obtener_horario_semana() -> dict:
    """
    Horario por día: weekday -> (apertura, cierre) en minutos, o None si cerrado.
    Parte del default y aplica los overrides de configs['business_hours'], cuyos
    valores son del estilo 'Lunes [16:00-24:00]' o 'Viernes [NOT_WORKING]'.
    """
    horario = dict(_HORARIO_DEFAULT)
    for value in await obtener_catalogo_config("business_hours"):
        m = re.match(r"\s*([A-Za-zÁÉÍÓÚáéíóúüÜñÑ]+)\s*\[\s*(.+?)\s*\]", value or "")
        if not m:
            logger.warning(f"business_hours con formato inválido (ignorado): {value!r}")
            continue
        weekday = _weekday_de_nombre(m.group(1))
        if weekday is None:
            logger.warning(f"business_hours con día no reconocido (ignorado): {value!r}")
            continue
        cuerpo = m.group(2).strip()
        if cuerpo.upper() == "NOT_WORKING":
            horario[weekday] = None
            continue
        # Separador tolerante: guion (-, –, —) y/o espacios. Acepta "16:00-24:00" y "16:00 24:00".
        rango = re.match(r"(\d{1,2}):(\d{2})\s*[-–—]?\s*(\d{1,2}):(\d{2})", cuerpo)
        if not rango:
            logger.warning(f"business_hours con rango inválido (ignorado): {value!r}")
            continue
        apertura = int(rango.group(1)) * 60 + int(rango.group(2))
        cierre = int(rango.group(3)) * 60 + int(rango.group(4))
        horario[weekday] = (apertura, cierre)
    return horario


async def _obtener_fechas_cerradas() -> set:
    """
    Fechas cerradas (feriados/días inhábiles) desde configs['business_closed_dates'],
    valores con una fecha 'YYYY-MM-DD' (admite etiqueta extra, ej. '2026-12-25 [Navidad]').
    Sin filas => set vacío => abierto todos los días del año.
    """
    fechas = set()
    for value in await obtener_catalogo_config("business_closed_dates"):
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value or "")
        if not m:
            logger.warning(f"business_closed_dates con fecha inválida (ignorado): {value!r}")
            continue
        try:
            fechas.add(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date())
        except ValueError:
            logger.warning(f"business_closed_dates con fecha inexistente (ignorado): {value!r}")
    return fechas


async def obtener_horario() -> dict:
    """
    Estado de atención AHORA (hora de México), configurable desde la tabla configs:
      - configs['business_hours']: override del horario semanal. Sin filas => default.
      - configs['business_closed_dates']: cierres por fecha (prioridad sobre el horario).
    Retorna: horario (texto), esta_abierto, hora_actual, dia_actual.
    """
    ahora = _ahora_local()
    dia = ahora.weekday()
    minutos = ahora.hour * 60 + ahora.minute

    # 1) Cierre por fecha (feriado) — tiene prioridad sobre el horario semanal
    if ahora.date() in await _obtener_fechas_cerradas():
        return {
            "horario": f"Cerrado hoy ({ahora.strftime('%d/%m/%Y')}) por día inhábil",
            "esta_abierto": False,
            "hora_actual": ahora.strftime("%I:%M %p"),
            "dia_actual": _DIAS[dia],
        }

    # 2) Horario del día (override de configs o default)
    semana = await _obtener_horario_semana()
    rango = semana.get(dia, _HORARIO_DEFAULT[dia])

    if rango is None:
        esta_abierto = False
        horario_str = f"{_DIAS[dia]}: cerrado"
    else:
        apertura, cierre = rango
        esta_abierto = apertura <= minutos < cierre
        horario_str = f"{_DIAS[dia]} {_fmt_minutos(apertura)}–{_fmt_minutos(cierre)}"

    return {
        "horario": horario_str,
        "esta_abierto": esta_abierto,
        "hora_actual": ahora.strftime("%I:%M %p"),
        "dia_actual": _DIAS[dia],
    }


async def obtener_horario_semanal_texto() -> str:
    """
    Horario semanal vigente en texto (default + overrides de configs), para inyectar
    al prompt. Incluye las próximas fechas cerradas si hay alguna configurada.
    """
    semana = await _obtener_horario_semana()
    lineas = []
    for i, nombre in enumerate(_DIAS):
        rango = semana.get(i)
        if rango is None:
            lineas.append(f"- {nombre}: cerrado")
        else:
            apertura, cierre = rango
            lineas.append(f"- {nombre}: {_fmt_minutos(apertura)}–{_fmt_minutos(cierre)}")
    texto = "\n".join(lineas)

    hoy = _ahora_local().date()
    futuras = sorted(f for f in await _obtener_fechas_cerradas() if f >= hoy)
    if futuras:
        texto += "\nDías cerrados próximos: " + ", ".join(f.strftime("%d/%m/%Y") for f in futuras)
    return texto


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


async def registrar_cliente(
    nombre: str,
    direccion: str | None = None,
    referencia: str | None = None,
    telefono: str | None = None,
) -> dict:
    """
    Da de alta un cliente NUEVO en la tabla clients, solo si su teléfono no existe.

    Si el número ya está registrado NO duplica: devuelve el client_id existente.
    El 'telefono' se inyecta del lado servidor (número real del remitente).

    Returns:
        {"ok": True, "client_id": int, "ya_existia": bool} o {"ok": False, "error": "..."}
    """
    if not nombre or not nombre.strip():
        return {"ok": False, "error": "Falta el nombre completo del cliente."}
    if not telefono:
        return {"ok": False, "error": "No se pudo determinar el número del cliente."}

    # No duplicar: si el número ya está registrado, devolver ese cliente
    existente = await buscar_cliente_por_telefono(telefono)
    if existente:
        return {"ok": True, "client_id": int(existente["id"]), "ya_existia": True}

    # Normalizar el teléfono al formato de 10 dígitos que usa el POS
    digitos = "".join(filter(str.isdigit, telefono))
    phone = digitos[-10:] if len(digitos) >= 10 else digitos

    # No dar de alta con un número propio del negocio (resolución del @lid fallida)
    if not phone or phone in await _numeros_propios():
        return {"ok": False, "error": "No se pudo determinar un número válido del cliente para darlo de alta."}

    ahora = datetime.now()
    try:
        conn = await aiomysql.connect(**_db_config())
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO clients (name, address, reference, phone, active, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s)",
                (nombre.strip(), (direccion.strip() if direccion else None),
                 (referencia.strip() if referencia else None), phone, ahora, ahora),
            )
            await conn.commit()
            nuevo_id = int(cur.lastrowid)
        conn.close()
        logger.info(f"Cliente nuevo dado de alta id={nuevo_id} phone={phone}")
        return {"ok": True, "client_id": nuevo_id, "ya_existia": False}
    except Exception as e:
        logger.error(f"Error al dar de alta cliente: {e}")
        return {"ok": False, "error": "No se pudo dar de alta al cliente por un problema técnico."}


async def registrar_pedido(
    items: list[dict],
    tipo_entrega: str,
    cantidad_recibida: float,
    client_id: int | None = None,
    direccion: str | None = None,
    telefono: str | None = None,
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
        client_id: id del cliente en clients, o None (se intenta resolver por teléfono)
        direccion: dirección de entrega indicada en el chat; se guarda en ventas.direccion_envio
        telefono: número real del remitente (se inyecta del lado servidor) para resolver client_id

    Returns:
        {"ok": True, "folio": int, "total": float, "cambio": float}
        o {"ok": False, "error": "..."}
    """
    if not items:
        return {"ok": False, "error": "El pedido no tiene productos."}

    # Red de seguridad: no registrar pedidos fuera del horario de atención
    horario = await obtener_horario()
    if not horario["esta_abierto"]:
        return {
            "ok": False,
            "error": (
                f"En este momento el negocio está cerrado ({horario['horario']}). "
                "No se puede registrar el pedido; invita al cliente a escribir en horario de atención."
            ),
        }

    # type: 2 = domicilio, 3 = pasa a recoger
    tipo_venta = 3 if tipo_entrega == "recoger" else 2

    # A domicilio la dirección es obligatoria (se guarda en el pedido para el ticket)
    if tipo_venta == 2 and not (direccion and direccion.strip()):
        return {"ok": False, "error": "Falta la dirección de entrega para el pedido a domicilio."}

    # Resolver el cliente por teléfono si no llegó client_id (p.ej. recién dado de alta)
    if client_id is None and telefono:
        encontrado = await buscar_cliente_por_telefono(telefono)
        if encontrado:
            client_id = encontrado["id"]

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
                f"SELECT id, name, price, type, promotion_type FROM products "
                f"WHERE id IN ({placeholders}) AND active = 1",
                ids,
            )
            info = {
                int(r["id"]): {
                    "name": r["name"],
                    "price": float(r["price"]),
                    "type": r["type"],
                    "promotion_type": r["promotion_type"],
                }
                for r in await cur.fetchall()
            }

            faltantes = sorted({i for i in ids if i not in info})
            if faltantes:
                return {"ok": False, "error": f"Productos no encontrados o inactivos: {faltantes}"}

            # Red de seguridad: los productos SOLO EN LOCAL (promotion_type != 2, incluye
            # toda bebida alcohólica y los combos/promos) no pueden salir del establecimiento —
            # ni a domicilio ni para recoger/llevar. Solo se venden para consumo en el local.
            if tipo_venta in (2, 3):
                solo_local = sorted({
                    info[i]["name"] for i in ids if info[i]["promotion_type"] != 2
                })
                if solo_local:
                    return {
                        "ok": False,
                        "error": (
                            "Estos productos son solo para consumo dentro del local y NO se "
                            f"pueden enviar a domicilio ni entregar para llevar: {', '.join(solo_local)}. "
                            "Ofrece alternativas que sí estén disponibles para llevar."
                        ),
                    }

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

            # 3) Cabecera del pedido (estatus=1 → Pedido Abierto, visible en comandas).
            # `order`=1: bandera que indica al POS que debe imprimir los tickets
            # (cocina para alimentos, barra para bebidas).
            await cur.execute(
                "INSERT INTO ventas "
                "(IdUsuario, client_id, montoTotal, montoTotalDescuento, montoSubtotal, "
                " montoIva, cantidadRecibida, cantidadProductos, type, estatus, activo, "
                " `order`, apply_discount, payment_type, venta_agente, direccion_envio, "
                " created_at, updated_at) "
                "VALUES (%s, %s, %s, NULL, 0, 0, %s, %s, %s, 1, 1, 1, 0, 1, 1, %s, %s, %s)",
                (id_bot, client_id, monto_total, cantidad_recibida, cantidad_productos,
                 tipo_venta, (direccion.strip() if direccion else None), ahora, ahora),
            )
            folio = int(cur.lastrowid)

            # 4) Detalle del pedido (delete_flag=0 → línea vigente; `order`=1 → misma
            # bandera de impresión que la cabecera, para que el ticket salga en su lugar).
            for (pid, cant, subtotal, desc) in lineas:
                await cur.execute(
                    "INSERT INTO ventasproductos "
                    "(IdProducto, IdVenta, cantidad, montoVenta, descripcion, `order`, "
                    " estatus, delete_flag, id_user_create, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, 1, 1, 0, %s, %s, %s)",
                    (pid, folio, cant, subtotal, desc, id_bot, ahora, ahora),
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
