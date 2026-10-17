from django.core.management.base import BaseCommand, CommandError

from materials import access_codes


class Command(BaseCommand):
    help = ("Set the employee PIN or the finance code from the PC (for when every owner has forgotten it): "
            "manage.py reset_access_code employee 1234. Signs out everybody signed in with the old code.")

    def add_arguments(self, parser):
        parser.add_argument('role', choices=access_codes.ROLES)
        parser.add_argument('code', help="Exactly 4 digits, and different from the other role's code.")

    def handle(self, *args, **options):
        role, code = options['role'], options['code']
        error = access_codes.validate_new_code(role, code)
        if error:
            raise CommandError(error)
        changed = access_codes.set_code(role, code)
        self.stdout.write(self.style.SUCCESS(
            f"{access_codes.LABELS[role]} changed; everybody signed in with the old one is signed out."
            if changed else f"That is already the {access_codes.LABELS[role]}; nothing changed."))
