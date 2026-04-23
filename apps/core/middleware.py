"""core/middleware.py"""

from django.contrib.auth.middleware import LoginRequiredMiddleware


class FarmLoginRequiredMiddleware(LoginRequiredMiddleware):
    """
    Require login for the main app while leaving probes, static assets, and
    Django admin (which performs its own authentication) reachable.
    """

    _public_prefixes = (
        "/static/",
        "/admin/",
        "/healthz/",
        "/readyz/",
        "/accounts/login",
        "/accounts/logout",
    )

    def process_view(self, request, view_func, view_args, view_kwargs):
        path = request.path
        if any(path.startswith(p) for p in self._public_prefixes):
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)
