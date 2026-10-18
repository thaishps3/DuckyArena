from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory, BaseInlineFormSet
from .models import Quiz, Question, Answer


class QuizForm(forms.ModelForm):
    class Meta:
        model = Quiz
        fields = ['title', 'description', 'is_published']
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'ducky-input form-control',
                'placeholder': 'Título del cuestionario (ej. Dominio de Bucles y Funciones)',
                'autofocus': True,
            }),
            'description': forms.Textarea(attrs={
                'class': 'ducky-textarea form-control',
                'placeholder': 'Descripción o instrucciones para los estudiantes...',
                'rows': 3,
            }),
            'is_published': forms.CheckboxInput(attrs={
                'class': 'ducky-checkbox form-check-input',
            }),
        }

    def clean_title(self):
        title = self.cleaned_data.get('title', '').strip()
        if len(title) < 5:
            raise ValidationError("El título debe contener al menos 5 caracteres no vacíos.")
        return title

    def clean(self):
        cleaned_data = super().clean()
        is_published = cleaned_data.get('is_published')

        # Regla: Todo Quiz publicado debe tener al menos 1 pregunta
        if is_published:
            if not self.instance.pk or not self.instance.questions.exists():
                self.add_error(
                    'is_published',
                    "No puedes publicar un cuestionario sin preguntas. Añade al menos una pregunta antes de publicarlo."
                )
        return cleaned_data


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ['text', 'time_limit', 'points', 'order', 'difficulty']
        widgets = {
            'text': forms.Textarea(attrs={
                'class': 'ducky-textarea form-control',
                'placeholder': 'Enunciado de la pregunta...',
                'rows': 3,
            }),
            'time_limit': forms.NumberInput(attrs={
                'class': 'ducky-input form-control',
                'min': 5,
                'max': 300,
                'step': 1,
            }),
            'points': forms.NumberInput(attrs={
                'class': 'ducky-input form-control',
                'min': 100,
                'step': 50,
            }),
            'order': forms.NumberInput(attrs={
                'class': 'ducky-input form-control',
                'min': 1,
            }),
            'difficulty': forms.Select(attrs={
                'class': 'ducky-input form-control',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Opcional: si no se envía, la pregunta queda como "fácil" (compatibilidad con formularios anteriores)
        self.fields['difficulty'].required = False

    def clean_difficulty(self):
        return self.cleaned_data.get('difficulty') or Question.Difficulty.FACIL

    def clean_time_limit(self):
        time_limit = self.cleaned_data.get('time_limit')
        if time_limit is None or time_limit < 5 or time_limit > 300:
            raise ValidationError("El tiempo límite debe estar comprendido entre 5 y 300 segundos.")
        return time_limit

    def clean_points(self):
        points = self.cleaned_data.get('points')
        if points is None or points <= 0:
            raise ValidationError("Los puntos asignados deben ser un valor positivo mayor a 0.")
        return points


class AnswerForm(forms.ModelForm):
    class Meta:
        model = Answer
        fields = ['text', 'is_correct', 'order']
        widgets = {
            'text': forms.TextInput(attrs={
                'class': 'ducky-input form-control answer-text-input',
                'placeholder': 'Texto de la alternativa de respuesta...',
            }),
            'is_correct': forms.CheckboxInput(attrs={
                'class': 'ducky-checkbox form-check-input correct-answer-check',
            }),
            'order': forms.NumberInput(attrs={
                'class': 'ducky-input form-control',
                'min': 1,
            }),
        }


class BaseAnswerFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return

        valid_answers = 0
        correct_count = 0

        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get('DELETE', False):
                continue

            text = form.cleaned_data.get('text', '').strip()
            if text:
                valid_answers += 1
                if form.cleaned_data.get('is_correct', False):
                    correct_count += 1

        if valid_answers < 2 or valid_answers > 6:
            raise ValidationError(
                f"Cada pregunta debe tener entre 2 y 6 respuestas válidas. Has proporcionado {valid_answers}."
            )

        if correct_count != 1:
            raise ValidationError(
                f"Debes marcar EXACTAMENTE una respuesta como correcta. Has seleccionado {correct_count}."
            )


AnswerFormSet = inlineformset_factory(
    parent_model=Question,
    model=Answer,
    form=AnswerForm,
    formset=BaseAnswerFormSet,
    extra=4,
    min_num=2,
    max_num=6,
    validate_min=True,
    validate_max=True,
    can_delete=True
)

QuestionFormSet = inlineformset_factory(
    parent_model=Quiz,
    model=Question,
    form=QuestionForm,
    extra=1,
    can_delete=True
)
