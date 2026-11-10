from django.urls import path

from . import mfa_views, views

urlpatterns = [
    path("register/", views.RegisterView.as_view(), name="register"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("login/2fa/", mfa_views.MfaLoginView.as_view(), name="login-2fa"),
    path("login/set-password/", views.TempPasswordChangeView.as_view(), name="login-set-password"),
    path("2fa/", mfa_views.MfaStatusView.as_view(), name="mfa-status"),
    path("2fa/setup/", mfa_views.MfaSetupView.as_view(), name="mfa-setup"),
    path("2fa/confirm/", mfa_views.MfaConfirmView.as_view(), name="mfa-confirm"),
    path("2fa/backup-codes/", mfa_views.MfaBackupCodesView.as_view(), name="mfa-backup-codes"),
    path("2fa/disable/", mfa_views.MfaDisableView.as_view(), name="mfa-disable"),
    path("users/<int:pk>/2fa/reset/", mfa_views.AdminMfaResetView.as_view(), name="admin-mfa-reset"),
    path("login/refresh/", views.SessionRefreshView.as_view(), name="login-refresh"),
    path("sessions/", views.SessionListView.as_view(), name="sessions"),
    path("sessions/<int:pk>/revoke/", views.SessionRevokeView.as_view(), name="session-revoke"),
    path("sessions/revoke-others/", views.SessionRevokeOthersView.as_view(), name="sessions-revoke-others"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("me/", views.MeView.as_view(), name="me"),
    path("password/change/", views.PasswordChangeView.as_view(), name="password-change"),
    path("email/send-code/", views.EmailSendCodeView.as_view(), name="email-send-code"),
    path("email/verify/", views.EmailVerifyView.as_view(), name="email-verify"),
    path("users/", views.AdminUserListCreateView.as_view(), name="admin-users"),
    path("users/<int:pk>/", views.AdminUserDetailView.as_view(), name="admin-user-detail"),
    path("me/avatar/", views.AvatarUploadView.as_view(), name="avatar-upload"),
    path("password-reset/", views.PasswordResetRequestView.as_view(), name="password-reset"),
    path("password-reset-confirm/", views.PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
]