from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse_lazy, reverse
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.forms import inlineformset_factory
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Sum
from .models import Quiz, Question, Answer
from .forms import QuizForm, QuestionForm, AnswerForm, AnswerFormSet, QuestionFormSet, BaseAnswerFormSet


class TeacherRequiredMixin(UserPassesTestMixin):
    """Garantiza que el usuario autenticado tenga el rol TEACHER o permisos de administración."""
    def test_func(self):
        user = self.request.user
        if not user.is_authenticated:
            return False
        if user.is_superuser or user.is_staff:
            return True
        return hasattr(user, 'profile') and user.profile.role == 'TEACHER'

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            raise PermissionDenied("Se requiere rol de PROFESOR (TEACHER) para realizar esta acción.")
        return super().handle_no_permission()


class QuizOwnerRequiredMixin(TeacherRequiredMixin):
    """Garantiza que el usuario sea profesor y el legítimo creador del cuestionario."""
    def get_object(self, queryset=None):
        if hasattr(super(), 'get_object'):
            return super().get_object(queryset)
        pk = self.kwargs.get('pk')
        return get_object_or_404(Quiz, pk=pk)

    def test_func(self):
        if not super().test_func():
            return False
        if self.request.user.is_superuser:
            return True
        quiz = self.get_object()
        return quiz.creator == self.request.user


class QuizListView(LoginRequiredMixin, ListView):
    model = Quiz
    template_name = 'quizzes/quiz_list.html'
    context_object_name = 'quizzes'
    paginate_by = 12

    def get_queryset(self):
        # Muestra cuestionarios publicados, optimizando lecturas de usuario
        return Quiz.objects.filter(is_published=True).select_related('creator').annotate(
            total_questions=Count('questions')
        ).order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        # Si es profesor o superuser, añade la lista de sus propios cuestionarios (borradores y publicados)
        is_teacher = user.is_authenticated and (user.is_superuser or user.is_staff or (hasattr(user, 'profile') and user.profile.role == 'TEACHER'))
        if is_teacher:
            context['my_quizzes'] = Quiz.objects.filter(creator=user).annotate(
                total_questions=Count('questions')
            ).order_by('-created_at')
            context['is_teacher'] = True
        else:
            context['my_quizzes'] = []
            context['is_teacher'] = False
        return context


class QuizDetailView(LoginRequiredMixin, DetailView):
    model = Quiz
    template_name = 'quizzes/quiz_detail.html'
    context_object_name = 'quiz'

    def get_queryset(self):
        return Quiz.objects.select_related('creator').prefetch_related(
            'questions__answers'
        ).annotate(
            total_questions=Count('questions', distinct=True),
            total_points=Sum('questions__points'),
            total_time=Sum('questions__time_limit')
        )

    def get_object(self, queryset=None):
        quiz = super().get_object(queryset)
        # Solo el creador puede visualizar cuestionarios no publicados
        if not quiz.is_published and quiz.creator != self.request.user:
            raise PermissionDenied("Este cuestionario está en borrador y solo puede ser visto por su creador.")
        return quiz

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        # Solo el creador (o superusuario) ve las preguntas; el resto las descubre jugando
        context['can_view_questions'] = (self.object.creator == user) or user.is_superuser
        # Minutos estimados (se calcula aqui porque la plantilla no sabe dividir)
        total_sec = self.object.total_time or 0
        context['estimated_minutes'] = round(total_sec / 60)
        return context


class QuizFormsetMixin:
    """Provee soporte para la gestión transaccional de Quiz -> Question -> Answer."""

    def get_question_formset(self, data=None):
        instance = getattr(self, 'object', None)
        extra = 0 if (instance and instance.pk and instance.questions.exists()) else 1
        formset_class = inlineformset_factory(
            parent_model=Quiz,
            model=Question,
            form=QuestionForm,
            extra=extra,
            can_delete=True
        )
        return formset_class(data, instance=instance, prefix='questions')

    def get_answer_formsets(self, question_formset, data=None):
        answer_formsets = []
        for q_form in question_formset:
            prefix = f'answers_{q_form.prefix}'
            q_instance = q_form.instance if (q_form.instance and q_form.instance.pk) else None
            extra = 0 if (q_instance and q_instance.pk and q_instance.answers.exists()) else 4
            ans_formset_class = inlineformset_factory(
                parent_model=Question,
                model=Answer,
                form=AnswerForm,
                formset=BaseAnswerFormSet,
                extra=extra,
                min_num=2,
                max_num=6,
                validate_min=True,
                validate_max=True,
                can_delete=True
            )
            ans_formset = ans_formset_class(data, instance=q_instance, prefix=prefix)
            q_form.answer_formset = ans_formset
            answer_formsets.append(ans_formset)
        return answer_formsets

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if 'question_formset' not in context:
            context['question_formset'] = self.get_question_formset()
        if 'answer_formsets' not in context:
            context['answer_formsets'] = self.get_answer_formsets(context['question_formset'])
        
        # Blueprint vacío para preguntas y respuestas nuevas vía JavaScript
        dummy_question = Question(order=1)
        dummy_q_form = QuestionForm(prefix='questions-__prefix__', instance=dummy_question)
        dummy_ans_formset = AnswerFormSet(
            instance=dummy_question,
            prefix='answers_questions-__prefix__'
        )
        context['empty_question_form'] = dummy_q_form
        context['empty_answer_formset'] = dummy_ans_formset
        return context

    def process_formsets(self, form):
        question_formset = self.get_question_formset(self.request.POST)
        answer_formsets = self.get_answer_formsets(question_formset, self.request.POST)

        form_valid = form.is_valid()
        q_valid = question_formset.is_valid()

        # Validar formsets de respuestas de preguntas activas
        ans_valid = True
        for q_form in question_formset:
            if not q_form.cleaned_data.get('DELETE', False):
                if q_form.has_changed() or q_form.instance.pk or q_form.cleaned_data.get('text'):
                    if hasattr(q_form, 'answer_formset') and not q_form.answer_formset.is_valid():
                        ans_valid = False

        if form_valid and q_valid and ans_valid:
            with transaction.atomic():
                self.object = form.save(commit=False)
                self.object.creator = self.request.user
                self.object.save()

                question_formset.instance = self.object
                for q_form in question_formset:
                    if q_form.cleaned_data.get('DELETE', False):
                        if q_form.instance.pk:
                            q_form.instance.delete()
                    elif q_form.cleaned_data and (q_form.has_changed() or q_form.instance.pk or q_form.cleaned_data.get('text')):
                        question = q_form.save(commit=False)
                        question.quiz = self.object
                        question.save()

                        if hasattr(q_form, 'answer_formset'):
                            ans_formset = q_form.answer_formset
                            ans_formset.instance = question
                            ans_formset.save()

            messages.success(self.request, "Cuestionario y preguntas guardados con éxito.")
            return redirect(self.get_success_url())
        else:
            messages.error(self.request, "Por favor corrige los errores señalados en el formulario.")
            return self.render_to_response(
                self.get_context_data(
                    form=form,
                    question_formset=question_formset,
                    answer_formsets=answer_formsets
                )
            )


class QuizCreateView(TeacherRequiredMixin, QuizFormsetMixin, CreateView):
    model = Quiz
    form_class = QuizForm
    template_name = 'quizzes/quiz_form.html'

    def get(self, request, *args, **kwargs):
        self.object = None
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = None
        form = self.get_form()
        return self.process_formsets(form)

    def get_success_url(self):
        return reverse('quizzes:quiz_detail', kwargs={'pk': self.object.pk})


class QuizUpdateView(QuizOwnerRequiredMixin, QuizFormsetMixin, UpdateView):
    model = Quiz
    form_class = QuizForm
    template_name = 'quizzes/quiz_form.html'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        return self.process_formsets(form)

    def get_success_url(self):
        return reverse('quizzes:quiz_detail', kwargs={'pk': self.object.pk})


class QuizDeleteView(QuizOwnerRequiredMixin, DeleteView):
    model = Quiz
    template_name = 'quizzes/quiz_confirm_delete.html'
    success_url = reverse_lazy('quizzes:quiz_list')

    def delete(self, request, *args, **kwargs):
        messages.success(self.request, "Cuestionario eliminado con éxito.")
        return super().delete(request, *args, **kwargs)


class QuizTogglePublishView(QuizOwnerRequiredMixin, View):
    """Alterna el estado de publicación validando condiciones pedagógicas de negocio."""
    def post(self, request, pk, *args, **kwargs):
        quiz = get_object_or_404(Quiz, pk=pk)
        
        if quiz.creator != request.user:
            raise PermissionDenied("No eres el propietario de este cuestionario.")

        if not quiz.is_published:
            # Validación estricta para permitir la publicación
            questions = quiz.questions.prefetch_related('answers').all()
            if not questions.exists():
                messages.error(request, "No puedes publicar un cuestionario sin preguntas.")
                return redirect('quizzes:quiz_detail', pk=quiz.pk)

            for q in questions:
                valid_answers = [a for a in q.answers.all()]
                correct_answers = [a for a in valid_answers if a.is_correct]
                if len(valid_answers) < 2 or len(valid_answers) > 6 or len(correct_answers) != 1:
                    messages.error(
                        request,
                        f"La pregunta '{q.text[:30]}...' no cumple el requisito de tener entre 2 y 6 respuestas con exactamente una correcta."
                    )
                    return redirect('quizzes:quiz_detail', pk=quiz.pk)

            quiz.is_published = True
            quiz.save(update_fields=['is_published', 'updated_at'])
            messages.success(request, f"¡El cuestionario '{quiz.title}' ha sido publicado con éxito!")
        else:
            quiz.is_published = False
            quiz.save(update_fields=['is_published', 'updated_at'])
            messages.info(request, f"El cuestionario '{quiz.title}' ahora está en modo borrador.")

        return redirect('quizzes:quiz_detail', pk=quiz.pk)
