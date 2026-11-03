from django.urls import path

from . import views

urlpatterns = [
    path("alerts/", views.AlertListView.as_view(), name="alerts"),
    path("alerts/count/", views.AlertCountView.as_view(), name="alerts-count"),
    path("alerts/<int:pk>/ack/", views.AlertAckView.as_view(), name="alerts-ack"),
]
