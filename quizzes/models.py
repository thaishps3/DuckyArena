from django.db import models
from django.conf import settings


class Quiz(models.Model):
    title = models.CharField(max_length=200, verbose_name="Título")
    description = models.TextField(blank=True, verbose_name="Descripción")
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='quizzes',
        verbose_name="Creador"
    )
    is_published = models.BooleanField(default=False, verbose_name="¿Publicado?")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Fecha de creación")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Última actualización")

    class Meta:
        verbose_name = "Cuestionario"
        verbose_name_plural = "Cuestionarios"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.creator.username})"


class Question(models.Model):
    class Difficulty(models.TextChoices):
        # Los 4 valores y sus claves son los que espera el Ecomotor (DAR_formulas.pdf):
        # facil x1.00, media x1.25, dificil x1.50, experto x2.00 (multiplicador de XP).
        FACIL = 'facil', 'Fácil'
        MEDIA = 'media', 'Media'
        DIFICIL = 'dificil', 'Difícil'
        EXPERTO = 'experto', 'Experto'

    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name='questions',
        verbose_name="Cuestionario"
    )
    text = models.TextField(verbose_name="Enunciado")
    time_limit = models.PositiveIntegerField(default=30, verbose_name="Tiempo límite (s)")
    points = models.PositiveIntegerField(default=1000, verbose_name="Puntos base")
    order = models.PositiveIntegerField(default=1, verbose_name="Orden")
    # Solo se guarda y se informa al Ecomotor; NO cambia la puntuación (score) de la partida.
    difficulty = models.CharField(
        max_length=10,
        choices=Difficulty.choices,
        default=Difficulty.FACIL,
        verbose_name="Dificultad",
    )

    class Meta:
        verbose_name = "Pregunta"
        verbose_name_plural = "Preguntas"
        ordering = ['order']

    def __str__(self):
        return f"[{self.quiz.title}] P{self.order}: {self.text[:50]}"


class Answer(models.Model):
    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name='answers',
        verbose_name="Pregunta"
    )
    text = models.CharField(max_length=255, verbose_name="Texto de respuesta")
    is_correct = models.BooleanField(default=False, verbose_name="¿Es correcta?")
    order = models.PositiveIntegerField(default=1, verbose_name="Orden")

    class Meta:
        verbose_name = "Respuesta"
        verbose_name_plural = "Respuestas"
        ordering = ['order']

    def __str__(self):
        return f"{self.text} ({'Correcta' if self.is_correct else 'Incorrecta'})"
