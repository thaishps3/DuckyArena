from django.contrib import admin
from django.db.models import Count
from .models import Quiz, Question, Answer


class AnswerInline(admin.TabularInline):
    model = Answer
    extra = 4
    fields = ('text', 'is_correct', 'order')
    ordering = ('order',)


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 1
    show_change_link = True
    fields = ('text', 'time_limit', 'points', 'difficulty', 'order')
    ordering = ('order',)


@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    list_display = ('title', 'creator', 'is_published', 'created_at', 'get_questions_count')
    list_filter = ('is_published', 'created_at')
    search_fields = ('title', 'creator__username', 'description')
    list_editable = ('is_published',)
    list_select_related = ('creator',)
    inlines = [QuestionInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(questions_total=Count('questions'))

    @admin.display(description="Total preguntas", ordering='questions_total')
    def get_questions_count(self, obj):
        return obj.questions_total


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ('text', 'quiz', 'time_limit', 'points', 'difficulty', 'order')
    list_filter = ('quiz', 'time_limit', 'difficulty')
    search_fields = ('text', 'quiz__title')
    list_select_related = ('quiz',)
    inlines = [AnswerInline]


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ('text', 'question', 'is_correct', 'order')
    list_filter = ('is_correct', 'question__quiz')
    search_fields = ('text', 'question__text')
    list_editable = ('is_correct',)
    list_select_related = ('question',)
