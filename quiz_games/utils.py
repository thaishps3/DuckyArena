"""
Compatibilidad: la lógica vive ahora en quiz_games/services.py.
Este módulo solo reexporta los nombres antiguos para no romper importaciones existentes.
"""
from .services import generate_unique_game_code, get_current_question  # noqa: F401
