from django import forms
from django.core.exceptions import ValidationError
from .models import Game


class JoinGameForm(forms.Form):
    code = forms.CharField(
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            'class': 'ducky-input form-control text-center fs-2 fw-bold tracking-widest',
            'placeholder': '000000',
            'autocomplete': 'off',
            'inputmode': 'numeric',
            'pattern': '[0-9]{6}',
            'autofocus': True,
        }),
        label="Código PIN de la Sala"
    )

    def clean_code(self):
        code = self.cleaned_data.get('code', '').strip()
        if not code.isdigit() or len(code) != 6:
            raise ValidationError("El PIN debe ser un código numérico de exactamente 6 dígitos.")

        game = Game.objects.filter(code=code).first()
        if not game:
            raise ValidationError("No existe ninguna sala con el código PIN ingresado.")

        if game.status != Game.Status.LOBBY:
            if game.status == Game.Status.RUNNING:
                raise ValidationError("Esta partida ya ha comenzado y no admite nuevos participantes.")
            elif game.status == Game.Status.FINISHED:
                raise ValidationError("Esta partida ya ha finalizado.")
            elif game.status == Game.Status.CANCELLED:
                raise ValidationError("El docente canceló esta sala.")
            else:
                raise ValidationError("Esta sala no está disponible.")

        self.cleaned_data['game'] = game
        return code
