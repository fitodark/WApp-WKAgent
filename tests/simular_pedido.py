# tests/simular_pedido.py — Pedido simulado del agente SIN llamar al modelo (QA)
#
# Ejecuta exactamente lo que hace el agente cuando el cliente confirma: la herramienta
# registrar_pedido (INSERT en ventas + ventasproductos con venta_agente=1) y, si sale un
# folio nuevo, la impresión de la orden en Laravel. No consume la key de OpenRouter.
#
# Uso:
#   python tests/simular_pedido.py                    # domicilio, envío express (Acatlima)
#   python tests/simular_pedido.py --envio gratuito   # domicilio, envío gratuito (Centro)
#   python tests/simular_pedido.py --recoger          # pasa a recoger
#   python tests/simular_pedido.py --sin-imprimir     # solo el insert
#
# OJO: inserta en la BD del .env (la local en QA). El pedido queda real en la bandeja.

import os
import sys
import asyncio
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from agent import brain_common  # noqa: E402
from agent.tools import buscar_cliente_por_telefono  # noqa: E402

# Cliente de prueba de QA: FITO (id 183)
TELEFONO_PRUEBA = "5219535409577"
COLONIAS = {"express": "Acatlima", "gratuito": "Centro"}


async def main(args):
    cliente = await buscar_cliente_por_telefono(TELEFONO_PRUEBA)
    if not cliente:
        print(f"No se encontró el cliente de prueba {TELEFONO_PRUEBA}")
        return
    print(f"Cliente: {cliente['name']} (id {cliente['id']})")

    # Mismo formato que manda el modelo: 10 alitas mitad y mitad
    pedido = {
        "items": [{
            "producto_id": args.producto,
            "cantidad": 1,
            "desglose": [
                {"piezas": "Mitad", "sabor": "Habanero"},
                {"piezas": "Mitad", "sabor": "A la Diabla"},
            ],
        }],
        "metodo_pago": "efectivo",
        "cantidad_recibida": args.paga_con,
    }
    if args.recoger:
        pedido["tipo_entrega"] = "recoger"
    else:
        pedido.update({
            "tipo_entrega": "domicilio",
            "tipo_envio": args.envio,
            "colonia": COLONIAS[args.envio],
            "direccion": cliente.get("address") or "Dirección de prueba QA",
        })
        # Domicilio con referencia, igual que la arma el agente
        if cliente.get("reference"):
            pedido["direccion"] += f" ({cliente['reference']})"

    if args.sin_imprimir:
        brain_common._programar_impresion = lambda folio: print(f"(impresión omitida para #{folio})")

    resultado = await brain_common.ejecutar_herramienta(
        "registrar_pedido", pedido, cliente["id"], TELEFONO_PRUEBA, "qa-simulado"
    )
    print("Resultado:", resultado)

    # Esperar a que termine la impresión en segundo plano antes de salir
    pendientes = list(brain_common._impresiones_pendientes)
    if pendientes:
        impresion = (await asyncio.gather(*pendientes))[0]
        print("Impresión:", impresion)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Pedido simulado del agente (sin modelo)")
    p.add_argument("--envio", choices=["express", "gratuito"], default="express")
    p.add_argument("--recoger", action="store_true")
    p.add_argument("--producto", type=int, default=107, help="producto_id (default 107, 10 alitas)")
    p.add_argument("--paga-con", type=float, default=200.0)
    p.add_argument("--sin-imprimir", action="store_true")
    asyncio.run(main(p.parse_args()))
