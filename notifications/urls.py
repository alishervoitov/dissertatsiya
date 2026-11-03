from django.urls import path

from . import views

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="notifications"),
    path("count/", views.NotificationCountView.as_view(), name="notifications-count"),
    path("read-all/", views.NotificationReadAllView.as_view(), name="notifications-read-all"),
    path("<int:pk>/read/", views.NotificationReadView.as_view(), name="notifications-read"),
]
