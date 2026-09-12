"""Adapter de LLM: puerto, implementaciones y prompts versionados.

El resto de la aplicacion depende de `ports.LLMExtractor`, nunca de `gemini.py`.
Eso es lo que permite que los tests corran con `fake.py` — gratis, deterministico
y sin red — y que cambiar de proveedor sea escribir un archivo.
"""
