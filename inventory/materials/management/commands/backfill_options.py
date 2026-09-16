"""
Backfill GradeOption/SizeOption from every distinct grade/size already
present in Material, so the New Coil Entry / Gate Entry picker includes
everything historically used, not just what's been manually configured.

Adds values as-is — if the historical data spells the same grade two
different ways (e.g. "EN8D" and "EN-8D"), both become separate, pickable
options. That's a data-quality call for an admin to clean up afterward via
the Grade/Size Option admin pages, not something this command resolves on
its own.
"""
from django.core.management.base import BaseCommand
from materials.models import Material, GradeOption, SizeOption


class Command(BaseCommand):
    help = "Backfill GradeOption/SizeOption from distinct grade/size values already in Material."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Report what would be added without changing anything.",
        )

    def handle(self, *args, **options):
        existing_grades = set(GradeOption.objects.values_list('name', flat=True))
        existing_sizes = set(SizeOption.objects.values_list('value', flat=True))

        material_grades = set(
            Material.objects.exclude(grade__isnull=True).exclude(grade='').values_list('grade', flat=True)
        )
        material_sizes = set(Material.objects.exclude(size__isnull=True).values_list('size', flat=True))

        new_grades = sorted(material_grades - existing_grades)
        too_long = [g for g in new_grades if len(g) > 20]
        new_grades = [g for g in new_grades if len(g) <= 20]
        new_sizes = sorted(material_sizes - existing_sizes)

        if options['dry_run']:
            self.stdout.write(f"Would add {len(new_grades)} grade option(s): {new_grades}")
            self.stdout.write(f"Would add {len(new_sizes)} size option(s): {new_sizes}")
            if too_long:
                self.stdout.write(self.style.WARNING(
                    f"Skipping {len(too_long)} grade(s) longer than 20 characters: {too_long}"
                ))
            return

        GradeOption.objects.bulk_create([GradeOption(name=g) for g in new_grades])
        SizeOption.objects.bulk_create([SizeOption(value=s) for s in new_sizes])

        self.stdout.write(self.style.SUCCESS(
            f"Added {len(new_grades)} grade option(s) and {len(new_sizes)} size option(s)."
        ))
        if too_long:
            self.stdout.write(self.style.WARNING(
                f"Skipped {len(too_long)} grade(s) longer than 20 characters: {too_long}"
            ))
