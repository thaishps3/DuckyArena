"""
Lógica de negocio del motor de partidas (quiz_games).

Es la ÚNICA fuente de verdad para: generar el PIN, iniciar y avanzar la partida,
calificar respuestas y ordenar el ranking. Tanto las vistas HTTP como el consumer
de WebSocket llaman a estas funciones, de modo que ambos caminos aplican exactamente
las mismas reglas (ver "Fase 2" del documento maestro).

Convenciones:
  - Game.current_question es un ÍNDICE que empieza en 0 (primera pregunta = 0).
  - Las funciones devuelven diccionarios simples; las que pueden fallar incluyen
    la clave 'error' (mensaje para el usuario) y 'code' (motivo técnico estable).
"""
import logging
import math
import secrets
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import IntegrityError, transaction
from django.utils import timezone

from quizzes.models import Answer, Question

from .models import Game, GamePlayer, PlayerAnswer

# --- Reglas de puntuación (un solo lugar para ajustarlas) ---------------------
logger = logging.getLogger(__name__)

GRACE_SECONDS = 1.5        # margen por latencia de red tras agotarse el tiempo
MIN_TIME_FACTOR = 0.5      # el acierto rápido vale hasta 100 %, el lento nunca baja de 50 %
STREAK_STEP = 100          # bono por cada acierto consecutivo a partir del segundo
STREAK_BONUS_CAP = 500     # tope del bono de racha


def generate_unique_game_code():
    """
    Genera un PIN numérico de 6 dígitos y comprueba con .exists() que ninguna
    partida lo esté usando (restricción unique del modelo Game).
    """
    while True:
        code = f"{secrets.randbelow(1000000):06d}"
        if not Game.objects.filter(code=code).exists():
            return code


def get_current_question(game):
    """Pregunta en curso (índice 0-based en game.current_question) o None si está fuera de rango."""
    index = game.current_question
    if index < 0:
        return None
    window = list(game.quiz.questions.order_by('order')[index:index + 1])
    return window[0] if window else None


def ranked_players(game):
    """Clasificación oficial: más puntos primero; a igualdad, quien se unió antes."""
    return game.players.select_related('player').order_by('-score', 'joined_at')


def calculate_points(base_points, time_limit, elapsed, streak):
    """
    Puntos de un acierto en tiempo.
      factor de tiempo = max(0.5, 1 - tiempo / límite)
      bono de racha    = min(500, (racha - 1) * 100)   (racha = aciertos seguidos incluido este)
    """
    if time_limit > 0:
        time_factor = max(MIN_TIME_FACTOR, 1.0 - (elapsed / time_limit))
    else:
        time_factor = MIN_TIME_FACTOR
    streak_bonus = min(STREAK_BONUS_CAP, max(0, (streak - 1) * STREAK_STEP))
    return int(base_points * time_factor + streak_bonus)


def best_streak(game_player):
    """
    Mejor racha de un jugador: la secuencia más larga de aciertos válidos seguidos.
    Se calcula leyendo sus respuestas guardadas (no hace falta un campo nuevo en el modelo),
    en el orden de las preguntas. Un acierto válido es el que sumó puntos: un acierto fuera
    de tiempo no suma ni cuenta, igual que en submit_answer, donde rompe la racha.
    Una pregunta sin responder no crea respuesta, así que tampoco rompe la racha.
    """
    best = current = 0
    answers = game_player.answers.order_by('question__order', 'pk').values_list('points_awarded', flat=True)
    for points in answers:
        if points > 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


# --- XP del personaje (fórmula de DAR_formulas.pdf, "Ecomotor") ---------------
# Son valores PROVISIONALES hasta que exista la app `ecomotor` con el personaje:
# mientras tanto se usan dificultad "facil" y estadísticas INT/VEL = 5 (valor por defecto del PDF).
BASE_XP = 10                      # el PDF sustituye los "10 XP fijos" por la fórmula
DEFAULT_STAT = 5
DIFFICULTY_MULTIPLIERS = {
    'facil': Decimal('1.00'),
    'media': Decimal('1.25'),
    'dificil': Decimal('1.50'),
    'experto': Decimal('2.00'),
}
COMPLETION_XP = 20                # XP fija por completar la sesión
PODIUM_XP = {1: 50, 2: 30, 3: 20} # XP fija extra según el puesto del podio


def speed_multiplier(seconds, vel_stat=DEFAULT_STAT):
    """Multiplicador de rapidez: <2 s mejor; cada punto de VEL añade un margen leve."""
    vel_bonus = Decimal(str(vel_stat)) * Decimal('0.01')
    if seconds < 2.0:
        return Decimal('1.50') + vel_bonus
    if seconds <= 4.0:
        return Decimal('1.25') + vel_bonus / 2
    if seconds <= 7.0:
        return Decimal('1.00')
    return Decimal('0.80')


def calculate_xp(difficulty='facil', seconds=0.0, int_stat=DEFAULT_STAT,
                 vel_stat=DEFAULT_STAT, streak=0, base_xp=BASE_XP):
    """
    XP de UNA respuesta correcta (fórmula del PDF de fórmulas):
      XP = base × dificultad × rapidez × (1 + 0,02 × INT) × bono de racha, redondeado hacia abajo
      bono de racha = ×1,10 si la racha es de 3 aciertos o más.
    Es independiente de los puntos de la partida (calculate_points): los puntos
    ordenan el ranking; el XP es el progreso del personaje.
    """
    m_diff = DIFFICULTY_MULTIPLIERS.get(str(difficulty).lower(), Decimal('1.00'))
    m_speed = speed_multiplier(seconds, vel_stat)
    f_int = Decimal('1.00') + Decimal(str(int_stat)) * Decimal('0.02')
    m_bonus = Decimal('1.10') if streak >= 3 else Decimal('1.00')
    return int(math.floor(Decimal(str(base_xp)) * m_diff * m_speed * f_int * m_bonus))


def session_end_xp(rank=None):
    """XP fija al cerrar la sesión: 20 por completar + 50/30/20 si quedó 1.º/2.º/3.º."""
    return COMPLETION_XP + PODIUM_XP.get(rank, 0)


def _locked_game(game):
    """Relee la partida dentro de la transacción (con bloqueo de fila donde la BD lo soporte)."""
    return Game.objects.select_for_update().select_related('quiz').get(pk=game.pk)


def start_game(game):
    """LOBBY -> RUNNING con current_question = 0. Devuelve {'success': True} o {'error', 'code'}."""
    with transaction.atomic():
        game = _locked_game(game)
        if game.status != Game.Status.LOBBY:
            return {'success': False, 'error': 'La partida ya no está en espera.', 'code': 'bad_state'}
        if not game.players.exists():
            return {'success': False, 'error': 'No puedes iniciar la partida sin jugadores en la sala.', 'code': 'no_players'}
        if not game.quiz.is_published:
            return {'success': False, 'error': 'El cuestionario ya no está publicado y no puede jugarse.', 'code': 'not_published'}
        if not game.quiz.questions.exists():
            return {'success': False, 'error': 'El cuestionario no contiene preguntas para jugar.', 'code': 'no_questions'}

        now = timezone.now()
        game.status = Game.Status.RUNNING
        game.started_at = now
        game.question_started_at = now
        game.current_question = 0
        game.save(update_fields=['status', 'started_at', 'question_started_at', 'current_question'])
    return {'success': True, 'event': 'game_started', 'current_question': 0, 'current_number': 1}


def advance_game(game):
    """
    Avanza a la siguiente pregunta o, si era la última, finaliza la partida.
    Devuelve {'success': True, 'event': 'next_question' | 'game_finished', ...}.
    """
    with transaction.atomic():
        game = _locked_game(game)
        if game.status != Game.Status.RUNNING:
            return {'success': False, 'error': 'La partida no está en curso.', 'code': 'bad_state'}

        total = game.quiz.questions.count()
        if game.current_question + 1 < total:
            game.current_question += 1
            game.question_started_at = timezone.now()
            game.save(update_fields=['current_question', 'question_started_at'])
            return {
                'success': True,
                'event': 'next_question',
                'current_question': game.current_question,
                'current_number': game.current_question + 1,
                'total_questions': total,
            }

        game.status = Game.Status.FINISHED
        game.finished_at = timezone.now()
        game.save(update_fields=['status', 'finished_at'])
        return {
            'success': True,
            'event': 'game_finished',
            'leaderboard_url': f"/games/leaderboard/{game.code}/",
        }


def finish_game(game):
    """
    Finaliza la partida (manual o al acabar). Devuelve el evento a difundir.
    No toca una partida que ya estaba FINISHED o CANCELLED.
    """
    with transaction.atomic():
        game = _locked_game(game)
        if game.status in (Game.Status.FINISHED, Game.Status.CANCELLED):
            return {'success': False, 'error': 'La partida ya estaba cerrada.', 'code': 'bad_state'}
        game.status = Game.Status.FINISHED
        game.finished_at = timezone.now()
        game.save(update_fields=['status', 'finished_at'])
    return {
        'success': True,
        'event': 'game_finished',
        'leaderboard_url': f"/games/leaderboard/{game.code}/",
    }


def cancel_game(game):
    """
    LOBBY -> CANCELLED. Solo se puede cancelar una sala que aún no empezó:
    una partida en curso se finaliza (finish_game), no se cancela.
    """
    with transaction.atomic():
        game = _locked_game(game)
        if game.status != Game.Status.LOBBY:
            return {'success': False, 'error': 'Solo se puede cancelar una sala que aún está en espera.', 'code': 'bad_state'}
        game.status = Game.Status.CANCELLED
        game.finished_at = timezone.now()
        game.save(update_fields=['status', 'finished_at'])
    return {'success': True, 'event': 'game_cancelled'}


def broadcast_to_room(code, payload):
    """
    Avisa en tiempo real a todos los conectados a la sala (docente y alumnos).
    Las vistas HTTP lo usan para que el resto de pantallas reaccionen igual que
    cuando la acción llega por WebSocket. Si no hay capa de canales, no falla.
    """
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            f"game_{code}", {'type': 'broadcast_payload', 'payload': payload}
        )
    except Exception:
        # El aviso en vivo es un extra: la acción en la base de datos ya se hizo,
        # pero el fallo se deja registrado para poder diagnosticarlo.
        logger.exception("No se pudo difundir el aviso a la sala %s", code)


def submit_answer(game, user, question_id, selected_answer_id):
    """
    Califica una respuesta aplicando las 4 validaciones obligatorias del PDF:
      1. el usuario participa en la partida (GamePlayer),
      2. la partida está en estado RUNNING,
      3. la respuesta pertenece a la pregunta en curso,
      4. el alumno no había respondido ya esa pregunta.
    El navegador nunca decide si acierta: se contrasta con Answer.is_correct en la BD y el
    tiempo se mide en el servidor. Todo se guarda dentro de transaction.atomic().

    Éxito:  {'is_correct', 'points_awarded', 'total_score', 'streak'}
    Fallo:  {'error': mensaje, 'code': motivo}  (code: bad_state, not_participant,
            invalid, not_current) o {'already_answered': True, 'code': 'already_answered'}.
    """
    if not user or not user.is_authenticated:
        return {'error': 'Debes iniciar sesión.', 'code': 'not_participant'}

    with transaction.atomic():
        game = _locked_game(game)

        if game.status != Game.Status.RUNNING:
            return {'error': 'La partida no está en curso.', 'code': 'bad_state'}

        player_entry = GamePlayer.objects.select_for_update().filter(game=game, player=user).first()
        if not player_entry:
            return {'error': 'Participante no registrado en la sala.', 'code': 'not_participant'}

        question = Question.objects.filter(pk=question_id, quiz=game.quiz).first()
        selected_answer = Answer.objects.filter(pk=selected_answer_id, question=question).first() if question else None
        if not question or not selected_answer:
            return {'error': 'Pregunta o alternativa inválida.', 'code': 'invalid'}

        current = get_current_question(game)
        if current is None or current.pk != question.pk:
            return {'error': 'La pregunta enviada no es la que está en curso.', 'code': 'not_current'}

        if PlayerAnswer.objects.filter(game_player=player_entry, question=question).exists():
            return {'already_answered': True, 'error': None, 'code': 'already_answered'}

        # Tiempo oficial medido en el servidor (el tiempo que diga el navegador se ignora)
        now = timezone.now()
        start = game.question_started_at or game.started_at or now
        elapsed = max(0.1, (now - start).total_seconds())
        time_limit = float(question.time_limit)

        is_correct = selected_answer.is_correct
        points = 0
        if is_correct and elapsed <= time_limit + GRACE_SECONDS:
            player_entry.current_streak += 1
            player_entry.correct_answers += 1
            points = calculate_points(question.points, time_limit, min(elapsed, time_limit), player_entry.current_streak)
            player_entry.score += points
        else:
            # Fallo, o acierto fuera de tiempo: sin puntos y se rompe la racha
            player_entry.current_streak = 0

        try:
            with transaction.atomic():
                PlayerAnswer.objects.create(
                    game_player=player_entry,
                    question=question,
                    selected_answer=selected_answer,
                    is_correct=is_correct,
                    points_awarded=points,
                    response_time=round(elapsed, 3),
                    answered_at=now,
                )
        except IntegrityError:
            # Doble envío simultáneo: la restricción única de la BD actúa como último cerrojo
            return {'already_answered': True, 'error': None, 'code': 'already_answered'}

        player_entry.save(update_fields=['score', 'correct_answers', 'current_streak'])

    return {
        'is_correct': is_correct,
        'points_awarded': points,
        'total_score': player_entry.score,
        'streak': player_entry.current_streak,
    }
