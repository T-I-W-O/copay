"""
Place this file at:
    yourapp/management/commands/migrate_service_images_to_cloudinary.py

(also make sure these exist, even if empty:
    yourapp/management/__init__.py
    yourapp/management/commands/__init__.py)

Run:
    python manage.py migrate_service_images_to_cloudinary --dry-run
    python manage.py migrate_service_images_to_cloudinary
    python manage.py migrate_service_images_to_cloudinary --delete-local

IMPORTANT: Cloudinary storage must already be configured in settings.py
(see the settings snippet in the chat) BEFORE running this command, because
obj.image.save() uploads through whatever storage backend is active.
"""
import os

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand

from core.models import Service  # <-- change "yourapp" to your app name


class Command(BaseCommand):
    help = "Upload locally saved Service images to Cloudinary and update the DB."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be uploaded without changing anything.",
        )
        parser.add_argument(
            "--delete-local",
            action="store_true",
            help="Delete the local file after a successful upload.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        delete_local = options["delete_local"]

        services = Service.objects.exclude(image="").exclude(image__isnull=True)
        total = services.count()
        self.stdout.write(f"Found {total} service(s) with an image.\n")

        uploaded = skipped = failed = 0

        for obj in services:
            old_name = obj.image.name
            local_path = os.path.join(settings.MEDIA_ROOT, old_name)

            if not os.path.isfile(local_path):
                self.stdout.write(
                    self.style.WARNING(
                        f"[SKIP] {obj.name}: no local file at {local_path}"
                    )
                )
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(f"[DRY]  {obj.name}: would upload {local_path}")
                continue

            try:
                with open(local_path, "rb") as f:
                    # Saves through the active storage (Cloudinary) and
                    # updates obj.image to the new Cloudinary reference.
                    obj.image.save(os.path.basename(old_name), File(f), save=True)

                self.stdout.write(
                    self.style.SUCCESS(f"[OK]   {obj.name}: {obj.image.url}")
                )
                uploaded += 1

                if delete_local:
                    os.remove(local_path)

            except Exception as exc:
                failed += 1
                self.stdout.write(
                    self.style.ERROR(f"[FAIL] {obj.name}: {exc}")
                )

        self.stdout.write(
            f"\nDone. Uploaded: {uploaded}, skipped: {skipped}, failed: {failed}"
        )