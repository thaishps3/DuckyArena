"""
Identidad y permisos de QuizArenas en UN solo lugar.

Por que existe este archivo
---------------------------
Antes, las preguntas "quien es profesor?" y "quien es el anfitrion de esta sala?"
estaban repetidas en muchas vistas y en el consumer de WebSocket. Si Core (Oscar)
decide otra forma de representar roles o usuarios (CORE-01 / CORE-04), ahora solo
hay que cambiar ESTE archivo; el motor del juego no se toca.

Regla de oro: este archivo NO modifica el modelo User global ni inventa roles.
Solo lee lo que ya existe (is_superuser, is_staff, profile.role).
El comportamiento es exactamente el mismo que antes.
"""


def is_teacher(user):
    """Profesor: superusuario, staff, o perfil con rol TEACHER."""
    return bool(
        user.is_superuser
        or user.is_staff
        or (hasattr(user, 'profile') and getattr(user.profile, 'role', None) == 'TEACHER')
    )


def is_host(game, user):
    """Anfitrion de una sala concreta (no depende de ningun rol)."""
    return game.host_id == getattr(user, 'id', None)


def can_create_game(user, quiz):
    """Puede crear una partida: ser profesor y creador del cuestionario (o superusuario)."""
    return is_teacher(user) and (quiz.creator_id == user.id or user.is_superuser)
