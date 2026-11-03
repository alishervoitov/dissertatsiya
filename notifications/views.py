from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import Role

from .models import Notification
from .services import ensure_rx_notices

MAX_ROWS = 50


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "kind", "params", "link", "is_read", "created_at"]


def _refresh(user):
    if user.role == Role.PATIENT:
        from appointments.services import today
        ensure_rx_notices(user, today())


class NotificationListView(APIView):
    """Foydalanuvchining o'z bildirishnomalari (boshqalarnikiga yo'l yo'q: filtr user=request.user)."""

    def get(self, request):
        _refresh(request.user)
        qs = Notification.objects.filter(user=request.user)
        return Response({
            "results": NotificationSerializer(qs[:MAX_ROWS], many=True).data,
            "unread": qs.filter(is_read=False).count(),
        })


class NotificationCountView(APIView):
    def get(self, request):
        _refresh(request.user)
        return Response({"unread": Notification.objects.filter(user=request.user, is_read=False).count()})


class NotificationReadView(APIView):
    def post(self, request, pk):
        n = get_object_or_404(Notification, pk=pk, user=request.user)
        if not n.is_read:
            n.is_read = True
            n.save(update_fields=["is_read"])
        return Response(NotificationSerializer(n).data)


class NotificationReadAllView(APIView):
    def post(self, request):
        updated = Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"updated": updated})
