"""
Merge a differently-spelled grade into the spelling to keep, everywhere it is
used (e.g. `merge_grades "EN9=EN-9" "SAE1008=SAE 1008"`).

Product codes carry a permanent number per grade, so spelling variants
(EN9 / EN-9) must be merged *before* the first code is made, or the same steel
ends up with two numbers. For each VARIANT=KEEP pair this rewrites the grade on
every record that has one (coils, gate-entry lots, product codes, allowed
specs, orders, queries, quote lines) and removes the variant's GradeOption,
keeping the KEEP option (created if missing). Matching is case-insensitive.
Dry-run by default; pass --apply to change anything. All pairs run in one
transaction, so a clash (e.g. two product codes that would become identical)
changes nothing.
"""
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from materials.models import GradeOption


def _grade_models():
    for model in apps.get_app_config('materials').get_models():
        if model is not GradeOption and any(f.name == 'grade' for f in model._meta.get_fields()):
            yield model


class Command(BaseCommand):
    help = "Merge grade spelling variants (VARIANT=KEEP) across all records. Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument('pairs', nargs='+', help='VARIANT=KEEP, e.g. "EN9=EN-9"')
        parser.add_argument('--apply', action='store_true', help='Actually make the changes.')

    def handle(self, *args, **options):
        pairs = []
        for raw in options['pairs']:
            variant, sep, keep = (part.strip() for part in raw.partition('='))
            if not sep or not variant or not keep:
                raise CommandError(f'"{raw}" is not VARIANT=KEEP')
            if variant == keep:
                raise CommandError(f'"{raw}": both sides are the same')
            pairs.append((variant, keep))

        with transaction.atomic():
            for variant, keep in pairs:
                self._merge(variant, keep)
            if not options['apply']:
                transaction.set_rollback(True)
                self.stdout.write('Dry run: nothing changed. Re-run with --apply.')

    def _merge(self, variant, keep):
        keep_option = GradeOption.objects.filter(name__iexact=keep).first()
        if keep_option is None:
            keep_option = GradeOption.objects.create(name=keep)
            self.stdout.write(f'{keep}: option added')
        for model in _grade_models():
            count = model.objects.filter(grade__iexact=variant).update(grade=keep_option.name)
            if count:
                self.stdout.write(f'{variant} -> {keep_option.name}: {count} {model._meta.verbose_name} record(s)')
        removed = GradeOption.objects.filter(name__iexact=variant).exclude(pk=keep_option.pk).delete()[0]
        if removed:
            self.stdout.write(f'{variant}: option removed (kept {keep_option.name}, number {keep_option.number})')
