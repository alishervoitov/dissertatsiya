from rest_framework import serializers

from .models import DoctorProfile, QueueTicket
from .services import queue_tz


def _local_iso(dt):
    return dt.astimezone(queue_tz()).isoformat(timespec="minutes") if dt else None


def doctor_name(user):
    return user.get_full_name() or user.username


class DoctorQueueSerializer(serializers.Serializer):
    """Navbat olish uchun shifokorlar ro'yxati: faqat ommaviy ma'lumot."""

    def to_representation(self, state):
        p = state.profile
        return {
            "id": p.user_id,
            "name": doctor_name(p.user),
            "specialty": p.specialty,
            "room": p.room,
            "work_start": p.work_start.strftime("%H:%M"),
            "work_end": p.work_end.strftime("%H:%M"),
            "waiting": len(state.present),
            "pending_online": len(state.pending),
            "current_number": state.current.number if state.current else None,
            "avg_minutes": round(state.minutes),
            "next_eta": _local_iso(state.next_eta),
            "accepting": state.accepting,
            # Shifoxonada (registratura, kiosk) chipta olayotgan bemor uchun: faqat kelganlar hisobga olinadi
            "next_eta_walkin": _local_iso(state.next_eta_walkin),
            "accepting_walkin": state.accepting_walkin,
        }


class TicketSerializer(serializers.Serializer):
    """Navbat chiptasi: raqam, holat, o'rin va taxminiy vaqt."""

    def to_representation(self, item):
        ticket, state = item
        eta = state.eta_for(ticket) if state else None
        if ticket.status == QueueTicket.CALLED:
            eta = ticket.called_at
        return {
            "id": ticket.id,
            "number": ticket.number,
            "date": ticket.date.isoformat(),
            "status": ticket.status,
            "source": ticket.source,
            "arrived": ticket.arrived,
            "doctor": {
                "id": ticket.doctor_id,
                "name": doctor_name(ticket.doctor),
                "specialty": getattr(getattr(ticket.doctor, "doctor_profile", None), "specialty", ""),
                "room": getattr(getattr(ticket.doctor, "doctor_profile", None), "room", ""),
            },
            "position": state.position_of(ticket) if state else None,
            "ahead": (state.position_of(ticket) - 1) if state and state.position_of(ticket) else 0,
            "eta": _local_iso(eta),
            "created_at": _local_iso(ticket.created_at),
        }


class DoctorTicketSerializer(serializers.Serializer):
    """Shifokor va registratura uchun: kim navbatda turgani ham ko'rinadi."""

    def to_representation(self, item):
        ticket, state = item
        if ticket.patient_id:
            u = ticket.patient.user
            who = u.get_full_name() or u.username
        else:
            who = ticket.visitor_name or ""
        eta = state.eta_for(ticket) if state and ticket.status == QueueTicket.WAITING else None
        return {
            "id": ticket.id,
            "number": ticket.number,
            "status": ticket.status,
            "source": ticket.source,
            "arrived": ticket.arrived,
            "name": who,
            "patient_id": ticket.patient_id,
            "doctor_id": ticket.doctor_id,
            "eta": _local_iso(eta),
            "arrived_at": _local_iso(ticket.arrived_at),
            "called_at": _local_iso(ticket.called_at),
            "finished_at": _local_iso(ticket.finished_at),
        }


class DoctorProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorProfile
        fields = ["specialty", "room", "work_start", "work_end", "avg_minutes", "accepts_queue"]

    def validate(self, attrs):
        start = attrs.get("work_start", getattr(self.instance, "work_start", None))
        end = attrs.get("work_end", getattr(self.instance, "work_end", None))
        if start and end and start >= end:
            raise serializers.ValidationError({"work_end": "Ish tugash vaqti boshlanishidan keyin bo'lishi kerak."})
        return attrs


class TakeTicketSerializer(serializers.Serializer):
    doctor = serializers.IntegerField()
    visitor_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
