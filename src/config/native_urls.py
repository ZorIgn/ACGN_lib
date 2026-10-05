"""Routes for the local library installation."""

from allauth.account import views as accounts
from django.urls import path
from django.views.generic import RedirectView

from app import (
    library,
    library_bangumi,
    library_bulk,
    library_clip,
    library_discovery,
    library_folders,
    library_merge,
    library_series,
    library_steam,
    library_transfer,
)

urlpatterns = [
    path(
        "", RedirectView.as_view(pattern_name="library", permanent=False), name="home"
    ),
    path("library/", library.shelf, name="library"),
    path("library/bulk/", library_bulk.edit, name="library_bulk"),
    path("library/data/", library_transfer.transfer, name="library_transfer"),
    path("library/clip/", library_clip.clip, name="library_clip"),
    path("library/bangumi/", library_bangumi.bangumi_import, name="library_bangumi"),
    path("library/series/", library_series.manage, name="library_series"),
    path("library/merge/", library_merge.merge, name="library_merge"),
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
    path(
        "library/recommendations/feedback/",
        library_discovery.feedback,
        name="library_recommendation_feedback",
    ),
    path("library/steam/", library_steam.connection, name="library_steam"),
    path("library/source/", library.source, name="library_source"),
    path("library/<str:kind>/<int:record_id>/", library.detail, name="library_detail"),
    path("accounts/login/", accounts.login, name="account_login"),
    path("accounts/logout/", accounts.logout, name="account_logout"),
    path("accounts/signup/", accounts.signup, name="account_signup"),
]
