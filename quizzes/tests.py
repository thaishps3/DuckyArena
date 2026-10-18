from types import SimpleNamespace
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.core.exceptions import ValidationError
from quizzes.models import Quiz, Question, Answer
from quizzes.forms import QuizForm, QuestionForm, AnswerFormSet, BaseAnswerFormSet

User = get_user_model()

# Registro en memoria de perfiles simulados del Equipo 0 para el ciclo de pruebas
_USER_PROFILES = {}


class DummyProfile:
    def __init__(self, role='TEACHER', ducky_coins=500):
        self.role = role
        self.ducky_coins = ducky_coins


# Conectar la propiedad profile en User para emular el contrato de la app accounts
User.profile = property(lambda self: _USER_PROFILES.get(self.username, DummyProfile(role='TEACHER', ducky_coins=500)))


def attach_profile(user, role='TEACHER', ducky_coins=500):
    """Helper para registrar el rol y saldo simulado de un usuario en el entorno de pruebas."""
    profile = DummyProfile(role=role, ducky_coins=ducky_coins)
    _USER_PROFILES[user.username] = profile
    return user


class QuizModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='teacher1', password='password123')
        attach_profile(self.user, role='TEACHER')
        self.quiz = Quiz.objects.create(
            title='Fundamentos de Python',
            description='Evaluación inicial de sintaxis y tipos de datos.',
            creator=self.user,
            is_published=False
        )

    def test_quiz_creation_and_defaults(self):
        self.assertEqual(self.quiz.title, 'Fundamentos de Python')
        self.assertFalse(self.quiz.is_published)
        self.assertEqual(self.quiz.creator, self.user)
        self.assertEqual(str(self.quiz), f"Fundamentos de Python ({self.user.username})")

    def test_question_and_answer_hierarchy(self):
        question = Question.objects.create(
            quiz=self.quiz,
            text='¿Cuál es la palabra clave para definir funciones en Python?',
            time_limit=30,
            points=1000,
            order=1
        )
        self.assertEqual(question.time_limit, 30)
        self.assertEqual(question.points, 1000)
        self.assertIn('def', str(question))

        ans1 = Answer.objects.create(question=question, text='def', is_correct=True, order=1)
        ans2 = Answer.objects.create(question=question, text='func', is_correct=False, order=2)

        self.assertEqual(question.answers.count(), 2)
        self.assertTrue(ans1.is_correct)
        self.assertFalse(ans2.is_correct)
        self.assertIn('Correcta', str(ans1))
        self.assertIn('Incorrecta', str(ans2))

    def test_cascade_deletion(self):
        question = Question.objects.create(
            quiz=self.quiz,
            text='Pregunta de prueba',
            time_limit=20,
            points=500,
            order=1
        )
        Answer.objects.create(question=question, text='Opción A', is_correct=True, order=1)
        
        self.assertEqual(Question.objects.count(), 1)
        self.assertEqual(Answer.objects.count(), 1)
        
        self.quiz.delete()
        
        self.assertEqual(Question.objects.count(), 0)
        self.assertEqual(Answer.objects.count(), 0)


class QuizFormValidationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='profesor_form', password='password123')
        attach_profile(self.user, role='TEACHER')

    def test_quiz_form_title_validation(self):
        # Título menor a 5 caracteres
        form_short = QuizForm(data={'title': 'Abc', 'description': 'Test'})
        self.assertFalse(form_short.is_valid())
        self.assertIn('title', form_short.errors)

        # Título válido
        form_valid = QuizForm(data={'title': 'Python Avanzado', 'description': 'Test'})
        self.assertTrue(form_valid.is_valid())

    def test_quiz_form_cannot_publish_without_questions(self):
        quiz = Quiz.objects.create(title='Quiz Vacío', creator=self.user, is_published=False)
        form = QuizForm(data={'title': 'Quiz Vacío', 'is_published': True}, instance=quiz)
        self.assertFalse(form.is_valid())
        self.assertIn('is_published', form.errors)

    def test_question_form_limits(self):
        # Tiempo límite fuera de rango (<5 o >300)
        form_time_low = QuestionForm(data={'text': '¿Pregunta válida?', 'time_limit': 2, 'points': 500, 'order': 1})
        self.assertFalse(form_time_low.is_valid())
        self.assertIn('time_limit', form_time_low.errors)

        form_time_high = QuestionForm(data={'text': '¿Pregunta válida?', 'time_limit': 350, 'points': 500, 'order': 1})
        self.assertFalse(form_time_high.is_valid())
        self.assertIn('time_limit', form_time_high.errors)

        # Puntos base <= 0
        form_points_zero = QuestionForm(data={'text': '¿Pregunta válida?', 'time_limit': 30, 'points': 0, 'order': 1})
        self.assertFalse(form_points_zero.is_valid())
        self.assertIn('points', form_points_zero.errors)

        # Datos correctos
        form_valid = QuestionForm(data={'text': '¿Pregunta válida?', 'time_limit': 45, 'points': 1200, 'order': 1})
        self.assertTrue(form_valid.is_valid())

    def test_question_difficulty_defaults_to_facil(self):
        # Sin enviar dificultad (formularios anteriores) la pregunta queda como "fácil"
        form = QuestionForm(data={'text': '¿Pregunta válida?', 'time_limit': 45, 'points': 1200, 'order': 1})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['difficulty'], 'facil')
        self.assertEqual(Question.objects.model._meta.get_field('difficulty').default, 'facil')

    def test_question_difficulty_accepts_ecomotor_values_only(self):
        # Los 4 valores exactos que espera el Ecomotor (DAR_formulas.pdf)
        for value in ('facil', 'media', 'dificil', 'experto'):
            form = QuestionForm(data={'text': '¿Pregunta válida?', 'time_limit': 45, 'points': 1200,
                                      'order': 1, 'difficulty': value})
            self.assertTrue(form.is_valid(), value)
            self.assertEqual(form.cleaned_data['difficulty'], value)
        invalid = QuestionForm(data={'text': '¿Pregunta válida?', 'time_limit': 45, 'points': 1200,
                                     'order': 1, 'difficulty': 'imposible'})
        self.assertFalse(invalid.is_valid())
        self.assertIn('difficulty', invalid.errors)


class AnswerFormSetPedagogicalTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='profesor_fs', password='password123')
        attach_profile(self.user, role='TEACHER')
        self.quiz = Quiz.objects.create(title='Cuestionario FS', creator=self.user)
        self.question = Question.objects.create(quiz=self.quiz, text='¿Cuánto es 2 + 2?', order=1)

    def test_formset_requires_between_2_and_6_answers(self):
        # 1 respuesta solamente -> Invalido
        data = {
            'answers-TOTAL_FORMS': '1',
            'answers-INITIAL_FORMS': '0',
            'answers-MIN_NUM_FORMS': '2',
            'answers-MAX_NUM_FORMS': '6',
            'answers-0-text': '4',
            'answers-0-is_correct': True,
            'answers-0-order': 1,
        }
        formset = AnswerFormSet(data, instance=self.question, prefix='answers')
        self.assertFalse(formset.is_valid())

        # 4 respuestas con 1 correcta -> Válido
        data_valid = {
            'answers-TOTAL_FORMS': '4',
            'answers-INITIAL_FORMS': '0',
            'answers-MIN_NUM_FORMS': '2',
            'answers-MAX_NUM_FORMS': '6',
            'answers-0-text': '4',
            'answers-0-is_correct': True,
            'answers-0-order': 1,
            'answers-1-text': '3',
            'answers-1-is_correct': False,
            'answers-1-order': 2,
            'answers-2-text': '5',
            'answers-2-is_correct': False,
            'answers-2-order': 3,
            'answers-3-text': '6',
            'answers-3-is_correct': False,
            'answers-3-order': 4,
        }
        formset_valid = AnswerFormSet(data_valid, instance=self.question, prefix='answers')
        self.assertTrue(formset_valid.is_valid())

    def test_formset_requires_exactly_one_correct_answer(self):
        # 0 respuestas correctas
        data_zero_correct = {
            'answers-TOTAL_FORMS': '2',
            'answers-INITIAL_FORMS': '0',
            'answers-MIN_NUM_FORMS': '2',
            'answers-MAX_NUM_FORMS': '6',
            'answers-0-text': 'Opción 1',
            'answers-0-is_correct': False,
            'answers-0-order': 1,
            'answers-1-text': 'Opción 2',
            'answers-1-is_correct': False,
            'answers-1-order': 2,
        }
        formset_zero = AnswerFormSet(data_zero_correct, instance=self.question, prefix='answers')
        self.assertFalse(formset_zero.is_valid())
        self.assertIn("EXACTAMENTE una respuesta como correcta", str(formset_zero.non_form_errors()))

        # 2 respuestas correctas
        data_multiple_correct = {
            'answers-TOTAL_FORMS': '2',
            'answers-INITIAL_FORMS': '0',
            'answers-MIN_NUM_FORMS': '2',
            'answers-MAX_NUM_FORMS': '6',
            'answers-0-text': 'Opción 1',
            'answers-0-is_correct': True,
            'answers-0-order': 1,
            'answers-1-text': 'Opción 2',
            'answers-1-is_correct': True,
            'answers-1-order': 2,
        }
        formset_multiple = AnswerFormSet(data_multiple_correct, instance=self.question, prefix='answers')
        self.assertFalse(formset_multiple.is_valid())
        self.assertIn("EXACTAMENTE una respuesta como correcta", str(formset_multiple.non_form_errors()))


class QuizViewsSecurityTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.teacher_owner = User.objects.create_user(username='teacher_owner', password='password123')
        self.teacher_other = User.objects.create_user(username='teacher_other', password='password123')
        self.student = User.objects.create_user(username='student_user', password='password123')

        # Asignar roles simulados
        attach_profile(self.teacher_owner, role='TEACHER')
        attach_profile(self.teacher_other, role='TEACHER')
        attach_profile(self.student, role='STUDENT')

        self.quiz = Quiz.objects.create(
            title='Quiz de Álgebra',
            description='Ecuaciones lineales',
            creator=self.teacher_owner,
            is_published=False
        )
        self.question = Question.objects.create(
            quiz=self.quiz,
            text='¿x + 2 = 5, cuánto vale x?',
            time_limit=30,
            points=1000,
            order=1
        )
        Answer.objects.create(question=self.question, text='3', is_correct=True, order=1)
        Answer.objects.create(question=self.question, text='2', is_correct=False, order=2)

    def test_anonymous_user_redirected_to_login(self):
        response = self.client.get(reverse('quizzes:quiz_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_quiz_list_view_authenticated(self):
        self.client.force_login(self.teacher_owner)
        response = self.client.get(reverse('quizzes:quiz_list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'quizzes/quiz_list.html')

    def test_draft_quiz_detail_access_control(self):
        # Creador puede ver su propio cuestionario en borrador
        self.client.force_login(self.teacher_owner)
        response = self.client.get(reverse('quizzes:quiz_detail', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(response.status_code, 200)

        # Otro profesor no puede ver el borrador -> 403 PermissionDenied
        self.client.force_login(self.teacher_other)
        response = self.client.get(reverse('quizzes:quiz_detail', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(response.status_code, 403)

        # Estudiante no puede ver el borrador -> 403 PermissionDenied
        self.client.force_login(self.student)
        response = self.client.get(reverse('quizzes:quiz_detail', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(response.status_code, 403)

    def test_quiz_detail_shows_estimated_minutes_not_boolean(self):
        # Fallo heredado: la plantilla mostraba "True min". Ahora muestra minutos reales.
        from quizzes.models import Question
        self.quiz.is_published = True
        self.quiz.save()
        Question.objects.all().filter(quiz=self.quiz).delete()
        Question.objects.create(quiz=self.quiz, text='A', time_limit=90, points=100, order=1)
        Question.objects.create(quiz=self.quiz, text='B', time_limit=90, points=100, order=2)
        self.client.force_login(self.teacher_owner)
        response = self.client.get(reverse('quizzes:quiz_detail', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(response.context['estimated_minutes'], 3)
        self.assertContains(response, '3 min')
        self.assertNotContains(response, 'True min')

    def test_published_quiz_detail_hides_questions_from_students(self):
        # Un alumno no debe ver preguntas ni opciones antes de jugar; el creador sí
        self.quiz.is_published = True
        self.quiz.save()
        url = reverse('quizzes:quiz_detail', kwargs={'pk': self.quiz.pk})

        self.client.force_login(self.student)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '¿x + 2 = 5, cuánto vale x?')
        self.assertContains(response, 'Las preguntas se revelan durante la partida.')

        self.client.force_login(self.teacher_other)
        self.assertNotContains(self.client.get(url), '¿x + 2 = 5, cuánto vale x?')

        self.client.force_login(self.teacher_owner)
        self.assertContains(self.client.get(url), '¿x + 2 = 5, cuánto vale x?')

    def test_quiz_update_and_delete_restricted_to_owner(self):
        # Intento de edición por otro profesor -> 403
        self.client.force_login(self.teacher_other)
        response_edit = self.client.get(reverse('quizzes:quiz_edit', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(response_edit.status_code, 403)

        response_delete = self.client.post(reverse('quizzes:quiz_delete', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(response_delete.status_code, 403)

        # Creador puede acceder a editar y confirmar borrado
        self.client.force_login(self.teacher_owner)
        response_owner_edit = self.client.get(reverse('quizzes:quiz_edit', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(response_owner_edit.status_code, 200)

    def test_quiz_toggle_publish_validation(self):
        # Publicar con preguntas válidas
        self.client.force_login(self.teacher_owner)
        response = self.client.post(reverse('quizzes:quiz_toggle_publish', kwargs={'pk': self.quiz.pk}))
        self.assertEqual(response.status_code, 302)
        
        self.quiz.refresh_from_db()
        self.assertTrue(self.quiz.is_published)

        # Intentar publicar un quiz vacío -> no debe publicarse
        empty_quiz = Quiz.objects.create(
            title='Quiz Sin Preguntas',
            creator=self.teacher_owner,
            is_published=False
        )
        response_empty = self.client.post(reverse('quizzes:quiz_toggle_publish', kwargs={'pk': empty_quiz.pk}))
        self.assertEqual(response_empty.status_code, 302)
        empty_quiz.refresh_from_db()
        self.assertFalse(empty_quiz.is_published)
