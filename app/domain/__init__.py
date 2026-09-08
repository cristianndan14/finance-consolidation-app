"""Logica de dominio: pura, sin I/O, sin dependencias de framework.

Todo lo que esta aca se testea sin base de datos, sin red y sin LLM. Es donde vive
la correctitud del proyecto: parseo de montos, inferencia de fechas, deduplicacion,
cuotas y validacion de extracciones.
"""
