"""Routes for the local library installation."""

from allauth.account import views as accounts
from django.urls import path
from django.views.generic import RedirectView

from app import library, library_discovery, library_folders, library_steam

urlpatterns = [
    path(
        "", RedirectView.as_view(pattern_name="library", permanent=False), name="home"
    ),
    path("library/", library.shelf, name="library"),
    path("library/add/", library.capture, name="library_capture"),
    path("library/folders/", library_folders.manage, name="library_folders"),
    path(
        "library/recommendations/",
        library_discovery.suggestions,
        name="library_recommendations",
    ),
    path(
        "library/recommendations/choose/",
        library_discovery.choose,
        name="library_recommendation_choose",
    ),
    path("library/steam/", library_steam.connection, name="library_steam"),
    path("library/source/", library.source, name="library_source"),
    path("library/<str:kind>/<int:record_id>/", library.detail, name="library_detail"),
    path("accounts/login/", accounts.login, name="account_login"),
    path("accounts/logout/", accounts.logout, name="account_logout"),
    path("accounts/signup/", accounts.signup, name="account_signup"),
]
