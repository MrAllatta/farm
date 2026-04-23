"""Bootstrap or repair staff admin accounts from environment."""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Ensure FARM_ADMIN_USERNAMES exist as active staff superusers. "
        "Optional FARM_ADMIN_BOOTSTRAP_PASSWORD sets password when creating "
        "users or when --sync-password is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--sync-password",
            action="store_true",
            help="Apply FARM_ADMIN_BOOTSTRAP_PASSWORD to existing users (use sparingly).",
        )

    def handle(self, *args, **options):
        raw = os.environ.get("FARM_ADMIN_USERNAMES", "").strip()
        if not raw:
            raise CommandError(
                "FARM_ADMIN_USERNAMES is not set. "
                "Use a comma-separated list, e.g. FARM_ADMIN_USERNAMES=teacher,sam"
            )
        usernames = [u.strip() for u in raw.split(",") if u.strip()]
        password = os.environ.get("FARM_ADMIN_BOOTSTRAP_PASSWORD", "").strip()
        sync_password = options["sync_password"]

        if sync_password and not password:
            raise CommandError("--sync-password requires FARM_ADMIN_BOOTSTRAP_PASSWORD in the environment.")

        User = get_user_model()
        for username in usernames:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@farm.invalid",
                    "is_active": True,
                },
            )
            user.is_staff = True
            user.is_superuser = True
            user.is_active = True
            if password and (created or sync_password):
                user.set_password(password)
            user.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Ensured staff admin {username!r} (created={created}, password_updated="
                    f"{bool(password and (created or sync_password))})"
                )
            )

        if not password:
            self.stdout.write(
                self.style.WARNING(
                    "FARM_ADMIN_BOOTSTRAP_PASSWORD not set: passwords were not changed. "
                    "New users may need a password via Django admin or a later run with the env var."
                )
            )
