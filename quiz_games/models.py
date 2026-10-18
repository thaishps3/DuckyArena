import secrets
from django.db import models
from django.conf import settings


class Game(models.Model):
    class Status(models.TextChoices):
        LOBBY = 'LOBBY', 'Esperando jugadores'
        RUNNING = 'RUNNING', 'En curso'
        FINISHED = 'FINISHED', 'Finalizada'
        CANCELLED = 'CANCELLED', 'Cancelada'

    quiz = models.ForeignKey(
        'quizzes.Quiz',
        on_delete=models.PROTECT,
        related_name='games',
        verbose_name="Cuestionario"
    )
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='hosted_quiz_games',
        verbose_name="Anfitrión"
    )
    code = models.CharField(max_length=6, unique=True, verbose_name="Código PIN")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.LOBBY,
        verbose_name="Estado"
    )
    current_question = models.PositiveIntegerField(
        default=0,
        verbose_name="Índice pregunta actual"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Creada en")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="Iniciada en")
    question_started_at = models.DateTimeField(null=True, blank=True, verbose_name="Inicio oficial de pregunta")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Finalizada en")

    class Meta:
        verbose_name = "Partida"
        verbose_name_plural = "Partidas"
        ordering = ['-created_at']

    @classmethod
    def generate_unique_code(cls):
        while True:
            candidate = f"{secrets.randbelow(1000000):06d}"
            if not cls.objects.filter(code=candidate).exists():
                return candidate

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = self.generate_unique_code()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Partida {self.code} - {self.quiz.title} ({self.get_status_display()})"


class GamePlayer(models.Model):
    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE,
        related_name='players',
        verbose_name="Partida"
    )
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='quiz_game_participations',
        verbose_name="Jugador"
    )
    score = models.PositiveIntegerField(default=0, verbose_name="Puntuación")
    correct_answers = models.PositiveIntegerField(default=0, verbose_name="Respuestas correctas")
    current_streak = models.PositiveIntegerField(default=0, verbose_name="Racha actual")
    is_settled = models.BooleanField(default=False, verbose_name="¿Liquidado a Profile?")
    joined_at = models.DateTimeField(auto_now_add=True, verbose_name="Unido en")

    avatar_body = models.CharField(max_length=50, default='body_yellow', verbose_name="Cuerpo del Avatar")
    avatar_clothes = models.CharField(max_length=50, default='', blank=True, verbose_name="Ropa del Avatar")
    avatar_head = models.CharField(max_length=50, default='', blank=True, verbose_name="Sombrero del Avatar")
    avatar_face = models.CharField(max_length=50, default='face_calm', verbose_name="Expresión del Avatar")

    class Meta:
        verbose_name = "Jugador de Partida"
        verbose_name_plural = "Jugadores de Partida"
        constraints = [
            models.UniqueConstraint(
                fields=['game', 'player'],
                name='unique_player_per_quiz_game'
            )
        ]

    @property
    def avatar_dict(self):
        return {
            'body': self.avatar_body or 'body_yellow',
            'clothes': self.avatar_clothes or '',
            'head': self.avatar_head or '',
            'face': self.avatar_face or 'face_calm'
        }

    def __str__(self):
        return f"{self.player.username} en Partida {self.game.code} ({self.score} pts)"


class PlayerAnswer(models.Model):
    game_player = models.ForeignKey(
        GamePlayer,
        on_delete=models.CASCADE,
        related_name='answers',
        verbose_name="Jugador"
    )
    question = models.ForeignKey(
        'quizzes.Question',
        on_delete=models.PROTECT,
        related_name='game_answers',
        verbose_name="Pregunta"
    )
    selected_answer = models.ForeignKey(
        'quizzes.Answer',
        on_delete=models.PROTECT,
        related_name='player_selections',
        verbose_name="Respuesta seleccionada"
    )
    is_correct = models.BooleanField(verbose_name="¿Fue correcta?")
    points_awarded = models.PositiveIntegerField(default=0, verbose_name="Puntos otorgados")
    response_time = models.FloatField(
        null=True, blank=True,
        verbose_name="Tiempo de respuesta (s)",
        help_text="Segundos desde que se mostró la pregunta hasta la respuesta, medidos en el servidor."
    )
    answered_at = models.DateTimeField(auto_now_add=True, verbose_name="Respondido en")

    class Meta:
        verbose_name = "Respuesta de Jugador"
        verbose_name_plural = "Respuestas de Jugadores"
        constraints = [
            models.UniqueConstraint(
                fields=['game_player', 'question'],
                name='unique_answer_per_player_question'
            )
        ]

    def __str__(self):
        status_label = 'Correcta' if self.is_correct else 'Fallida'
        return f"{self.game_player.player.username} - P{self.question.id} ({status_label})"
