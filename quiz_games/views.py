import csv
import hashlib
import json
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.db import transaction
from django.db.models import Avg, Max, Count
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse, HttpResponseNotModified
from django.views.decorators.http import require_POST
from quizzes.models import Quiz
from .models import Game, GamePlayer, PlayerAnswer
from .forms import JoinGameForm
from . import services, identity
from .services import generate_unique_game_code, ranked_players


@login_required
@require_POST
def game_create(request, quiz_pk):
    """
    Crea una nueva sala de juego en estado LOBBY para un cuestionario publicado.
    Garantiza atómicamente la asignación del anfitrión y el PIN sin colisión.
    """
    user = request.user
    if not identity.is_teacher(user):
        raise PermissionDenied("Solo los usuarios con rol de PROFESOR pueden iniciar salas de juego.")

    quiz = get_object_or_404(Quiz, pk=quiz_pk)

    if not identity.can_create_game(user, quiz):
        raise PermissionDenied("Solo el profesor creador del cuestionario puede iniciar la partida.")

    if not quiz.is_published:
        messages.error(request, "No se puede iniciar una partida con un cuestionario en modo borrador.")
        return redirect('quizzes:quiz_detail', pk=quiz.pk)

    if not quiz.questions.exists():
        messages.error(request, "El cuestionario debe tener al menos una pregunta antes de jugarse.")
        return redirect('quizzes:quiz_detail', pk=quiz.pk)

    with transaction.atomic():
        pin = generate_unique_game_code()
        game = Game.objects.create(
            quiz=quiz,
            host=user,
            code=pin,
            status=Game.Status.LOBBY,
            current_question=0
        )

    messages.success(request, f"¡Sala de juego creada con éxito! Código PIN: {game.code}")
    return redirect('quiz_games:game_host_lobby', code=game.code)


@login_required
def game_host_lobby(request, code):
    """Panel interactivo del profesor mientras se unen los estudiantes."""
    game = get_object_or_404(Game.objects.select_related('quiz', 'host'), code=code)
    if not identity.is_host(game, request.user):
        raise PermissionDenied("Acceso exclusivo para el anfitrión de la sala.")

    players = game.players.select_related('player').order_by('joined_at')
    return render(request, 'quiz_games/game_host_lobby.html', {
        'game': game,
        'quiz': game.quiz,
        'players': players,
        'players_count': players.count(),
    })


@login_required
@require_POST
def host_kick_player(request, code, player_id):
    """Permite al anfitrión expulsar a un participante no deseado en fase de Lobby."""
    game = get_object_or_404(Game, code=code)
    if not identity.is_host(game, request.user):
        raise PermissionDenied("Solo el anfitrión puede expulsar participantes.")
    if game.status != Game.Status.LOBBY:
        return JsonResponse({'success': False, 'error': 'Solo se puede expulsar en fase de Lobby.'}, status=400)

    with transaction.atomic():
        GamePlayer.objects.filter(game=game, id=player_id).delete()

    return JsonResponse({'success': True, 'kicked_id': player_id})


def _generate_etag(*args):
    raw = ":".join(str(a) for a in args)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


@login_required
def game_lobby_status(request, code):
    """API JSON consultada mediante polling por el anfitrión y los jugadores con soporte 304 ETag."""
    game = get_object_or_404(Game, code=code)
    user = request.user

    # Verificación de pertenencia a la partida
    is_host = identity.is_host(game, user)
    is_player = game.players.filter(player=user).exists()
    if not (is_host or is_player):
        return HttpResponseForbidden("No perteneces a esta partida.")

    players_data = list(
        game.players.select_related('player')
        .order_by('joined_at')
        .values('id', 'player__username', 'joined_at', 'avatar_body', 'avatar_clothes', 'avatar_head', 'avatar_face')
    )
    player_count = len(players_data)
    avatar_tokens = "".join(f"{p['id']}:{p['avatar_body']}:{p['avatar_clothes']}:{p['avatar_head']}:{p['avatar_face']}" for p in players_data)
    etag = _generate_etag(game.status, game.current_question, player_count, avatar_tokens)

    client_etag = request.headers.get('If-None-Match')
    if client_etag and client_etag == etag:
        return HttpResponseNotModified()

    response = JsonResponse({
        'status': game.status,
        'current_question': game.current_question,
        'player_count': player_count,
        'players': [
            {
                'id': p['id'],
                'username': p['player__username'],
                'joined_at': p['joined_at'].strftime('%H:%M:%S'),
                'avatar': {
                    'body': p['avatar_body'] or 'body_yellow',
                    'clothes': p['avatar_clothes'] or '',
                    'head': p['avatar_head'] or '',
                    'face': p['avatar_face'] or 'face_calm'
                }
            }
            for p in players_data
        ]
    })
    response['ETag'] = etag
    return response


@login_required
@require_POST
def update_player_avatar(request, code):
    """
    Actualiza la personalización modular del avatar de un participante en la sala.
    """
    game = get_object_or_404(Game, code=code)
    try:
        data = json.loads(request.body) if request.content_type == 'application/json' else request.POST
        body = data.get('body', 'body_yellow')
        clothes = data.get('clothes', '')
        head = data.get('head', '')
        face = data.get('face', 'face_calm')
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Parámetros inválidos.'}, status=400)

    VALID_BODIES = {'body_yellow', 'body_brown', 'body_black', 'body_white', 'body_pink', 'body_blue', 'body_green', 'body_lavender'}
    VALID_FACES = {'face_focused', 'face_confident', 'face_surprised', 'face_calm'}
    VALID_CLOTHES = {'', 'clothes_caveman', 'clothes_egypt', 'clothes_greek', 'clothes_roman', 'clothes_viking', 'clothes_samurai', 'clothes_medieval', 'clothes_pirate', 'clothes_steampunk', 'clothes_cyberpunk'}
    VALID_HEADS = {'', 'head_caveman', 'head_egypt', 'head_greek', 'head_roman', 'head_viking', 'head_samurai', 'head_medieval', 'head_pirate', 'head_steampunk', 'head_cyberpunk'}

    if body not in VALID_BODIES or face not in VALID_FACES or clothes not in VALID_CLOTHES or head not in VALID_HEADS:
        return JsonResponse({'success': False, 'error': 'Componente de avatar no válido.'}, status=400)

    with transaction.atomic():
        player_entry = GamePlayer.objects.select_for_update().filter(game=game, player=request.user).first()
        if not player_entry:
            return JsonResponse({'success': False, 'error': 'Participante no registrado en la sala.'}, status=403)

        player_entry.avatar_body = body
        player_entry.avatar_clothes = clothes
        player_entry.avatar_head = head
        player_entry.avatar_face = face
        player_entry.save(update_fields=['avatar_body', 'avatar_clothes', 'avatar_head', 'avatar_face'])

    return JsonResponse({
        'success': True,
        'avatar': player_entry.avatar_dict
    })


@login_required
@require_POST
def host_start_game(request, code):
    """
    Transiciona el estado de LOBBY a RUNNING, fija started_at y activa
    la primera pregunta de forma atómica.
    """
    game = get_object_or_404(Game, code=code)

    if not identity.is_host(game, request.user):
        raise PermissionDenied("Solo el anfitrión puede iniciar la partida.")

    if game.status != Game.Status.LOBBY:
        messages.warning(request, f"La partida ya no está en espera (Estado actual: {game.get_status_display()}).")
        return redirect('quiz_games:game_host_play', code=game.code)

    result = services.start_game(game)
    if not result['success']:
        messages.error(request, result['error'])
        return redirect('quiz_games:game_host_lobby', code=game.code)

    services.broadcast_to_room(game.code, result)
    messages.success(request, "¡La partida ha comenzado!")
    return redirect('quiz_games:game_host_play', code=game.code)


# Alias para compatibilidad
game_start = host_start_game


@login_required
@require_POST
def host_next_question(request, code):
    """
    Avanza a la siguiente pregunta del cuestionario o marca la partida
    como FINISHED si se completó la última pregunta.
    """
    game = get_object_or_404(Game, code=code)

    if not identity.is_host(game, request.user):
        raise PermissionDenied("Solo el anfitrión puede avanzar de pregunta.")

    result = services.advance_game(game)
    if not result['success']:
        return JsonResponse({'success': False, 'error': result['error']}, status=400)

    services.broadcast_to_room(game.code, result)

    if result['event'] == 'next_question':
        return JsonResponse({
            'success': True,
            'action': 'NEXT_QUESTION',
            'current_question': result['current_question'],
            'current_number': result['current_number'],
            'redirect_url': f"/games/host-play/{game.code}/"
        })
    return JsonResponse({
        'success': True,
        'action': 'GAME_FINISHED',
        'redirect_url': f"/games/leaderboard/{game.code}/"
    })


@login_required
@require_POST
def host_finish_game(request, code):
    """Finaliza manualmente la partida en cualquier momento."""
    game = get_object_or_404(Game, code=code)

    if not identity.is_host(game, request.user):
        raise PermissionDenied("Solo el anfitrión puede finalizar la partida.")

    # Una partida ya cerrada no se vuelve a finalizar: se conserva su fecha original de cierre
    if game.status == Game.Status.FINISHED:
        messages.info(request, "La partida ya estaba finalizada.")
        return redirect('quiz_games:game_leaderboard', code=game.code)
    if game.status == Game.Status.CANCELLED:
        messages.info(request, "La partida fue cancelada y no puede finalizarse.")
        return redirect('quizzes:quiz_detail', pk=game.quiz_id)

    result = services.finish_game(game)
    if result['success']:
        services.broadcast_to_room(game.code, result)

    messages.info(request, "Partida finalizada por el anfitrión.")
    return redirect('quiz_games:game_leaderboard', code=game.code)


@login_required
@require_POST
def host_cancel_game(request, code):
    """Cancela una sala en espera (LOBBY -> CANCELLED) y avisa a los alumnos."""
    game = get_object_or_404(Game, code=code)

    if not identity.is_host(game, request.user):
        raise PermissionDenied("Solo el anfitrión puede cancelar la sala.")

    result = services.cancel_game(game)
    if not result['success']:
        messages.warning(request, result['error'])
        return redirect('quiz_games:game_host_lobby', code=game.code)

    services.broadcast_to_room(game.code, result)
    messages.info(request, "Sala cancelada. Los estudiantes han sido avisados.")
    return redirect('quizzes:quiz_detail', pk=game.quiz_id)


@login_required
def game_host_play(request, code):
    """
    Pantalla proyectable del profesor durante la pregunta en curso.
    Muestra enunciado, temporizador, alternativas y conteo de respuestas enviadas.
    """
    game = get_object_or_404(Game.objects.select_related('quiz', 'host'), code=code)

    if not identity.is_host(game, request.user):
        raise PermissionDenied("Acceso exclusivo para el anfitrión de la sala.")

    if game.status != Game.Status.RUNNING:
        if game.status == Game.Status.LOBBY:
            return redirect('quiz_games:game_host_lobby', code=game.code)
        elif game.status == Game.Status.FINISHED:
            return redirect('quiz_games:game_leaderboard', code=game.code)

    questions = list(game.quiz.questions.prefetch_related('answers').order_by('order'))
    total_questions = len(questions)
    current_idx = game.current_question

    if current_idx < 0 or current_idx >= total_questions:
        messages.error(request, "Índice de pregunta fuera de rango.")
        return redirect('quiz_games:game_host_lobby', code=game.code)

    current_question = questions[current_idx]
    total_players = game.players.count()
    answered_count = PlayerAnswer.objects.filter(
        game_player__game=game,
        question=current_question
    ).count()
    top_players = ranked_players(game)[:5]

    context = {
        'game': game,
        'quiz': game.quiz,
        'question': current_question,
        'answers': current_question.answers.order_by('order'),
        'current_num': game.current_question + 1,
        'total_questions': total_questions,
        'total_players': total_players,
        'answered_count': answered_count,
        'top_players': top_players,
    }
    return render(request, 'quiz_games/game_host_play.html', context)


@login_required
def game_player_play(request, code):
    """
    Renderiza la interfaz de juego del participante para responder la pregunta activa.
    Las respuestas se presentan sin el campo is_correct y ordenadas aleatoriamente.
    """
    game = get_object_or_404(Game.objects.select_related('quiz'), code=code)
    player_entry = GamePlayer.objects.filter(game=game, player=request.user).first()

    if not player_entry:
        return redirect('quiz_games:game_join')

    if game.status == Game.Status.LOBBY:
        return redirect('quiz_games:game_player_lobby', code=game.code)
    elif game.status == Game.Status.FINISHED:
        return redirect('quiz_games:game_player_results', code=game.code)
    elif game.status == Game.Status.CANCELLED:
        return redirect('quiz_games:game_join')

    questions = list(game.quiz.questions.order_by('order'))
    current_idx = game.current_question

    if current_idx < 0 or current_idx >= len(questions):
        return redirect('quiz_games:game_player_lobby', code=game.code)

    current_question = questions[current_idx]

    # Comprobar si ya respondió la pregunta en curso
    existing_answer = PlayerAnswer.objects.filter(
        game_player=player_entry,
        question=current_question
    ).select_related('selected_answer').first()

    # Obtener alternativas excluyendo is_correct y barajando el orden de visualización
    raw_answers = current_question.answers.all().order_by('?')
    answers_data = [{'id': a.id, 'text': a.text} for a in raw_answers]

    ranking = list(ranked_players(game))
    player_position = None
    for idx, p in enumerate(ranking, 1):
        if p.id == player_entry.id:
            player_position = idx
            break

    context = {
        'game': game,
        'player_entry': player_entry,
        'question': current_question,
        'answers': answers_data,
        'has_answered': existing_answer is not None,
        'existing_answer': existing_answer,
        'current_num': game.current_question + 1,
        'total_questions': len(questions),
        'players': ranking,
        'player_position': player_position,
    }
    return render(request, 'quiz_games/game_player_play.html', context)


@login_required
@require_POST
def submit_player_answer(request, code):
    """
    Recibe la respuesta seleccionada por el estudiante vía AJAX, califica en el servidor
    con cálculo de tiempo oficial server-side (Anti-Cheat), calcula bonificación por tiempo
    y racha, y persiste la información de forma atómica.
    """
    game = get_object_or_404(Game, code=code)

    try:
        data = json.loads(request.body) if request.content_type == 'application/json' else request.POST
        question_id = int(data.get('question_id'))
        selected_answer_id = int(data.get('selected_answer_id'))
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Parámetros inválidos.'}, status=400)

    result = services.submit_answer(game, request.user, question_id, selected_answer_id)

    if result.get('code') == 'already_answered':
        return JsonResponse({'success': False, 'error': 'Ya has enviado tu respuesta para esta pregunta.'}, status=400)
    if 'error' in result:
        status = 403 if result['code'] == 'not_participant' else 400
        return JsonResponse({'success': False, 'error': result['error']}, status=status)

    return JsonResponse({
        'success': True,
        'is_correct': result['is_correct'],
        'points_awarded': result['points_awarded'],
        'total_score': result['total_score'],
        'streak': result['streak'],
    })


@login_required
def game_leaderboard(request, code):
    """
    Pantalla 1 de Cierre: Tabla de Posiciones Completa (solo lectura).
    Muestra los puntos de la partida; no entrega monedas ni XP.
    """
    game = get_object_or_404(Game.objects.select_related('quiz', 'host'), code=code)

    if game.status != Game.Status.FINISHED:
        if identity.is_host(game, request.user):
            return redirect('quiz_games:game_host_play', code=game.code)
        return redirect('quiz_games:game_player_play', code=game.code)

    is_host = (identity.is_host(game, request.user))
    is_participant = game.players.filter(player=request.user).exists()
    if not (is_host or is_participant):
        raise PermissionDenied("No tienes autorización para ver los resultados de esta sala.")

    # Esta pantalla es de SOLO LECTURA: no escribe nada en la base de datos.
    # Los premios (XP / Duki Coins) los decide Ecomotor, no este módulo.
    ranking = list(ranked_players(game))
    for gp in ranking:
        gp.best_streak = services.best_streak(gp)

    first_place = ranking[0] if len(ranking) > 0 else None
    second_place = ranking[1] if len(ranking) > 1 else None
    third_place = ranking[2] if len(ranking) > 2 else None
    rest_players = ranking[3:] if len(ranking) > 3 else []

    stats = game.players.aggregate(
        max_score=Max('score'),
        avg_correct=Avg('correct_answers'),
        total_participants=Count('id')
    )

    context = {
        'game': game,
        'quiz': game.quiz,
        'ranking': ranking,
        'first_place': first_place,
        'second_place': second_place,
        'third_place': third_place,
        'rest_players': rest_players,
        'max_score': stats['max_score'] or 0,
        'avg_correct': round(stats['avg_correct'] or 0, 1),
        'total_participants': stats['total_participants'] or 0,
        'total_questions': game.quiz.questions.count(),
        'is_host': is_host,
    }
    return render(request, 'quiz_games/game_leaderboard.html', context)


@login_required
def game_podium(request, code):
    """
    Pantalla 2 de Cierre: Ceremonia Visual Exclusiva del Podio Olímpico Top 3.
    """
    game = get_object_or_404(Game.objects.select_related('quiz', 'host'), code=code)

    if game.status != Game.Status.FINISHED:
        return redirect('quiz_games:game_leaderboard', code=game.code)

    is_host = (identity.is_host(game, request.user))
    is_participant = game.players.filter(player=request.user).exists()
    if not (is_host or is_participant):
        raise PermissionDenied("No tienes autorización para ver el podio de esta sala.")

    ranking = list(ranked_players(game))

    first_place = ranking[0] if len(ranking) > 0 else None
    second_place = ranking[1] if len(ranking) > 1 else None
    third_place = ranking[2] if len(ranking) > 2 else None

    context = {
        'game': game,
        'quiz': game.quiz,
        'first_place': first_place,
        'second_place': second_place,
        'third_place': third_place,
        'total_participants': len(ranking),
        'is_host': is_host,
    }
    return render(request, 'quiz_games/game_podium.html', context)


@login_required
def game_player_results(request, code):
    """
    Pantalla personal para el estudiante con su posición final, precisión y resumen de aciertos.
    """
    game = get_object_or_404(Game.objects.select_related('quiz'), code=code)

    if game.status != Game.Status.FINISHED:
        return redirect('quiz_games:game_player_play', code=game.code)

    player_entry = GamePlayer.objects.filter(game=game, player=request.user).first()
    if not player_entry:
        return redirect('quiz_games:game_join')

    # Determinar posición en el ranking general
    ranked_ids = [p.id for p in ranked_players(game)]
    position = (ranked_ids.index(player_entry.id) + 1) if player_entry.id in ranked_ids else None
    total_players = len(ranked_ids)

    # Precisión porcentual protegida contra división por cero
    total_questions = game.quiz.questions.count()
    if total_questions > 0:
        accuracy = round((player_entry.correct_answers / total_questions) * 100, 1)
    else:
        accuracy = 0.0

    # Determinar medalla o mención
    medal = None
    if position == 1:
        medal = {'tipo': 'ORO', 'icono': '🥇', 'clase': 'badge-gold'}
    elif position == 2:
        medal = {'tipo': 'PLATA', 'icono': '🥈', 'clase': 'badge-silver'}
    elif position == 3:
        medal = {'tipo': 'BRONCE', 'icono': '🥉', 'clase': 'badge-bronze'}

    context = {
        'game': game,
        'quiz': game.quiz,
        'player_entry': player_entry,
        'position': position,
        'total_players': total_players,
        'total_questions': total_questions,
        'accuracy': accuracy,
        'medal': medal,
        'best_streak': services.best_streak(player_entry),
        'incorrect_answers': max(total_questions - player_entry.correct_answers, 0),
    }
    return render(request, 'quiz_games/game_player_results.html', context)


@login_required
def game_question_status(request, code):
    """API JSON para actualizar en vivo el contador de respuestas recibidas con soporte 304 ETag."""
    game = get_object_or_404(Game, code=code)
    is_host = (identity.is_host(game, request.user))
    if not (is_host or game.players.filter(player=request.user).exists()):
        return HttpResponseForbidden("No perteneces a esta partida.")

    questions = list(game.quiz.questions.order_by('order'))
    current_idx = game.current_question

    answered_count = 0
    if 0 <= current_idx < len(questions):
        current_question = questions[current_idx]
        answered_count = PlayerAnswer.objects.filter(
            game_player__game=game,
            question=current_question
        ).count()

    total_players = game.players.count()
    etag = _generate_etag(game.status, game.current_question, answered_count, total_players)

    client_etag = request.headers.get('If-None-Match')
    if client_etag and client_etag == etag:
        return HttpResponseNotModified()

    response = JsonResponse({
        'status': game.status,
        'current_question': game.current_question,
        'total_players': total_players,
        'answered_count': answered_count,
    })
    response['ETag'] = etag
    return response


@login_required
def game_question_breakdown(request, code):
    """
    Retorna la distribución de votos por respuesta para la pregunta activa.
    Accesible únicamente para el anfitrión de la partida.
    """
    game = get_object_or_404(Game, code=code)
    if not identity.is_host(game, request.user):
        raise PermissionDenied("Solo el anfitrión puede consultar el desglose de respuestas.")

    questions = list(game.quiz.questions.prefetch_related('answers').order_by('order'))
    current_idx = game.current_question

    if current_idx < 0 or current_idx >= len(questions):
        return JsonResponse({'success': False, 'error': 'Pregunta fuera de rango.'}, status=400)

    current_q = questions[current_idx]

    # Contabilizar selecciones por cada alternativa de la pregunta
    answers_qs = current_q.answers.all().order_by('order')
    counts_map = dict(
        PlayerAnswer.objects.filter(
            game_player__game=game,
            question=current_q
        ).values('selected_answer').annotate(total=Count('id')).values_list('selected_answer', 'total')
    )

    data = []
    for ans in answers_qs:
        data.append({
            'answer_id': ans.id,
            'text': ans.text,
            'is_correct': ans.is_correct,
            'count': counts_map.get(ans.id, 0)
        })

    return JsonResponse({
        'success': True,
        'question_text': current_q.text,
        'breakdown': data,
        'total_answers': sum(counts_map.values())
    })


@login_required
def export_game_results_csv(request, code):
    """
    Genera y descarga un archivo CSV con las calificaciones, aciertos y métricas
    de todos los participantes de una partida concluida.
    """
    game = get_object_or_404(Game.objects.select_related('quiz'), code=code)
    if not identity.is_host(game, request.user):
        raise PermissionDenied("Solo el anfitrión puede exportar las calificaciones de esta sala.")

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="reporte_partida_{game.code}_{game.quiz.title[:15]}.csv"'

    # UTF-8 BOM para compatibilidad inmediata con Excel
    response.write('\ufeff'.encode('utf8'))

    writer = csv.writer(response)
    writer.writerow(['Posición', 'Usuario', 'Nombre Completo', 'Puntos Totales', 'Respuestas Correctas', 'Total Preguntas', 'Precisión (%)', 'Racha Final'])

    ranking = ranked_players(game)
    total_q = game.quiz.questions.count()

    for pos, player_entry in enumerate(ranking, start=1):
        user = player_entry.player
        full_name = f"{user.first_name} {user.last_name}".strip() or "Sin nombre"
        accuracy = round((player_entry.correct_answers / total_q) * 100, 1) if total_q > 0 else 0.0

        writer.writerow([
            pos,
            user.username,
            full_name,
            player_entry.score,
            player_entry.correct_answers,
            total_q,
            f"{accuracy}%",
            player_entry.current_streak
        ])

    return response


@login_required
def game_join(request):
    """Permite a cualquier usuario autenticado ingresar a una sala activa mediante su PIN."""
    if request.method == 'POST':
        form = JoinGameForm(request.POST)
        if form.is_valid():
            game = form.cleaned_data['game']

            if identity.is_host(game, request.user):
                messages.info(request, "Eres el anfitrión de esta sala. Te hemos redirigido a tu panel de control.")
                return redirect('quiz_games:game_host_lobby', code=game.code)

            with transaction.atomic():
                player_entry, created = GamePlayer.objects.get_or_create(
                    game=game,
                    player=request.user
                )

            if created:
                messages.success(request, f"¡Te has unido a la partida de {game.quiz.title}!")
            else:
                messages.info(request, "Ya estabas registrado en esta sala.")

            return redirect('quiz_games:game_player_lobby', code=game.code)
    else:
        form = JoinGameForm()

    return render(request, 'quiz_games/game_join.html', {'form': form})


@login_required
def game_player_lobby(request, code):
    """Pantalla de espera del participante aguardando a que el profesor inicie el juego."""
    game = get_object_or_404(Game.objects.select_related('quiz', 'host'), code=code)

    player_entry = GamePlayer.objects.filter(game=game, player=request.user).first()
    if not player_entry:
        messages.error(request, "Debes ingresar el PIN para unirte a esta sala antes de acceder.")
        return redirect('quiz_games:game_join')

    if game.status == Game.Status.RUNNING:
        return redirect('quiz_games:game_player_play', code=game.code)
    elif game.status == Game.Status.CANCELLED:
        messages.warning(request, "El docente canceló la sala.")
        return redirect('quiz_games:game_join')
    elif game.status == Game.Status.FINISHED:
        messages.warning(request, "La partida ya no se encuentra activa.")
        return redirect('quiz_games:game_join')

    context = {
        'game': game,
        'quiz': game.quiz,
        'player_entry': player_entry,
    }
    return render(request, 'quiz_games/game_player_lobby.html', context)


@login_required
def game_player_resume(request):
    """
    Si request.user tiene una instancia activa de GamePlayer en una partida con
    status=Game.Status.RUNNING, redirígelo automáticamente a game_player_play
    manteniendo su score y streak intactos.
    Si no tiene partidas activas, redirige a game_join.
    """
    active_player = GamePlayer.objects.filter(
        player=request.user,
        game__status=Game.Status.RUNNING
    ).select_related('game').order_by('-game__started_at').first()

    if active_player:
        return redirect('quiz_games:game_player_play', code=active_player.game.code)

    return redirect('quiz_games:game_join')
