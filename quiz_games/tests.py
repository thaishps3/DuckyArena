from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.urls import reverse
from django.utils import timezone
from django.db import IntegrityError
from django.db.models import ProtectedError
from quizzes.models import Quiz, Question, Answer
from quiz_games.models import Game, GamePlayer, PlayerAnswer
from quiz_games.consumers import GameRoomConsumer
from quiz_games.forms import JoinGameForm
from quiz_games.services import generate_unique_game_code
from quiz_games import services

User = get_user_model()

_USER_PROFILES = {}


class DummyProfile:
    def __init__(self, role='TEACHER', ducky_coins=500):
        self.role = role
        self.ducky_coins = ducky_coins


if not hasattr(User, 'profile'):
    User.profile = property(lambda self: _USER_PROFILES.get(self.username, DummyProfile(role='TEACHER', ducky_coins=500)))


def attach_profile(user, role='TEACHER', ducky_coins=500):
    profile = DummyProfile(role=role, ducky_coins=ducky_coins)
    _USER_PROFILES[user.username] = profile
    return user


class GameUtilsTest(TestCase):
    def test_generate_unique_game_code_format_and_length(self):
        code = generate_unique_game_code()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())


class JoinGameFormTest(TestCase):
    def setUp(self):
        self.host = User.objects.create_user(username='form_host', password='password123')
        attach_profile(self.host, role='TEACHER')
        self.quiz = Quiz.objects.create(
            title='Quiz Form Test',
            creator=self.host,
            is_published=True
        )
        self.game = Game.objects.create(
            quiz=self.quiz,
            host=self.host,
            code='654321',
            status=Game.Status.LOBBY
        )

    def test_valid_pin_is_accepted(self):
        form = JoinGameForm(data={'code': '654321'})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['game'], self.game)

    def test_invalid_length_pin(self):
        form = JoinGameForm(data={'code': '12345'})
        self.assertFalse(form.is_valid())
        self.assertIn('code', form.errors)

    def test_non_digit_pin(self):
        form = JoinGameForm(data={'code': 'ABC123'})
        self.assertFalse(form.is_valid())
        self.assertIn('code', form.errors)

    def test_non_existent_pin(self):
        form = JoinGameForm(data={'code': '999999'})
        self.assertFalse(form.is_valid())
        self.assertIn('No existe ninguna sala', str(form.errors['code']))

    def test_running_game_pin_rejected(self):
        self.game.status = Game.Status.RUNNING
        self.game.save()
        form = JoinGameForm(data={'code': '654321'})
        self.assertFalse(form.is_valid())
        self.assertIn('ya ha comenzado', str(form.errors['code']))

    def test_finished_game_pin_rejected(self):
        self.game.status = Game.Status.FINISHED
        self.game.save()
        form = JoinGameForm(data={'code': '654321'})
        self.assertFalse(form.is_valid())
        self.assertIn('ya ha finalizado', str(form.errors['code']))


class GameViewsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.teacher = User.objects.create_user(username='teacher_host', password='password123')
        attach_profile(self.teacher, role='TEACHER')

        self.student = User.objects.create_user(username='student_guest', password='password123')
        attach_profile(self.student, role='STUDENT')

        self.student2 = User.objects.create_user(username='student_two', password='password123')
        attach_profile(self.student2, role='STUDENT')

        self.other_teacher = User.objects.create_user(username='other_teacher', password='password123')
        attach_profile(self.other_teacher, role='TEACHER')

        self.unrelated_user = User.objects.create_user(username='unrelated_user', password='password123')
        attach_profile(self.unrelated_user, role='STUDENT')

        self.quiz = Quiz.objects.create(
            title='Python Arena Master',
            description='Test Arena Quiz',
            creator=self.teacher,
            is_published=True
        )

        self.question1 = Question.objects.create(
            quiz=self.quiz,
            text='¿Qué es un decorador en Python?',
            time_limit=30,
            points=1000,
            order=1
        )
        self.ans1_1 = Answer.objects.create(question=self.question1, text='Una función que envuelve a otra', is_correct=True, order=1)
        self.ans1_2 = Answer.objects.create(question=self.question1, text='Una variable global', is_correct=False, order=2)

        self.question2 = Question.objects.create(
            quiz=self.quiz,
            text='¿Cuál es la función para obtener longitud?',
            time_limit=20,
            points=500,
            order=2
        )
        self.ans2_1 = Answer.objects.create(question=self.question2, text='len()', is_correct=True, order=1)
        self.ans2_2 = Answer.objects.create(question=self.question2, text='size()', is_correct=False, order=2)

    def test_game_create_requires_login(self):
        url = reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_game_create_denies_student(self):
        self.client.login(username='student_guest', password='password123')
        url = reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_game_create_denies_non_creator_teacher(self):
        self.client.login(username='other_teacher', password='password123')
        url = reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_game_create_denies_staff_who_is_not_creator(self):
        # Un docente con is_staff (como en el panel de admin) tampoco puede usar el quiz de otro
        self.other_teacher.is_staff = True
        self.other_teacher.save()
        self.client.login(username='other_teacher', password='password123')
        url = reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Game.objects.filter(quiz=self.quiz).exists())

    def test_game_create_allows_superuser_like_the_quiz_page_button(self):
        # La ficha del quiz muestra el botón al creador y al superusuario (administrador de la plataforma)
        admin = User.objects.create_superuser(username='admin_plataforma', password='password123')
        self.client.login(username='admin_plataforma', password='password123')
        url = reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk})
        response = self.client.post(url)
        game = Game.objects.get(quiz=self.quiz)
        self.assertRedirects(response, reverse('quiz_games:game_host_lobby', kwargs={'code': game.code}))
        self.assertEqual(game.host, admin)

    def test_game_create_allows_creator(self):
        self.client.login(username='teacher_host', password='password123')
        url = reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk})
        response = self.client.post(url)
        game = Game.objects.get(quiz=self.quiz)
        self.assertRedirects(response, reverse('quiz_games:game_host_lobby', kwargs={'code': game.code}))
        self.assertEqual(game.host, self.teacher)
        self.assertEqual(game.status, Game.Status.LOBBY)

    def test_game_create_draft_quiz_redirects_with_error(self):
        self.quiz.is_published = False
        self.quiz.save()
        self.client.login(username='teacher_host', password='password123')
        url = reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk})
        response = self.client.post(url, follow=True)
        self.assertRedirects(response, reverse('quizzes:quiz_detail', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(Game.objects.count(), 0)

    def test_game_create_no_questions_redirects_with_error(self):
        empty_quiz = Quiz.objects.create(
            title='Cuestionario Vacío',
            creator=self.teacher,
            is_published=True
        )
        self.client.login(username='teacher_host', password='password123')
        url = reverse('quiz_games:game_create', kwargs={'quiz_pk': empty_quiz.pk})
        response = self.client.post(url, follow=True)
        self.assertRedirects(response, reverse('quizzes:quiz_detail', kwargs={'pk': empty_quiz.pk}))
        self.assertEqual(Game.objects.count(), 0)

    def test_game_create_success(self):
        self.client.login(username='teacher_host', password='password123')
        url = reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk})
        response = self.client.post(url)
        self.assertEqual(Game.objects.count(), 1)

        game = Game.objects.first()
        self.assertEqual(game.quiz, self.quiz)
        self.assertEqual(game.host, self.teacher)
        self.assertEqual(game.status, Game.Status.LOBBY)
        self.assertEqual(game.current_question, 0)
        self.assertEqual(len(game.code), 6)
        self.assertRedirects(response, reverse('quiz_games:game_host_lobby', kwargs={'code': game.code}))

    def test_game_host_lobby_access_control(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='123456',
            status=Game.Status.LOBBY
        )
        url = reverse('quiz_games:game_host_lobby', kwargs={'code': game.code})

        # Otro usuario no tiene permiso
        self.client.login(username='other_teacher', password='password123')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

        # El profesor anfitrión sí tiene acceso
        self.client.login(username='teacher_host', password='password123')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'quiz_games/game_host_lobby.html')
        self.assertEqual(response.context['game'], game)
        self.assertEqual(response.context['players_count'], 0)

    def test_game_join_view_get(self):
        self.client.login(username='student_guest', password='password123')
        response = self.client.get(reverse('quiz_games:game_join'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'quiz_games/game_join.html')

    def test_game_join_as_host_redirects_to_host_lobby(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.LOBBY
        )
        self.client.login(username='teacher_host', password='password123')
        response = self.client.post(reverse('quiz_games:game_join'), {'code': '112233'})
        self.assertRedirects(response, reverse('quiz_games:game_host_lobby', kwargs={'code': '112233'}))
        self.assertEqual(GamePlayer.objects.filter(game=game).count(), 0)

    def test_game_join_as_student_registers_player(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.LOBBY
        )
        self.client.login(username='student_guest', password='password123')
        response = self.client.post(reverse('quiz_games:game_join'), {'code': '112233'})
        self.assertRedirects(response, reverse('quiz_games:game_player_lobby', kwargs={'code': '112233'}))
        self.assertEqual(GamePlayer.objects.filter(game=game, player=self.student).count(), 1)

        # Ingresar de nuevo no duplica (idempotencia get_or_create)
        response2 = self.client.post(reverse('quiz_games:game_join'), {'code': '112233'})
        self.assertRedirects(response2, reverse('quiz_games:game_player_lobby', kwargs={'code': '112233'}))
        self.assertEqual(GamePlayer.objects.filter(game=game, player=self.student).count(), 1)

    def test_game_player_lobby_unregistered_redirects(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.LOBBY
        )
        self.client.login(username='student_guest', password='password123')
        response = self.client.get(reverse('quiz_games:game_player_lobby', kwargs={'code': '112233'}))
        self.assertRedirects(response, reverse('quiz_games:game_join'))

    def test_game_player_lobby_registered_access(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.LOBBY
        )
        GamePlayer.objects.create(game=game, player=self.student)
        self.client.login(username='student_guest', password='password123')
        response = self.client.get(reverse('quiz_games:game_player_lobby', kwargs={'code': '112233'}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'quiz_games/game_player_lobby.html')

    def test_game_lobby_status_forbidden_for_unrelated_user(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.LOBBY
        )
        self.client.login(username='unrelated_user', password='password123')
        response = self.client.get(reverse('quiz_games:game_lobby_status', kwargs={'code': '112233'}))
        self.assertEqual(response.status_code, 403)

    def test_game_lobby_status_success_for_host_and_player(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.LOBBY
        )
        gp = GamePlayer.objects.create(game=game, player=self.student)

        # Host accede a la API de estado
        self.client.login(username='teacher_host', password='password123')
        response_host = self.client.get(reverse('quiz_games:game_lobby_status', kwargs={'code': '112233'}))
        self.assertEqual(response_host.status_code, 200)
        data_host = response_host.json()
        self.assertEqual(data_host['status'], 'LOBBY')
        self.assertEqual(data_host['player_count'], 1)
        self.assertEqual(data_host['players'][0]['username'], self.student.username)

        # Jugador registrado accede a la API de estado
        self.client.login(username='student_guest', password='password123')
        response_player = self.client.get(reverse('quiz_games:game_lobby_status', kwargs={'code': '112233'}))
        self.assertEqual(response_player.status_code, 200)
        data_player = response_player.json()
        self.assertEqual(data_player['player_count'], 1)

    def test_host_start_game_denies_non_host(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.LOBBY
        )
        GamePlayer.objects.create(game=game, player=self.student)
        self.client.login(username='other_teacher', password='password123')
        response = self.client.post(reverse('quiz_games:host_start_game', kwargs={'code': '112233'}))
        self.assertEqual(response.status_code, 403)

    def test_host_start_game_requires_at_least_one_player(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.LOBBY
        )
        self.client.login(username='teacher_host', password='password123')
        response = self.client.post(reverse('quiz_games:host_start_game', kwargs={'code': '112233'}), follow=True)
        self.assertRedirects(response, reverse('quiz_games:game_host_lobby', kwargs={'code': '112233'}))
        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.LOBBY)

    def test_host_start_game_success_transitions_to_running(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.LOBBY,
            current_question=0
        )
        GamePlayer.objects.create(game=game, player=self.student)
        self.client.login(username='teacher_host', password='password123')
        response = self.client.post(reverse('quiz_games:host_start_game', kwargs={'code': '112233'}))
        self.assertRedirects(response, reverse('quiz_games:game_host_play', kwargs={'code': '112233'}))

        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.RUNNING)
        self.assertEqual(game.current_question, 0)
        self.assertIsNotNone(game.started_at)

    def test_game_host_play_view(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student)

        # Acceso denegado a no anfitrión
        self.client.login(username='other_teacher', password='password123')
        response = self.client.get(reverse('quiz_games:game_host_play', kwargs={'code': '112233'}))
        self.assertEqual(response.status_code, 403)

        # Acceso concedido al anfitrión
        self.client.login(username='teacher_host', password='password123')
        response = self.client.get(reverse('quiz_games:game_host_play', kwargs={'code': '112233'}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'quiz_games/game_host_play.html')
        self.assertEqual(response.context['question'], self.question1)
        self.assertEqual(response.context['current_num'], 1)
        self.assertEqual(response.context['total_players'], 1)
        self.assertEqual(response.context['answered_count'], 0)

    def test_game_player_play_view_and_answered_state(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student)

        # Jugador no registrado es redirigido
        self.client.login(username='unrelated_user', password='password123')
        response_unreg = self.client.get(reverse('quiz_games:game_player_play', kwargs={'code': '112233'}))
        self.assertRedirects(response_unreg, reverse('quiz_games:game_join'))

        # Jugador registrado puede ver la pantalla de respuesta
        self.client.login(username='student_guest', password='password123')
        response = self.client.get(reverse('quiz_games:game_player_play', kwargs={'code': '112233'}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'quiz_games/game_player_play.html')
        self.assertFalse(response.context['has_answered'])

        # Si el jugador ya respondió, has_answered es True
        PlayerAnswer.objects.create(
            game_player=gp,
            question=self.question1,
            selected_answer=self.ans1_1,
            is_correct=True,
            points_awarded=1000
        )
        response2 = self.client.get(reverse('quiz_games:game_player_play', kwargs={'code': '112233'}))
        self.assertEqual(response2.status_code, 200)
        self.assertTrue(response2.context['has_answered'])

    def test_game_question_status_endpoint(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student)
        PlayerAnswer.objects.create(
            game_player=gp,
            question=self.question1,
            selected_answer=self.ans1_1,
            is_correct=True,
            points_awarded=1000
        )

        self.client.login(username='teacher_host', password='password123')
        response = self.client.get(reverse('quiz_games:game_question_status', kwargs={'code': '112233'}))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'RUNNING')
        self.assertEqual(data['current_question'], 0)
        self.assertEqual(data['total_players'], 1)
        self.assertEqual(data['answered_count'], 1)

    def test_submit_player_answer_correct(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student, score=0, current_streak=0)

        self.client.login(username='student_guest', password='password123')
        payload = {
            'question_id': self.question1.id,
            'selected_answer_id': self.ans1_1.id,
            'time_taken': 5.0
        }
        response = self.client.post(
            reverse('quiz_games:submit_player_answer', kwargs={'code': '112233'}),
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_correct'])
        self.assertGreater(data['points_awarded'], 0)
        self.assertEqual(data['streak'], 1)

        gp.refresh_from_db()
        self.assertEqual(gp.current_streak, 1)
        self.assertEqual(gp.correct_answers, 1)
        self.assertGreater(gp.score, 0)
        self.assertEqual(PlayerAnswer.objects.filter(game_player=gp, question=self.question1).count(), 1)

    def test_submit_player_answer_incorrect(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student, score=500, current_streak=3)

        self.client.login(username='student_guest', password='password123')
        payload = {
            'question_id': self.question1.id,
            'selected_answer_id': self.ans1_2.id,
            'time_taken': 2.0
        }
        response = self.client.post(
            reverse('quiz_games:submit_player_answer', kwargs={'code': '112233'}),
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['is_correct'])
        self.assertEqual(data['points_awarded'], 0)
        self.assertEqual(data['streak'], 0)

        gp.refresh_from_db()
        self.assertEqual(gp.current_streak, 0)
        self.assertEqual(gp.score, 500)

    def test_submit_player_answer_duplicate_rejected(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student)
        PlayerAnswer.objects.create(
            game_player=gp,
            question=self.question1,
            selected_answer=self.ans1_1,
            is_correct=True,
            points_awarded=1000
        )

        self.client.login(username='student_guest', password='password123')
        payload = {
            'question_id': self.question1.id,
            'selected_answer_id': self.ans1_1.id,
            'time_taken': 3.0
        }
        response = self.client.post(
            reverse('quiz_games:submit_player_answer', kwargs={'code': '112233'}),
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('Ya has enviado tu respuesta', data['error'])

    def test_student_cannot_advance_or_finish_game(self):
        """TDA Equipo 2, tests de seguridad: un alumno no puede avanzar ni finalizar escribiendo la URL."""
        game = Game.objects.create(
            quiz=self.quiz, host=self.teacher, code='667788',
            status=Game.Status.RUNNING, current_question=0
        )
        GamePlayer.objects.create(game=game, player=self.student)
        self.client.login(username='student_guest', password='password123')
        for name in ('host_next_question', 'host_finish_game'):
            resp = self.client.post(reverse(f'quiz_games:{name}', kwargs={'code': '667788'}))
            self.assertEqual(resp.status_code, 403)
        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.RUNNING)
        self.assertEqual(game.current_question, 0)

    def test_host_next_question_advances_and_finishes(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.RUNNING,
            current_question=0
        )
        GamePlayer.objects.create(game=game, player=self.student)

        # 1. Avanzar de pregunta 1 a pregunta 2 (quiz tiene 2 preguntas)
        self.client.login(username='teacher_host', password='password123')
        response1 = self.client.post(reverse('quiz_games:host_next_question', kwargs={'code': '112233'}))
        self.assertEqual(response1.status_code, 200)
        data1 = response1.json()
        self.assertEqual(data1['action'], 'NEXT_QUESTION')
        self.assertEqual(data1['current_question'], 1)

        game.refresh_from_db()
        self.assertEqual(game.current_question, 1)
        self.assertEqual(game.status, Game.Status.RUNNING)

        # 2. Avanzar de pregunta 2 -> concluye la partida (GAME_FINISHED)
        response2 = self.client.post(reverse('quiz_games:host_next_question', kwargs={'code': '112233'}))
        self.assertEqual(response2.status_code, 200)
        data2 = response2.json()
        self.assertEqual(data2['action'], 'GAME_FINISHED')
        self.assertIn('/leaderboard/', data2['redirect_url'])

        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.FINISHED)
        self.assertIsNotNone(game.finished_at)

    def test_host_finish_game_manual(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.RUNNING,
            current_question=0
        )
        self.client.login(username='teacher_host', password='password123')
        response = self.client.post(reverse('quiz_games:host_finish_game', kwargs={'code': '112233'}))
        self.assertRedirects(response, reverse('quiz_games:game_leaderboard', kwargs={'code': '112233'}))

        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.FINISHED)
        self.assertIsNotNone(game.finished_at)

    def test_game_leaderboard_access_and_ranking(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.FINISHED
        )
        gp1 = GamePlayer.objects.create(game=game, player=self.student, score=1500, correct_answers=2)
        gp2 = GamePlayer.objects.create(game=game, player=self.student2, score=800, correct_answers=1)

        # Acceso denegado a usuario no involucrado
        self.client.login(username='unrelated_user', password='password123')
        response_unrel = self.client.get(reverse('quiz_games:game_leaderboard', kwargs={'code': '112233'}))
        self.assertEqual(response_unrel.status_code, 403)

        # Anfitrión accede al podio
        self.client.login(username='teacher_host', password='password123')
        response_host = self.client.get(reverse('quiz_games:game_leaderboard', kwargs={'code': '112233'}))
        self.assertEqual(response_host.status_code, 200)
        self.assertTemplateUsed(response_host, 'quiz_games/game_leaderboard.html')
        self.assertEqual(list(response_host.context['ranking']), [gp1, gp2])

        # Jugador accede al podio
        self.client.login(username='student_guest', password='password123')
        response_player = self.client.get(reverse('quiz_games:game_leaderboard', kwargs={'code': '112233'}))
        self.assertEqual(response_player.status_code, 200)

    def test_game_leaderboard_is_read_only(self):
        """Ver el marcador no debe escribir nada: ni abonar monedas/XP ni marcar liquidaciones."""
        game = Game.objects.create(
            quiz=self.quiz, host=self.teacher, code='445566', status=Game.Status.FINISHED
        )
        gp = GamePlayer.objects.create(game=game, player=self.student, score=1500, correct_answers=2)
        profile = self.student.profile
        coins_before = profile.ducky_coins

        url = reverse('quiz_games:game_leaderboard', kwargs={'code': '445566'})
        for username in ('teacher_host', 'student_guest', 'student_guest'):
            self.client.login(username=username, password='password123')
            self.assertEqual(self.client.get(url).status_code, 200)

        gp.refresh_from_db()
        self.assertFalse(gp.is_settled)
        self.assertEqual(gp.score, 1500)
        self.assertEqual(self.student.profile.ducky_coins, coins_before)

    def test_game_leaderboard_racha_max_is_best_not_final(self):
        """Racha Máx. del marcador es la mejor racha (bien, bien, mal => 2), no la racha final (0)."""
        game = Game.objects.create(
            quiz=self.quiz, host=self.teacher, code='121314', status=Game.Status.FINISHED
        )
        gp = GamePlayer.objects.create(game=game, player=self.student, score=2000,
                                       correct_answers=2, current_streak=0)
        PlayerAnswer.objects.create(game_player=gp, question=self.question1,
                                    selected_answer=self.ans1_1, is_correct=True, points_awarded=1000)
        PlayerAnswer.objects.create(game_player=gp, question=self.question2,
                                    selected_answer=self.ans2_1, is_correct=True, points_awarded=1000)
        self.client.login(username='teacher_host', password='password123')
        response = self.client.get(reverse('quiz_games:game_leaderboard', kwargs={'code': '121314'}))
        self.assertEqual(response.context['ranking'][0].best_streak, 2)

    def test_game_leaderboard_has_no_currency_wording(self):
        game = Game.objects.create(
            quiz=self.quiz, host=self.teacher, code='778899', status=Game.Status.FINISHED
        )
        GamePlayer.objects.create(game=game, player=self.student, score=900, correct_answers=1)
        self.client.login(username='student_guest', password='password123')
        html = self.client.get(reverse('quiz_games:game_leaderboard', kwargs={'code': '778899'})).content.decode()
        self.assertNotIn('DuckyCoins', html)
        self.assertNotIn('Saldo', html)
        self.assertIn('pts', html)

    def test_game_player_results_view(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.FINISHED
        )
        gp = GamePlayer.objects.create(game=game, player=self.student, score=1200, correct_answers=1)
        PlayerAnswer.objects.create(
            game_player=gp,
            question=self.question1,
            selected_answer=self.ans1_1,
            is_correct=True,
            points_awarded=1200
        )

        # Estudiante registrado accede a sus resultados
        self.client.login(username='student_guest', password='password123')
        response = self.client.get(reverse('quiz_games:game_player_results', kwargs={'code': '112233'}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'quiz_games/game_player_results.html')
        self.assertEqual(response.context['position'], 1)
        self.assertEqual(response.context['total_players'], 1)
        self.assertEqual(response.context['accuracy'], 50.0)
        self.assertEqual(response.context['medal']['tipo'], 'ORO')
        # TDA Equipo 2, tarea 28: el resultado individual incluye correctas e incorrectas
        self.assertEqual(response.context['incorrect_answers'], 1)
        self.assertContains(response, 'Incorrectas: 1')

    def test_game_leaderboard_redirect_if_not_finished(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='112233',
            status=Game.Status.RUNNING,
            current_question=0
        )
        GamePlayer.objects.create(game=game, player=self.student)

        # Host se redirige a game_host_play
        self.client.login(username='teacher_host', password='password123')
        resp_host = self.client.get(reverse('quiz_games:game_leaderboard', kwargs={'code': '112233'}))
        self.assertRedirects(resp_host, reverse('quiz_games:game_host_play', kwargs={'code': '112233'}))

        # Estudiante se redirige a game_player_play
        self.client.login(username='student_guest', password='password123')
        resp_stud = self.client.get(reverse('quiz_games:game_leaderboard', kwargs={'code': '112233'}))
        self.assertRedirects(resp_stud, reverse('quiz_games:game_player_play', kwargs={'code': '112233'}))

    def test_game_player_results_zero_questions_accuracy(self):
        empty_quiz = Quiz.objects.create(
            title='Quiz Sin Preguntas',
            creator=self.teacher,
            is_published=True
        )
        game = Game.objects.create(
            quiz=empty_quiz,
            host=self.teacher,
            code='998877',
            status=Game.Status.FINISHED
        )
        gp = GamePlayer.objects.create(game=game, player=self.student, score=0, correct_answers=0)

        self.client.login(username='student_guest', password='password123')
        response = self.client.get(reverse('quiz_games:game_player_results', kwargs={'code': '998877'}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['accuracy'], 0.0)


class QuizGamesE2ETestCase(TestCase):
    """
    Suite E2E Integral que valida el 100% de los contratos de ducky_arena_master_spec.pdf
    y PROJECT_RULES.md: ciclo de vida, permisos, zero leaks, restricciones de unicidad
    y protección relacional con models.PROTECT.
    """
    def setUp(self):
        self.client = Client()
        self.teacher = User.objects.create_user(username='e2e_teacher', password='password123')
        attach_profile(self.teacher, role='TEACHER', ducky_coins=1000)

        self.student = User.objects.create_user(username='e2e_student', password='password123')
        attach_profile(self.student, role='STUDENT', ducky_coins=200)

        self.student2 = User.objects.create_user(username='e2e_student2', password='password123')
        attach_profile(self.student2, role='STUDENT', ducky_coins=150)

        # Quiz publicado con 2 preguntas y 4 opciones cada una (exactamente 1 correcta)
        self.quiz = Quiz.objects.create(
            title='E2E Python Master Arena',
            description='Certificación integral E2E',
            creator=self.teacher,
            is_published=True
        )

        # Pregunta 1
        self.q1 = Question.objects.create(
            quiz=self.quiz,
            text='¿Qué estructura es inmutable en Python?',
            time_limit=30,
            points=1000,
            order=1
        )
        self.q1_ans1 = Answer.objects.create(question=self.q1, text='tuple', is_correct=True, order=1)
        self.q1_ans2 = Answer.objects.create(question=self.q1, text='list', is_correct=False, order=2)
        self.q1_ans3 = Answer.objects.create(question=self.q1, text='dict', is_correct=False, order=3)
        self.q1_ans4 = Answer.objects.create(question=self.q1, text='set', is_correct=False, order=4)

        # Pregunta 2
        self.q2 = Question.objects.create(
            quiz=self.quiz,
            text='¿Qué operador se utiliza para exponenciación?',
            time_limit=20,
            points=800,
            order=2
        )
        self.q2_ans1 = Answer.objects.create(question=self.q2, text='**', is_correct=True, order=1)
        self.q2_ans2 = Answer.objects.create(question=self.q2, text='^', is_correct=False, order=2)
        self.q2_ans3 = Answer.objects.create(question=self.q2, text='exp()', is_correct=False, order=3)
        self.q2_ans4 = Answer.objects.create(question=self.q2, text='//', is_correct=False, order=4)

    def test_quiz_lifecycle_and_permissions(self):
        """Verifica que solo el TEACHER creador puede gestionar el Quiz y los STUDENT reciben 403."""
        # Profesor dueño puede ver detalle
        self.client.login(username='e2e_teacher', password='password123')
        response_owner = self.client.get(reverse('quizzes:quiz_detail', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(response_owner.status_code, 200)

        # Estudiante intenta crear sala -> 403 PermissionDenied
        self.client.login(username='e2e_student', password='password123')
        response_student = self.client.post(reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk}))
        self.assertEqual(response_student.status_code, 403)

    def test_game_creation_and_unique_pin(self):
        """Verifica generación de Game en estado LOBBY con PIN numérico de 6 dígitos único."""
        self.client.login(username='e2e_teacher', password='password123')
        response = self.client.post(reverse('quiz_games:game_create', kwargs={'quiz_pk': self.quiz.pk}))
        self.assertEqual(response.status_code, 302)

        game = Game.objects.filter(quiz=self.quiz).first()
        self.assertIsNotNone(game)
        self.assertEqual(game.status, Game.Status.LOBBY)
        self.assertEqual(game.current_question, 0)
        self.assertEqual(len(game.code), 6)
        self.assertTrue(game.code.isdigit())

    def test_unique_player_constraint(self):
        """Verifica registro de jugador y que intentos duplicados respeten unique_player_per_quiz_game."""
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='123987',
            status=Game.Status.LOBBY
        )
        # Primer registro exitoso
        gp1 = GamePlayer.objects.create(game=game, player=self.student)
        self.assertIsNotNone(gp1.pk)

        # Intento de duplicado directo en BD viola UniqueConstraint
        with self.assertRaises(IntegrityError):
            GamePlayer.objects.create(game=game, player=self.student)

    def test_zero_leak_and_answer_evaluation(self):
        """
        Verifica Cero Leaks de is_correct en pantalla del estudiante y evaluación server-side
        con cálculo de puntaje, racha y persistencia atómica.
        """
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='554433',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student, score=0, current_streak=0)

        # 1. Cero Leaks: la vista del estudiante no debe filtrar 'is_correct'
        self.client.login(username='e2e_student', password='password123')
        play_response = self.client.get(reverse('quiz_games:game_player_play', kwargs={'code': '554433'}))
        self.assertEqual(play_response.status_code, 200)

        # Contexto HTML no contiene clave is_correct
        if 'answers' in play_response.context:
            for ans in play_response.context['answers']:
                self.assertNotIn('is_correct', ans)

        # 2. Evaluación de respuesta server-side
        payload = {
            'question_id': self.q1.id,
            'selected_answer_id': self.q1_ans1.id,
            'time_taken': 6.0
        }
        submit_response = self.client.post(
            reverse('quiz_games:submit_player_answer', kwargs={'code': '554433'}),
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(submit_response.status_code, 200)
        data = submit_response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_correct'])
        self.assertGreater(data['points_awarded'], 0)
        self.assertEqual(data['streak'], 1)

        gp.refresh_from_db()
        self.assertEqual(gp.score, data['total_score'])
        self.assertEqual(gp.correct_answers, 1)
        self.assertEqual(gp.current_streak, 1)
        self.assertEqual(PlayerAnswer.objects.filter(game_player=gp, question=self.q1).count(), 1)

    def test_unique_answer_constraint(self):
        """Verifica que un segundo intento de respuesta lance error y respete unique_answer_per_player_question."""
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='776655',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student)
        PlayerAnswer.objects.create(
            game_player=gp,
            question=self.q1,
            selected_answer=self.q1_ans1,
            is_correct=True,
            points_awarded=1000
        )

        # Endpoint rechaza con 400
        self.client.login(username='e2e_student', password='password123')
        payload = {
            'question_id': self.q1.id,
            'selected_answer_id': self.q1_ans2.id,
            'time_taken': 3.0
        }
        response = self.client.post(
            reverse('quiz_games:submit_player_answer', kwargs={'code': '776655'}),
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('Ya has enviado tu respuesta', response.json()['error'])

        # Intento de inserción directa en BD viola UniqueConstraint
        with self.assertRaises(IntegrityError):
            PlayerAnswer.objects.create(
                game_player=gp,
                question=self.q1,
                selected_answer=self.q1_ans2,
                is_correct=False,
                points_awarded=0
            )

    def test_game_finish_and_protect_constraint(self):
        """
        Verifica transición a FINISHED, consistencia de rankings y bloqueo de borrado
        en cascada del Quiz mediante models.PROTECT.
        """
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='990011',
            status=Game.Status.RUNNING,
            current_question=1
        )
        gp1 = GamePlayer.objects.create(game=game, player=self.student, score=1800, correct_answers=2)
        gp2 = GamePlayer.objects.create(game=game, player=self.student2, score=900, correct_answers=1)

        # Finalizar partida
        self.client.login(username='e2e_teacher', password='password123')
        response_finish = self.client.post(reverse('quiz_games:host_finish_game', kwargs={'code': '990011'}))
        self.assertRedirects(response_finish, reverse('quiz_games:game_leaderboard', kwargs={'code': '990011'}))

        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.FINISHED)
        self.assertIsNotNone(game.finished_at)

        # Consultar podio
        response_lb = self.client.get(reverse('quiz_games:game_leaderboard', kwargs={'code': '990011'}))
        self.assertEqual(response_lb.status_code, 200)
        self.assertEqual(response_lb.context['first_place'], gp1)
        self.assertEqual(response_lb.context['second_place'], gp2)

        # Consultar podio olímpico desacoplado
        response_podium = self.client.get(reverse('quiz_games:game_podium', kwargs={'code': '990011'}))
        self.assertEqual(response_podium.status_code, 200)
        self.assertEqual(response_podium.context['first_place'], gp1)
        self.assertEqual(response_podium.context['second_place'], gp2)

        # Protección relacional: borrar el Quiz debe ser bloqueado por models.PROTECT
        with self.assertRaises(ProtectedError):
            self.quiz.delete()


class AntiCheatAndModerationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.teacher = User.objects.create_user(username='ac_teacher', password='password123')
        attach_profile(self.teacher, role='TEACHER')

        self.student = User.objects.create_user(username='ac_student', password='password123')
        attach_profile(self.student, role='STUDENT')

        self.student2 = User.objects.create_user(username='ac_student2', password='password123')
        attach_profile(self.student2, role='STUDENT')

        self.quiz = Quiz.objects.create(
            title='AntiCheat Quiz',
            creator=self.teacher,
            is_published=True
        )
        self.q1 = Question.objects.create(
            quiz=self.quiz,
            text='Pregunta de prueba',
            time_limit=30,
            points=1000,
            order=1
        )
        self.q1_ans1 = Answer.objects.create(question=self.q1, text='Opción Correcta', is_correct=True, order=1)
        self.q1_ans2 = Answer.objects.create(question=self.q1, text='Opción Incorrecta', is_correct=False, order=2)

    def test_host_kick_player_success(self):
        game = Game.objects.create(quiz=self.quiz, host=self.teacher, code='111222', status=Game.Status.LOBBY)
        gp = GamePlayer.objects.create(game=game, player=self.student)

        self.client.login(username='ac_teacher', password='password123')
        url = reverse('quiz_games:host_kick_player', kwargs={'code': game.code, 'player_id': gp.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['kicked_id'], gp.id)
        self.assertFalse(GamePlayer.objects.filter(id=gp.id).exists())

    def test_host_kick_player_permission_denied_for_non_host(self):
        game = Game.objects.create(quiz=self.quiz, host=self.teacher, code='111222', status=Game.Status.LOBBY)
        gp = GamePlayer.objects.create(game=game, player=self.student)

        self.client.login(username='ac_student2', password='password123')
        url = reverse('quiz_games:host_kick_player', kwargs={'code': game.code, 'player_id': gp.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(GamePlayer.objects.filter(id=gp.id).exists())

    def test_host_kick_player_forbidden_when_not_in_lobby(self):
        game = Game.objects.create(quiz=self.quiz, host=self.teacher, code='111222', status=Game.Status.RUNNING)
        gp = GamePlayer.objects.create(game=game, player=self.student)

        self.client.login(username='ac_teacher', password='password123')
        url = reverse('quiz_games:host_kick_player', kwargs={'code': game.code, 'player_id': gp.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('Solo se puede expulsar en fase de Lobby', data['error'])
        self.assertTrue(GamePlayer.objects.filter(id=gp.id).exists())

    def test_anti_cheat_server_time_calculation_ignores_client_time(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='111222',
            status=Game.Status.RUNNING,
            current_question=0,
            question_started_at=timezone.now()
        )
        gp = GamePlayer.objects.create(game=game, player=self.student, score=0, current_streak=0)

        self.client.login(username='ac_student', password='password123')
        # El cliente envía time_taken=0.0 fraudulentamente
        payload = {
            'question_id': self.q1.id,
            'selected_answer_id': self.q1_ans1.id,
            'time_taken': 0.0
        }
        response = self.client.post(
            reverse('quiz_games:submit_player_answer', kwargs={'code': game.code}),
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_correct'])
        self.assertGreater(data['points_awarded'], 0)

    def test_response_time_is_stored_from_server_clock(self):
        """Cada respuesta guarda los segundos que pasaron desde que se mostró la pregunta."""
        from datetime import timedelta
        game = Game.objects.create(
            quiz=self.quiz, host=self.teacher, code='555000',
            status=Game.Status.RUNNING, current_question=0,
            question_started_at=timezone.now() - timedelta(seconds=4),
        )
        gp = GamePlayer.objects.create(game=game, player=self.student)
        self.client.login(username='ac_student', password='password123')
        # El cliente intenta mentir diciendo que tardó 0 s
        self.client.post(
            reverse('quiz_games:submit_player_answer', kwargs={'code': game.code}),
            data={'question_id': self.q1.id, 'selected_answer_id': self.q1_ans1.id, 'time_taken': 0.0},
            content_type='application/json'
        )
        saved = PlayerAnswer.objects.get(game_player=gp, question=self.q1)
        self.assertIsNotNone(saved.response_time)
        self.assertGreaterEqual(saved.response_time, 4.0)
        self.assertLess(saved.response_time, 10.0)

    def test_broadcast_failure_is_logged_not_hidden(self):
        from unittest import mock
        from quiz_games import services
        with mock.patch.object(services, 'get_channel_layer') as layer_mock, \
                mock.patch.object(services, 'async_to_sync', side_effect=RuntimeError('canal caído')):
            layer_mock.return_value = object()
            with self.assertLogs('quiz_games.services', level='ERROR') as logs:
                services.broadcast_to_room('123456', {'event': 'x'})
        self.assertIn('123456', logs.output[0])

    def test_game_player_resume_redirects_to_running_game(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='333444',
            status=Game.Status.RUNNING,
            started_at=timezone.now(),
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student, score=500, current_streak=2)

        self.client.login(username='ac_student', password='password123')
        response = self.client.get(reverse('quiz_games:game_player_resume'))
        self.assertRedirects(response, reverse('quiz_games:game_player_play', kwargs={'code': '333444'}))

    def test_game_player_resume_redirects_to_join_when_no_active_game(self):
        # Partida ya finalizada
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='333444',
            status=Game.Status.FINISHED
        )
        GamePlayer.objects.create(game=game, player=self.student)

        self.client.login(username='ac_student', password='password123')
        response = self.client.get(reverse('quiz_games:game_player_resume'))
        self.assertRedirects(response, reverse('quiz_games:game_join'))


class AnalyticsAndExportTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.teacher = User.objects.create_user(
            username='analytics_teacher',
            first_name='Albert',
            last_name='Einstein',
            password='password123'
        )
        attach_profile(self.teacher, role='TEACHER')

        self.student1 = User.objects.create_user(
            username='analytics_student1',
            first_name='Marie',
            last_name='Curie',
            password='password123'
        )
        attach_profile(self.student1, role='STUDENT')

        self.student2 = User.objects.create_user(
            username='analytics_student2',
            first_name='Isaac',
            last_name='Newton',
            password='password123'
        )
        attach_profile(self.student2, role='STUDENT')

        self.other_user = User.objects.create_user(username='other_user', password='password123')
        attach_profile(self.other_user, role='STUDENT')

        self.quiz = Quiz.objects.create(
            title='Física Cuántica',
            creator=self.teacher,
            is_published=True
        )
        self.q1 = Question.objects.create(
            quiz=self.quiz,
            text='¿Qué partícula no tiene masa?',
            time_limit=30,
            points=1000,
            order=1
        )
        self.ans1 = Answer.objects.create(question=self.q1, text='Fotón', is_correct=True, order=1)
        self.ans2 = Answer.objects.create(question=self.q1, text='Protón', is_correct=False, order=2)

    def test_game_question_breakdown_success(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='888999',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp1 = GamePlayer.objects.create(game=game, player=self.student1)
        gp2 = GamePlayer.objects.create(game=game, player=self.student2)

        PlayerAnswer.objects.create(
            game_player=gp1,
            question=self.q1,
            selected_answer=self.ans1,
            is_correct=True,
            points_awarded=1000
        )
        PlayerAnswer.objects.create(
            game_player=gp2,
            question=self.q1,
            selected_answer=self.ans2,
            is_correct=False,
            points_awarded=0
        )

        self.client.login(username='analytics_teacher', password='password123')
        url = reverse('quiz_games:game_question_breakdown', kwargs={'code': game.code})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['question_text'], self.q1.text)
        self.assertEqual(data['total_answers'], 2)
        self.assertEqual(len(data['breakdown']), 2)

        # Check breakdown counts
        fot_item = next(item for item in data['breakdown'] if item['answer_id'] == self.ans1.id)
        self.assertEqual(fot_item['count'], 1)
        self.assertTrue(fot_item['is_correct'])

        prot_item = next(item for item in data['breakdown'] if item['answer_id'] == self.ans2.id)
        self.assertEqual(prot_item['count'], 1)
        self.assertFalse(prot_item['is_correct'])

    def test_game_question_breakdown_denies_non_host(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='888999',
            status=Game.Status.RUNNING,
            current_question=0
        )
        self.client.login(username='analytics_student1', password='password123')
        url = reverse('quiz_games:game_question_breakdown', kwargs={'code': game.code})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_game_question_breakdown_invalid_question_index(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='888999',
            status=Game.Status.RUNNING,
            current_question=99
        )
        self.client.login(username='analytics_teacher', password='password123')
        url = reverse('quiz_games:game_question_breakdown', kwargs={'code': game.code})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])

    def test_export_game_results_csv_success(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='888999',
            status=Game.Status.FINISHED
        )
        gp1 = GamePlayer.objects.create(game=game, player=self.student1, score=1500, correct_answers=1, current_streak=1)
        gp2 = GamePlayer.objects.create(game=game, player=self.student2, score=500, correct_answers=0, current_streak=0)

        self.client.login(username='analytics_teacher', password='password123')
        url = reverse('quiz_games:export_game_results_csv', kwargs={'code': game.code})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="reporte_partida_888999', response['Content-Disposition'])

        content = response.content.decode('utf-8-sig')
        self.assertIn('Posición,Usuario,Nombre Completo,Puntos Totales,Respuestas Correctas,Total Preguntas,Precisión (%),Racha Final', content)
        self.assertIn('1,analytics_student1,Marie Curie,1500,1,1,100.0%,1', content)
        self.assertIn('2,analytics_student2,Isaac Newton,500,0,1,0.0%,0', content)

    def test_export_game_results_csv_denies_non_host(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='888999',
            status=Game.Status.FINISHED
        )
        self.client.login(username='analytics_student1', password='password123')
        url = reverse('quiz_games:export_game_results_csv', kwargs={'code': game.code})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)


class NetworkPerformanceAndAudioTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.teacher = User.objects.create_user(username='perf_teacher', password='password123')
        attach_profile(self.teacher, role='TEACHER')

        self.student = User.objects.create_user(username='perf_student', password='password123')
        attach_profile(self.student, role='STUDENT')

        self.quiz = Quiz.objects.create(
            title='Quiz Performance Test',
            creator=self.teacher,
            is_published=True
        )
        self.q1 = Question.objects.create(
            quiz=self.quiz,
            text='Pregunta Performance',
            time_limit=30,
            points=1000,
            order=1
        )
        self.ans1 = Answer.objects.create(question=self.q1, text='Opción A', is_correct=True, order=1)
        self.ans2 = Answer.objects.create(question=self.q1, text='Opción B', is_correct=False, order=2)

    def test_game_lobby_status_etag_and_304(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='667788',
            status=Game.Status.LOBBY
        )
        GamePlayer.objects.create(game=game, player=self.student)

        self.client.login(username='perf_teacher', password='password123')
        url = reverse('quiz_games:game_lobby_status', kwargs={'code': game.code})

        # 1. Primera petición -> 200 con cabecera ETag
        response1 = self.client.get(url)
        self.assertEqual(response1.status_code, 200)
        self.assertIn('ETag', response1.headers)
        etag = response1.headers['ETag']
        self.assertTrue(len(etag) > 0)

        # 2. Petición condicional con If-None-Match idéntico -> 304 Not Modified
        response2 = self.client.get(url, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(response2.status_code, 304)

        # 3. Tras cambio (otro jugador se une) -> 200 con nuevo ETag
        student2 = User.objects.create_user(username='perf_student2', password='password123')
        attach_profile(student2, role='STUDENT')
        GamePlayer.objects.create(game=game, player=student2)

        response3 = self.client.get(url, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(response3.status_code, 200)
        self.assertNotEqual(response3.headers['ETag'], etag)

    def test_game_question_status_etag_and_304(self):
        game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='667788',
            status=Game.Status.RUNNING,
            current_question=0
        )
        gp = GamePlayer.objects.create(game=game, player=self.student)

        self.client.login(username='perf_teacher', password='password123')
        url = reverse('quiz_games:game_question_status', kwargs={'code': game.code})

        # 1. Primera petición -> 200 con ETag
        response1 = self.client.get(url)
        self.assertEqual(response1.status_code, 200)
        self.assertIn('ETag', response1.headers)
        etag = response1.headers['ETag']

        # 2. Petición condicional con If-None-Match idéntico -> 304
        response2 = self.client.get(url, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(response2.status_code, 304)

        # 3. Tras respuesta enviada -> cambia ETag -> 200
        PlayerAnswer.objects.create(
            game_player=gp,
            question=self.q1,
            selected_answer=self.ans1,
            is_correct=True,
            points_awarded=1000
        )
        response3 = self.client.get(url, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(response3.status_code, 200)
        self.assertNotEqual(response3.headers['ETag'], etag)







class CurrentQuestionAndAccessTests(TestCase):
    """
    Requisitos del informe del proyecto para el motor de partidas:
    - La respuesta enviada debe pertenecer a la pregunta ACTUAL del juego
      (tanto por la vista HTTP como por el consumidor WebSocket).
    - Una partida ya finalizada no se puede finalizar de nuevo.
    - Solo el anfitrión y los jugadores de una partida pueden consultar su estado
      o conectarse a su sala en tiempo real.
    """

    def setUp(self):
        self.client = Client()
        self.teacher = User.objects.create_user(username='cq_teacher', password='password123')
        attach_profile(self.teacher, role='TEACHER')
        self.student = User.objects.create_user(username='cq_student', password='password123')
        attach_profile(self.student, role='STUDENT')
        self.outsider = User.objects.create_user(username='cq_outsider', password='password123')
        attach_profile(self.outsider, role='STUDENT')

        self.quiz = Quiz.objects.create(title='Quiz pregunta actual', creator=self.teacher, is_published=True)
        self.q1 = Question.objects.create(quiz=self.quiz, text='Pregunta uno', time_limit=30, points=1000, order=1)
        self.q1_ok = Answer.objects.create(question=self.q1, text='Correcta 1', is_correct=True, order=1)
        self.q1_bad = Answer.objects.create(question=self.q1, text='Incorrecta 1', is_correct=False, order=2)
        self.q2 = Question.objects.create(quiz=self.quiz, text='Pregunta dos', time_limit=30, points=1000, order=2)
        self.q2_ok = Answer.objects.create(question=self.q2, text='Correcta 2', is_correct=True, order=1)
        self.q2_bad = Answer.objects.create(question=self.q2, text='Incorrecta 2', is_correct=False, order=2)

        self.game = Game.objects.create(
            quiz=self.quiz,
            host=self.teacher,
            code='445566',
            status=Game.Status.RUNNING,
            current_question=0,
            question_started_at=timezone.now()
        )
        self.gp = GamePlayer.objects.create(game=self.game, player=self.student)

    def _http_submit(self, question, answer):
        self.client.login(username='cq_student', password='password123')
        return self.client.post(
            reverse('quiz_games:submit_player_answer', kwargs={'code': self.game.code}),
            data={'question_id': question.id, 'selected_answer_id': answer.id},
            content_type='application/json'
        )

    def _ws_submit(self, question, answer):
        consumer = GameRoomConsumer()
        return async_to_sync(consumer.process_answer_db)(
            self.student, self.game.code, question.id, answer.id
        )

    # ----- Vista HTTP: pregunta actual -----
    def test_http_rejects_answer_to_non_current_question(self):
        response = self._http_submit(self.q2, self.q2_ok)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.gp.refresh_from_db()
        self.assertEqual(self.gp.score, 0)
        self.assertEqual(self.gp.correct_answers, 0)
        self.assertFalse(PlayerAnswer.objects.filter(game_player=self.gp).exists())

    def test_http_accepts_answer_once_host_advances_to_that_question(self):
        self.game.current_question = 1
        self.game.save(update_fields=['current_question'])
        response = self._http_submit(self.q2, self.q2_ok)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['is_correct'])
        self.assertTrue(PlayerAnswer.objects.filter(game_player=self.gp, question=self.q2).exists())

    def test_http_rejects_answer_to_previous_question_after_advance(self):
        self.game.current_question = 1
        self.game.save(update_fields=['current_question'])
        response = self._http_submit(self.q1, self.q1_ok)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PlayerAnswer.objects.filter(game_player=self.gp).exists())

    # ----- Consumidor WebSocket: pregunta actual -----
    def test_ws_rejects_answer_to_non_current_question(self):
        result = self._ws_submit(self.q2, self.q2_ok)
        self.assertIn('error', result)
        self.gp.refresh_from_db()
        self.assertEqual(self.gp.score, 0)
        self.assertFalse(PlayerAnswer.objects.filter(game_player=self.gp).exists())

    def test_ws_accepts_answer_to_current_question(self):
        result = self._ws_submit(self.q1, self.q1_ok)
        self.assertNotIn('error', result)
        self.assertTrue(result['is_correct'])
        self.assertTrue(PlayerAnswer.objects.filter(game_player=self.gp, question=self.q1).exists())

    # ----- Finalizar dos veces -----
    def test_finishing_an_already_finished_game_keeps_original_finished_at(self):
        original = timezone.now() - timezone.timedelta(hours=1)
        self.game.status = Game.Status.FINISHED
        self.game.finished_at = original
        self.game.save(update_fields=['status', 'finished_at'])

        self.client.login(username='cq_teacher', password='password123')
        response = self.client.post(reverse('quiz_games:host_finish_game', kwargs={'code': self.game.code}))
        self.assertRedirects(response, reverse('quiz_games:game_leaderboard', kwargs={'code': self.game.code}))

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, Game.Status.FINISHED)
        self.assertEqual(self.game.finished_at, original)

    # ----- Estado de la pregunta: pertenencia a la partida -----
    def test_question_status_denies_user_outside_the_game(self):
        self.client.login(username='cq_outsider', password='password123')
        response = self.client.get(reverse('quiz_games:game_question_status', kwargs={'code': self.game.code}))
        self.assertEqual(response.status_code, 403)

    def test_question_status_allows_player_and_host(self):
        for username in ('cq_student', 'cq_teacher'):
            self.client.login(username=username, password='password123')
            response = self.client.get(reverse('quiz_games:game_question_status', kwargs={'code': self.game.code}))
            self.assertEqual(response.status_code, 200)

    # ----- Conexión WebSocket: pertenencia a la partida -----
    async def _ws_connect(self, user):
        communicator = WebsocketCommunicator(GameRoomConsumer.as_asgi(), f"/ws/game/{self.game.code}/")
        communicator.scope['user'] = user
        communicator.scope['url_route'] = {'kwargs': {'code': self.game.code}}
        connected, _ = await communicator.connect()
        if connected:
            await communicator.disconnect()
        return connected

    async def test_ws_connect_denied_for_anonymous_user(self):
        self.assertFalse(await self._ws_connect(AnonymousUser()))

    async def test_ws_connect_denied_for_authenticated_user_outside_the_game(self):
        self.assertFalse(await self._ws_connect(self.outsider))

    async def test_ws_connect_allowed_for_player_and_host(self):
        self.assertTrue(await self._ws_connect(self.student))
        self.assertTrue(await self._ws_connect(self.teacher))

    async def test_ws_submit_non_current_question_replies_with_error_message(self):
        """El mensaje completo por WebSocket informa el rechazo y no acredita puntos."""
        communicator = WebsocketCommunicator(GameRoomConsumer.as_asgi(), f"/ws/game/{self.game.code}/")
        communicator.scope['user'] = self.student
        communicator.scope['url_route'] = {'kwargs': {'code': self.game.code}}
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.receive_json_from()  # connection_established

        await communicator.send_json_to({
            'action': 'submit_answer',
            'question_id': self.q2.id,
            'selected_answer_id': self.q2_ok.id,
        })
        reply = await communicator.receive_json_from()
        await communicator.disconnect()

        self.assertEqual(reply['type'], 'answer_result')
        self.assertTrue(reply['error'])
        self.assertEqual(reply['ducky_coins_awarded'], 0)


class ServicesLayerTests(TestCase):
    """Reglas únicas de services.py, compartidas por las vistas HTTP y el WebSocket."""

    def setUp(self):
        from datetime import timedelta
        self.timedelta = timedelta
        self.teacher = User.objects.create_user(username='sv_teacher', password='password123')
        attach_profile(self.teacher, role='TEACHER')
        self.student = User.objects.create_user(username='sv_student', password='password123')
        attach_profile(self.student, role='STUDENT')
        self.student2 = User.objects.create_user(username='sv_student2', password='password123')
        attach_profile(self.student2, role='STUDENT')

        self.quiz = Quiz.objects.create(title='Quiz servicios', creator=self.teacher, is_published=True)
        self.questions = []
        self.correct = []
        self.wrong = []
        for n in range(1, 4):
            q = Question.objects.create(quiz=self.quiz, text=f'P{n}', time_limit=30, points=1000, order=n)
            self.correct.append(Answer.objects.create(question=q, text='ok', is_correct=True, order=1))
            self.wrong.append(Answer.objects.create(question=q, text='mal', is_correct=False, order=2))
            self.questions.append(q)

        self.game = Game.objects.create(quiz=self.quiz, host=self.teacher, code='778899')
        self.gp = GamePlayer.objects.create(game=self.game, player=self.student)

    def _start(self):
        from quiz_games import services
        return services.start_game(self.game)

    # ----- Puntuación -----
    def test_calculate_points_formula(self):
        from quiz_games.services import calculate_points
        # 6 s de 30 s => factor 0.8; primera correcta => sin bono de racha
        self.assertEqual(calculate_points(1000, 30, 6, 1), 800)
        # tercera correcta seguida => bono (3-1)*100 = 200
        self.assertEqual(calculate_points(1000, 30, 6, 3), 1000)
        # respuesta lenta: el factor nunca baja de 0.5
        self.assertEqual(calculate_points(1000, 30, 30, 1), 500)
        # el bono de racha tiene tope de 500
        self.assertEqual(calculate_points(1000, 30, 30, 20), 1000)

    def test_question_difficulty_does_not_change_game_score(self):
        """La dificultad solo se guarda para el Ecomotor (XP); los puntos de la partida no cambian."""
        from quiz_games import services
        self._start()
        for difficulty in ('facil', 'experto'):
            Question.objects.filter(pk=self.questions[0].pk).update(difficulty=difficulty)
            PlayerAnswer.objects.all().delete()
            GamePlayer.objects.filter(pk=self.gp.pk).update(score=0, correct_answers=0, current_streak=0)
            result = services.submit_answer(self.game, self.student, self.questions[0].pk, self.correct[0].pk)
            self.assertTrue(result['is_correct'])
            # Respuesta casi instantánea: factor de tiempo ~1, sin racha => ~1000 en ambos casos
            self.assertGreaterEqual(result['points_awarded'], 950)
            self.assertLessEqual(result['points_awarded'], 1000)

    # ----- Flujo con índice base 0 -----
    def test_start_game_sets_index_zero_and_running(self):
        result = self._start()
        self.assertTrue(result['success'])
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, Game.Status.RUNNING)
        self.assertEqual(self.game.current_question, 0)
        self.assertEqual(self.game.quiz.questions.order_by('order')[0].pk, self.questions[0].pk)
        from quiz_games.services import get_current_question
        self.assertEqual(get_current_question(self.game).pk, self.questions[0].pk)

    def test_start_game_requires_lobby_players_and_questions(self):
        from quiz_games import services
        empty_game = Game.objects.create(quiz=self.quiz, host=self.teacher, code='990011')
        self.assertEqual(services.start_game(empty_game)['code'], 'no_players')
        empty_quiz = Quiz.objects.create(title='Sin preguntas', creator=self.teacher, is_published=True)
        game_no_q = Game.objects.create(quiz=empty_quiz, host=self.teacher, code='990022')
        GamePlayer.objects.create(game=game_no_q, player=self.student)
        self.assertEqual(services.start_game(game_no_q)['code'], 'no_questions')
        self._start()
        self.assertEqual(self._start()['code'], 'bad_state')

    def test_start_game_requires_published_quiz(self):
        """TDA Equipo 2, tarea 18: antes de iniciar se comprueba que el Quiz sigue publicado."""
        from quiz_games import services
        draft_quiz = Quiz.objects.create(title='Despublicado', creator=self.teacher, is_published=True)
        Question.objects.create(quiz=draft_quiz, text='¿Qué es?', order=1)
        game = Game.objects.create(quiz=draft_quiz, host=self.teacher, code='990033')
        GamePlayer.objects.create(game=game, player=self.student)
        draft_quiz.is_published = False
        draft_quiz.save()
        result = services.start_game(game)
        self.assertEqual(result['code'], 'not_published')
        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.LOBBY)

    def test_advance_walks_all_questions_then_finishes(self):
        from quiz_games import services
        self._start()
        for expected_index in (1, 2):
            result = services.advance_game(self.game)
            self.assertEqual(result['event'], 'next_question')
            self.assertEqual(result['current_question'], expected_index)
            self.assertEqual(result['current_number'], expected_index + 1)
        result = services.advance_game(self.game)
        self.assertEqual(result['event'], 'game_finished')
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, Game.Status.FINISHED)
        self.assertIsNotNone(self.game.finished_at)
        # Una partida cerrada ya no avanza
        self.assertEqual(services.advance_game(self.game)['code'], 'bad_state')

    # ----- Regla única de respuesta tardía (HTTP y WebSocket idénticos) -----
    def _make_late(self):
        self._start()
        self.game.refresh_from_db()
        self.game.question_started_at = timezone.now() - self.timedelta(seconds=60)
        self.game.save(update_fields=['question_started_at'])

    def test_late_correct_answer_scores_zero_and_breaks_streak_via_http(self):
        self._make_late()
        self.gp.current_streak = 4
        self.gp.save(update_fields=['current_streak'])
        self.client.login(username='sv_student', password='password123')
        response = self.client.post(
            reverse('quiz_games:submit_player_answer', kwargs={'code': self.game.code}),
            data={'question_id': self.questions[0].id, 'selected_answer_id': self.correct[0].id},
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['points_awarded'], 0)
        self.gp.refresh_from_db()
        self.assertEqual(self.gp.score, 0)
        self.assertEqual(self.gp.current_streak, 0)
        self.assertEqual(self.gp.correct_answers, 0)

    def test_late_correct_answer_scores_zero_via_websocket_path(self):
        self._make_late()
        consumer = GameRoomConsumer()
        result = async_to_sync(consumer.process_answer_db)(
            self.student, self.game.code, self.questions[0].id, self.correct[0].id
        )
        self.assertEqual(result['ducky_coins_awarded'], 0)
        self.gp.refresh_from_db()
        self.assertEqual(self.gp.score, 0)

    # ----- Las cuatro validaciones del PDF -----
    def test_four_mandatory_validations(self):
        from quiz_games import services
        # 2. estado distinto de RUNNING
        r = services.submit_answer(self.game, self.student, self.questions[0].id, self.correct[0].id)
        self.assertEqual(r['code'], 'bad_state')
        self._start()
        # 1. usuario no inscrito
        r = services.submit_answer(self.game, self.student2, self.questions[0].id, self.correct[0].id)
        self.assertEqual(r['code'], 'not_participant')
        # 3. pregunta que no es la actual
        r = services.submit_answer(self.game, self.student, self.questions[1].id, self.correct[1].id)
        self.assertEqual(r['code'], 'not_current')
        # respuesta que pertenece a OTRA pregunta
        r = services.submit_answer(self.game, self.student, self.questions[0].id, self.correct[1].id)
        self.assertEqual(r['code'], 'invalid')
        # camino feliz y 4. respuesta repetida
        r = services.submit_answer(self.game, self.student, self.questions[0].id, self.correct[0].id)
        self.assertTrue(r['is_correct'])
        r = services.submit_answer(self.game, self.student, self.questions[0].id, self.correct[0].id)
        self.assertEqual(r['code'], 'already_answered')
        self.assertEqual(PlayerAnswer.objects.filter(game_player=self.gp).count(), 1)

    # ----- Ranking según el PDF: -score, joined_at -----
    def test_ranking_orders_by_score_then_join_time(self):
        from quiz_games.services import ranked_players
        gp2 = GamePlayer.objects.create(game=self.game, player=self.student2)
        self.gp.score = 500
        self.gp.save(update_fields=['score'])
        gp2.score = 500
        gp2.save(update_fields=['score'])
        self.assertEqual([p.pk for p in ranked_players(self.game)], [self.gp.pk, gp2.pk])
        gp2.score = 900
        gp2.save(update_fields=['score'])
        self.assertEqual([p.pk for p in ranked_players(self.game)], [gp2.pk, self.gp.pk])


class FullGameWebSocketFlowTest(TestCase):
    """Partida completa de punta a punta por WebSocket: iniciar, responder, avanzar, finalizar."""

    def test_full_game_flow(self):
        teacher = User.objects.create_user(username='fg_teacher', password='password123')
        attach_profile(teacher, role='TEACHER')
        s1 = User.objects.create_user(username='fg_s1', password='password123')
        s2 = User.objects.create_user(username='fg_s2', password='password123')
        for u in (s1, s2):
            attach_profile(u, role='STUDENT')
        quiz = Quiz.objects.create(title='Flujo completo', creator=teacher, is_published=True)
        qs = []
        for n in (1, 2):
            q = Question.objects.create(quiz=quiz, text=f'P{n}', time_limit=30, points=1000, order=n)
            ok = Answer.objects.create(question=q, text='ok', is_correct=True, order=1)
            bad = Answer.objects.create(question=q, text='mal', is_correct=False, order=2)
            qs.append((q, ok, bad))
        game = Game.objects.create(quiz=quiz, host=teacher, code='123123')
        GamePlayer.objects.create(game=game, player=s1)
        GamePlayer.objects.create(game=game, player=s2)

        async def run():
            async def connect(user):
                comm = WebsocketCommunicator(GameRoomConsumer.as_asgi(), f"/ws/game/{game.code}/")
                comm.scope['user'] = user
                comm.scope['url_route'] = {'kwargs': {'code': game.code}}
                ok, _ = await comm.connect()
                assert ok
                await comm.receive_json_from()
                return comm

            async def drain(comm, until_event):
                for _ in range(10):
                    msg = await comm.receive_json_from()
                    if msg.get('event') == until_event or msg.get('type') == until_event:
                        return msg
                raise AssertionError(f'no llegó {until_event}')

            host, c1, c2 = await connect(teacher), await connect(s1), await connect(s2)
            await host.send_json_to({'action': 'host_start_game'})
            started = await drain(host, 'game_started')
            self.assertEqual(started['current_question'], 0)

            # Pregunta 1: s1 acierta, s2 falla
            await c1.send_json_to({'action': 'submit_answer', 'question_id': qs[0][0].id, 'selected_answer_id': qs[0][1].id})
            r1 = await drain(c1, 'answer_result')
            await c2.send_json_to({'action': 'submit_answer', 'question_id': qs[0][0].id, 'selected_answer_id': qs[0][2].id})
            r2 = await drain(c2, 'answer_result')
            self.assertTrue(r1['is_correct'])
            self.assertGreater(r1['ducky_coins_awarded'], 0)
            self.assertFalse(r2['is_correct'])

            await host.send_json_to({'action': 'host_next_question'})
            nxt = await drain(host, 'next_question')
            self.assertEqual(nxt['current_question'], 1)

            # Pregunta 2: ambos aciertan
            for comm in (c1, c2):
                await comm.send_json_to({'action': 'submit_answer', 'question_id': qs[1][0].id, 'selected_answer_id': qs[1][1].id})
                res = await drain(comm, 'answer_result')
                self.assertTrue(res['is_correct'])

            await host.send_json_to({'action': 'host_next_question'})
            await drain(host, 'game_finished')
            for comm in (host, c1, c2):
                await comm.disconnect()

        async_to_sync(run)()

        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.FINISHED)
        ranking = list(game.players.order_by('-score', 'joined_at').values_list('player__username', flat=True))
        self.assertEqual(ranking[0], 'fg_s1')  # acertó las dos; fg_s2 solo la segunda
        self.client.login(username='fg_teacher', password='password123')
        self.assertEqual(self.client.get(reverse('quiz_games:game_podium', kwargs={'code': game.code})).status_code, 200)
        self.assertEqual(self.client.get(reverse('quiz_games:game_leaderboard', kwargs={'code': game.code})).status_code, 200)


class HttpActionsNotifyPlayersTest(TestCase):
    """Las acciones del docente por HTTP también deben avisar en vivo a los alumnos."""

    def setUp(self):
        self.teacher = User.objects.create_user(username='nt_teacher', password='password123')
        attach_profile(self.teacher, role='TEACHER')
        self.student = User.objects.create_user(username='nt_student', password='password123')
        attach_profile(self.student, role='STUDENT')
        quiz = Quiz.objects.create(title='Aviso en vivo', creator=self.teacher, is_published=True)
        self.q1 = Question.objects.create(quiz=quiz, text='P1', time_limit=30, points=1000, order=1)
        Answer.objects.create(question=self.q1, text='ok', is_correct=True, order=1)
        Answer.objects.create(question=self.q1, text='mal', is_correct=False, order=2)
        self.game = Game.objects.create(quiz=quiz, host=self.teacher, code='321321',
                                        status=Game.Status.RUNNING, current_question=0,
                                        question_started_at=timezone.now())
        GamePlayer.objects.create(game=self.game, player=self.student)

    def _student_socket(self):
        comm = WebsocketCommunicator(GameRoomConsumer.as_asgi(), f"/ws/game/{self.game.code}/")
        comm.scope['user'] = self.student
        comm.scope['url_route'] = {'kwargs': {'code': self.game.code}}
        return comm

    def test_http_finish_game_broadcasts_game_finished_to_players(self):
        from asgiref.sync import sync_to_async
        client = Client()
        client.login(username='nt_teacher', password='password123')
        url = reverse('quiz_games:host_finish_game', kwargs={'code': self.game.code})

        async def run():
            comm = self._student_socket()
            connected, _ = await comm.connect()
            assert connected
            await comm.receive_json_from()  # connection_established
            response = await sync_to_async(client.post)(url)
            msg = await comm.receive_json_from()
            await comm.disconnect()
            return response, msg

        response, msg = async_to_sync(run)()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(msg['event'], 'game_finished')
        self.assertIn(self.game.code, msg['leaderboard_url'])
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, Game.Status.FINISHED)

    def test_http_next_question_on_last_question_broadcasts_game_finished(self):
        from asgiref.sync import sync_to_async
        client = Client()
        client.login(username='nt_teacher', password='password123')
        url = reverse('quiz_games:host_next_question', kwargs={'code': self.game.code})

        async def run():
            comm = self._student_socket()
            connected, _ = await comm.connect()
            assert connected
            await comm.receive_json_from()
            response = await sync_to_async(client.post)(url)
            msg = await comm.receive_json_from()
            await comm.disconnect()
            return response, msg

        response, msg = async_to_sync(run)()
        self.assertEqual(response.json()['action'], 'GAME_FINISHED')
        self.assertEqual(msg['event'], 'game_finished')


class BestStreakTests(TestCase):
    """Paso 3: la «mejor racha» del resultado individual (TDA Equipo 2, tarea 28)."""

    def setUp(self):
        self.teacher = User.objects.create_user(username='bs_teacher', password='password123')
        attach_profile(self.teacher, role='TEACHER')
        self.student = User.objects.create_user(username='bs_student', password='password123')
        attach_profile(self.student, role='STUDENT')
        self.quiz = Quiz.objects.create(title='Rachas', creator=self.teacher, is_published=True)
        self.answers = []
        for n in range(1, 7):
            q = Question.objects.create(quiz=self.quiz, text=f'P{n}', time_limit=30, points=1000, order=n)
            self.answers.append(Answer.objects.create(question=q, text='ok', is_correct=True, order=1))
        self.game = Game.objects.create(quiz=self.quiz, host=self.teacher, code='565656',
                                        status=Game.Status.FINISHED)
        self.gp = GamePlayer.objects.create(game=self.game, player=self.student)

    def _answer(self, index, points):
        PlayerAnswer.objects.create(
            game_player=self.gp, question=self.answers[index].question,
            selected_answer=self.answers[index], is_correct=points > 0, points_awarded=points)

    def test_no_answers_means_zero(self):
        self.assertEqual(services.best_streak(self.gp), 0)

    def test_longest_run_is_returned_not_the_final_one(self):
        # acierta, acierta, acierta, falla, acierta, acierta  -> mejor 3 (la final es solo 2)
        for index, points in enumerate([800, 900, 700, 0, 800, 600]):
            self._answer(index, points)
        self.assertEqual(services.best_streak(self.gp), 3)

    def test_all_correct_is_the_total(self):
        for index in range(6):
            self._answer(index, 500)
        self.assertEqual(services.best_streak(self.gp), 6)

    def test_late_correct_answer_breaks_the_streak(self):
        # el 3.º acierto llegó fuera de tiempo: 0 puntos, como en submit_answer
        for index, points in enumerate([800, 800, 0, 800]):
            self._answer(index, points)
        self.assertEqual(services.best_streak(self.gp), 2)

    def test_results_page_shows_best_streak(self):
        for index, points in enumerate([800, 900, 700, 0, 800]):
            self._answer(index, points)
        client = Client()
        client.login(username='bs_student', password='password123')
        response = client.get(reverse('quiz_games:game_player_results', kwargs={'code': self.game.code}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['best_streak'], 3)
        self.assertContains(response, 'Mejor Racha')


class CancelGameTests(TestCase):
    """Paso 2: el botón «Cancelar Sala» cancela de verdad (LOBBY -> CANCELLED)."""

    def setUp(self):
        self.teacher = User.objects.create_user(username='cc_teacher', password='password123')
        attach_profile(self.teacher, role='TEACHER')
        self.other = User.objects.create_user(username='cc_other', password='password123')
        attach_profile(self.other, role='TEACHER')
        self.student = User.objects.create_user(username='cc_student', password='password123')
        attach_profile(self.student, role='STUDENT')
        self.quiz = Quiz.objects.create(title='Cancelable', creator=self.teacher, is_published=True)
        q = Question.objects.create(quiz=self.quiz, text='P1', time_limit=30, points=1000, order=1)
        Answer.objects.create(question=q, text='ok', is_correct=True, order=1)
        self.game = Game.objects.create(quiz=self.quiz, host=self.teacher, code='454545')
        GamePlayer.objects.create(game=self.game, player=self.student)
        self.url = reverse('quiz_games:host_cancel_game', kwargs={'code': self.game.code})

    def test_service_cancels_lobby(self):
        result = services.cancel_game(self.game)
        self.assertTrue(result['success'])
        self.assertEqual(result['event'], 'game_cancelled')
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, Game.Status.CANCELLED)
        self.assertIsNotNone(self.game.finished_at)

    def test_service_refuses_running_game(self):
        services.start_game(self.game)
        result = services.cancel_game(self.game)
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'bad_state')
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, Game.Status.RUNNING)

    def test_host_can_cancel_via_post(self):
        client = Client()
        client.login(username='cc_teacher', password='password123')
        response = client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, Game.Status.CANCELLED)

    def test_get_is_not_allowed(self):
        client = Client()
        client.login(username='cc_teacher', password='password123')
        self.assertEqual(client.get(self.url).status_code, 405)
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, Game.Status.LOBBY)

    def test_other_teacher_cannot_cancel(self):
        client = Client()
        client.login(username='cc_other', password='password123')
        self.assertEqual(client.post(self.url).status_code, 403)
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, Game.Status.LOBBY)

    def test_student_cannot_cancel(self):
        client = Client()
        client.login(username='cc_student', password='password123')
        self.assertEqual(client.post(self.url).status_code, 403)

    def test_cancelled_game_cannot_be_started_or_joined(self):
        services.cancel_game(self.game)
        self.assertFalse(services.start_game(self.game)['success'])
        form = JoinGameForm(data={'code': self.game.code})
        self.assertFalse(form.is_valid())
        self.assertIn('canceló', str(form.errors))

    def test_cancel_confirmation_uses_in_page_dialog_not_browser_confirm(self):
        client = Client()
        client.login(username='cc_teacher', password='password123')
        response = client.get(reverse('quiz_games:game_host_lobby', kwargs={'code': self.game.code}))
        self.assertContains(response, 'data-confirm="¿Cancelar la sala?')
        self.assertContains(response, 'id="ducky-dialog"')
        self.assertNotContains(response, 'confirm(\'')

    def test_player_lobby_of_cancelled_game_redirects_with_cancel_message(self):
        services.cancel_game(self.game)
        client = Client()
        client.login(username='cc_student', password='password123')
        response = client.get(reverse('quiz_games:game_player_lobby', kwargs={'code': self.game.code}), follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse('quiz_games:game_join'))
        self.assertContains(response, 'El docente canceló la sala.')

    def test_cancel_broadcasts_game_cancelled_to_players(self):
        from asgiref.sync import sync_to_async
        client = Client()
        client.login(username='cc_teacher', password='password123')

        async def run():
            comm = WebsocketCommunicator(GameRoomConsumer.as_asgi(), f"/ws/game/{self.game.code}/")
            comm.scope['user'] = self.student
            comm.scope['url_route'] = {'kwargs': {'code': self.game.code}}
            connected, _ = await comm.connect()
            assert connected
            await comm.receive_json_from()
            await sync_to_async(client.post)(self.url)
            msg = await comm.receive_json_from()
            await comm.disconnect()
            return msg

        msg = async_to_sync(run)()
        self.assertEqual(msg['event'], 'game_cancelled')


class XpFormulaTests(TestCase):
    """Fórmula de XP del PDF de fórmulas (DAR_formulas.pdf), función pura sin base de datos."""

    def test_ejemplo_del_pdf(self):
        # 10 x 1,5 (difícil) x 1,5 (<2 s) x 1,10 (INT 5) x 1,10 (racha 3) = 27,22 -> 27
        xp = services.calculate_xp('dificil', 1.75, int_stat=5, vel_stat=0, streak=3)
        self.assertEqual(xp, 27)

    def test_tramos_de_velocidad_con_vel_5(self):
        self.assertEqual(services.calculate_xp('facil', 1.0), 17)   # 10 x 1,55 x 1,1
        self.assertEqual(services.calculate_xp('facil', 3.0), 14)   # 10 x 1,275 x 1,1
        self.assertEqual(services.calculate_xp('facil', 5.0), 11)   # 10 x 1,00 x 1,1
        self.assertEqual(services.calculate_xp('facil', 9.0), 8)    # 10 x 0,80 x 1,1

    def test_dificultad_experto_y_racha(self):
        self.assertEqual(services.calculate_xp('experto', 5.0, streak=2), 22)  # 10 x 2 x 1 x 1,1
        self.assertEqual(services.calculate_xp('experto', 5.0, streak=3), 24)  # x 1,10 de racha

    def test_dificultad_desconocida_vale_facil(self):
        self.assertEqual(services.calculate_xp('inventada', 5.0), services.calculate_xp('facil', 5.0))

    def test_int_sube_la_xp(self):
        self.assertGreater(services.calculate_xp('media', 5.0, int_stat=10),
                           services.calculate_xp('media', 5.0, int_stat=1))

    def test_xp_de_fin_de_sesion(self):
        self.assertEqual(services.session_end_xp(1), 70)   # 20 + 50
        self.assertEqual(services.session_end_xp(2), 50)   # 20 + 30
        self.assertEqual(services.session_end_xp(3), 40)   # 20 + 20
        self.assertEqual(services.session_end_xp(4), 20)
        self.assertEqual(services.session_end_xp(None), 20)


class IdentityModuleTests(TestCase):
    """identity.py concentra permisos; debe comportarse igual que antes de aislarlo."""

    def setUp(self):
        from quiz_games import identity
        self.identity = identity
        self.teacher = attach_profile(User.objects.create_user('id_teacher', password='x'), role='TEACHER')
        self.other = attach_profile(User.objects.create_user('id_other', password='x'), role='TEACHER')
        # Usuario simulado: evita depender del perfil falso compartido entre archivos de tests
        from types import SimpleNamespace
        self.student = SimpleNamespace(id=-1, is_superuser=False, is_staff=False,
                                       profile=DummyProfile(role='STUDENT'))
        self.admin = User.objects.create_superuser('id_admin', 'a@a.com', 'x')
        self.quiz = Quiz.objects.create(title='Q', creator=self.teacher, is_published=True)
        self.game = Game.objects.create(quiz=self.quiz, host=self.teacher, code='424242')

    def test_is_teacher(self):
        self.assertTrue(self.identity.is_teacher(self.teacher))
        self.assertTrue(self.identity.is_teacher(self.admin))
        self.assertFalse(self.identity.is_teacher(self.student))

    def test_is_host_depends_only_on_the_room(self):
        self.assertTrue(self.identity.is_host(self.game, self.teacher))
        self.assertFalse(self.identity.is_host(self.game, self.other))
        self.assertFalse(self.identity.is_host(self.game, self.student))

    def test_can_create_game_requires_creator(self):
        self.assertTrue(self.identity.can_create_game(self.teacher, self.quiz))
        self.assertFalse(self.identity.can_create_game(self.other, self.quiz))
        self.assertFalse(self.identity.can_create_game(self.student, self.quiz))
        self.assertTrue(self.identity.can_create_game(self.admin, self.quiz))
