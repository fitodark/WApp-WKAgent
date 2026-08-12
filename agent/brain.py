# agent/brain.py — Selector del proveedor de IA (Claude API u OpenRouter)
# Generado por AgentKit

"""
Dispatcher: expone generar_respuesta() apuntando a la implementación del
proveedor configurado en PROVIDER_CHAT (.env). La lógica de negocio (prompt,
tools, ejecución de herramientas) vive en agent/brain_common.py y es
compartida por ambos proveedores — solo cambia cómo se llama a la IA:

- claudeapi   -> agent/brain_claude.py     (SDK oficial anthropic)
- openrouter  -> agent/brain_openrouter.py (SDK openai, vía openrouter.ai)
"""

import os
from dotenv import load_dotenv

load_dotenv()

PROVIDER_CHAT = os.getenv("PROVIDER_CHAT", "claudeapi").lower()

if PROVIDER_CHAT == "claudeapi":
    from agent.brain_claude import generar_respuesta
elif PROVIDER_CHAT == "openrouter":
    from agent.brain_openrouter import generar_respuesta
else:
    raise ValueError(f"PROVIDER_CHAT no soportado: {PROVIDER_CHAT}. Usa: claudeapi u openrouter")
