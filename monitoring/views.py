from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from records.audit import log_audit
from users.permissions import IsAdmin

from .detector import config
from .models import SecurityAlert
from .serializers import SecurityAlertSerializer


class AlertListView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        qs = SecurityAlert.objects.select_related("actor", "acknowledged_by")
        status = request.query_params.get("status")
        if status in (SecurityAlert.OPEN, SecurityAlert.ACKED):
            qs = qs.filter(status=status)
        return Response({
            "results": SecurityAlertSerializer(qs[:300], many=True).data,
            "open_count": SecurityAlert.objects.filter(status=SecurityAlert.OPEN).count(),
            "rules": config(),
        })


class AlertCountView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response({"open_count": SecurityAlert.objects.filter(status=SecurityAlert.OPEN).count()})


class AlertAckView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        alert = get_object_or_404(SecurityAlert, pk=pk)
        if alert.status != SecurityAlert.ACKED:
            alert.status = SecurityAlert.ACKED
            alert.acknowledged_by = request.user
            alert.acknowledged_at = timezone.now()
            alert.save(update_fields=["status", "acknowledged_by", "acknowledged_at"])
            log_audit(request.user, "update", detail=f"Xavfsizlik ogohlantirishi ko'rib chiqildi: #{alert.id} {alert.rule}")
            if alert.auto_response == SecurityAlert.LOCKED and alert.actor:
                from .response import is_suspended
                if not is_suspended(alert.actor):
                    log_audit(request.user, "update", detail=f"Hisob qayta faollashtirildi: {alert.actor.username}")
        return Response(SecurityAlertSerializer(alert).data)
