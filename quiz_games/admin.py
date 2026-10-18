from django.contrib import admin
from django.db.models import Count
from .models import Game, GamePlayer, PlayerAnswer


class GamePlayerInline(admin.TabularInline):
    model = GamePlayer
    extra = 0
    can_delete = False
    readonly_fields = ('player', 'score', 'correct_answers', 'current_streak', 'joined_at')
    ordering = ('-score',)


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ('code', 'quiz', 'host', 'status', 'current_question', 'created_at', 'get_players_count')
    list_filter = ('status', 'created_at')
    search_fields = ('code', 'quiz__title', 'host__username')
    readonly_fields = ('code', 'created_at')
    list_select_related = ('quiz', 'host')
    inlines = [GamePlayerInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(players_total=Count('players'))

    @admin.display(description="Nº Jugadores", ordering='players_total')
    def get_players_count(self, obj):
        return obj.players_total


@admin.register(GamePlayer)
class GamePlayerAdmin(admin.ModelAdmin):
    list_display = ('player', 'game', 'score', 'correct_answers', 'current_streak', 'joined_at')
    list_filter = ('game__status', 'joined_at')
    search_fields = ('player__username', 'game__code')
    list_select_related = ('player', 'game')
    readonly_fields = ('joined_at',)


@admin.register(PlayerAnswer)
class PlayerAnswerAdmin(admin.ModelAdmin):
    list_display = ('game_player', 'question', 'selected_answer', 'is_correct', 'points_awarded', 'response_time', 'answered_at')
    list_filter = ('is_correct', 'answered_at')
    search_fields = ('game_player__player__username', 'question__text')
    list_select_related = ('game_player__player', 'question', 'selected_answer')
    readonly_fields = ('game_player', 'question', 'selected_answer', 'is_correct', 'points_awarded', 'response_time', 'answered_at')
