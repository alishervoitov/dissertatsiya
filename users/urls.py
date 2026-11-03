from django.urls import path

from . import views

urlpatterns = [
    path("register/", views.RegisterView.as_view(), name="register"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("login/refresh/", views.SessionRefreshView.as_view(), name="login-refresh"),
    path("sessions/", views.SessionListView.as_view(), name="sessions"),
    path("sessions/<int:pk>/revoke/", views.SessionRevokeView.as_view(), name="session-revoke"),
    path("sessions/revoke-others/", views.SessionRevokeOthersView.as_view(), name="sessions-revoke-others"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("me/", views.MeView.as_view(), name="me"),
    path("users/", views.AdminUserListCreateView.as_view(), name="admin-users"),
    path("users/<int:pk>/", views.AdminUserDetailView.as_view(), name="admin-user-detail"),
    path("me/avatar/", views.AvatarUploadView.as_view(), name="avatar-upload"),
    path("password-reset/", views.PasswordResetRequestView.as_view(), name="password-reset"),
    path("password-reset-confirm/", views.PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
]