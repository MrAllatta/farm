"""core/forms.py"""

from django import forms
from django.contrib.auth.forms import AuthenticationForm


class StaffAuthenticationForm(AuthenticationForm):
    """Reject non-staff accounts at login time (operational admin surface)."""

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise forms.ValidationError(
                "This account is not authorized for staff access.",
                code="not_staff",
            )
