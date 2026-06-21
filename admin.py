# admin.py — Herramienta de administración de Wings Kings Agent
# Uso: python admin.py [comando] [argumentos]

import asyncio
import sys
from agent.memory import inicializar_db, bloquear_numero, desbloquear_numero, listar_bloqueados

AYUDA = """
Wings Kings Agent — Administración
===================================

Comandos disponibles:

  python admin.py bloquear [telefono] [motivo]
      Bloquea un número. El agente dejará de responderle.
      Ejemplo: python admin.py bloquear 9535001234 "Pedidos falsos repetidos"

  python admin.py desbloquear [telefono]
      Desbloquea un número bloqueado anteriormente.
      Ejemplo: python admin.py desbloquear 9535001234

  python admin.py listar
      Muestra todos los números bloqueados con su motivo.
"""


async def main():
    await inicializar_db()

    if len(sys.argv) < 2:
        print(AYUDA)
        return

    comando = sys.argv[1].lower()

    if comando == "bloquear":
        if len(sys.argv) < 4:
            print("Uso: python admin.py bloquear [telefono] [motivo]")
            return
        telefono = sys.argv[2]
        motivo = sys.argv[3]
        await bloquear_numero(telefono, motivo)
        print(f"✅ Número {telefono} bloqueado.")
        print(f"   Motivo: {motivo}")

    elif comando == "desbloquear":
        if len(sys.argv) < 3:
            print("Uso: python admin.py desbloquear [telefono]")
            return
        telefono = sys.argv[2]
        desbloqueado = await desbloquear_numero(telefono)
        if desbloqueado:
            print(f"✅ Número {telefono} desbloqueado.")
        else:
            print(f"⚠️  El número {telefono} no estaba en la lista de bloqueados.")

    elif comando == "listar":
        bloqueados = await listar_bloqueados()
        if not bloqueados:
            print("No hay números bloqueados.")
            return
        print(f"\n{'='*55}")
        print(f"  Números bloqueados ({len(bloqueados)} total)")
        print(f"{'='*55}")
        for b in bloqueados:
            print(f"\n  📵 {b['telefono']}")
            print(f"     Motivo: {b['motivo']}")
            print(f"     Fecha:  {b['bloqueado_en']}")
        print()

    else:
        print(f"Comando no reconocido: {comando}")
        print(AYUDA)


if __name__ == "__main__":
    asyncio.run(main())
