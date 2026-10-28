from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("submissions/", views.history, name="history"),
    path("submissions/clear/", views.clear_submissions, name="clear_submissions"),
    path("submissions/<int:pk>/", views.submission, name="submission"),
    path("submissions/<int:pk>/status/", views.submission_status, name="submission_status"),
    path("submissions/<int:pk>/regrade/", views.regrade, name="regrade"),
    path("submissions/<int:pk>/report.<str:fmt>", views.submission_download, name="download"),
    path("rules/", views.rules_list, name="rules"),
    path("rules/new/", views.rules_edit, name="rules_new"),
    path("rules/<str:name>/", views.rules_edit, name="rules_edit"),
    path("api/grade/", views.api_grade, name="api_grade"),
]
