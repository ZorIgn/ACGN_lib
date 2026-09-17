from allauth.account.adapter import DefaultAccountAdapter
from django.contrib.auth import get_user_model


class FirstUserAccountAdapter(DefaultAccountAdapter):
    """Allow the owner to create the initial local account."""

    def is_open_for_signup(self, request):  # noqa: ARG002
        """Close registration once an account exists."""
        return not get_user_model().objects.exists()

class NoNewUsersAccountAdapter(DefaultAccountAdapter):
    """Custom account adapter to prevent new users from signing up."""

    def is_open_for_signup(self, request):  # noqa: ARG002
        """Check whether or not the site is open for signups."""
        return False
