from rest_framework import serializers

from .models import SecurityAlert


class SecurityAlertSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    acknowledged_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SecurityAlert
        fields = ["id", "rule", "severity", "status", "actor", "actor_name", "actor_role", "count", "detail",
                  "first_seen", "last_seen", "acknowledged_by_name", "acknowledged_at"]

    def get_actor_name(self, obj):
        return (obj.actor.get_full_name() or obj.actor.username) if obj.actor else None

    def get_acknowledged_by_name(self, obj):
        u = obj.acknowledged_by
        return (u.get_full_name() or u.username) if u else None
