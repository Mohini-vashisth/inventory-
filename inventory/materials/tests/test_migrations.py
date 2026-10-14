"""The product code / product type rename migrations (0058, 0059) rename the records Django keeps for the admin
(content types and permissions) without touching the data tables."""

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [('materials', '0057_gate_bills_and_non_coil_entries')]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


def _latest():
    return [node for node in MigrationExecutor(connection).loader.graph.leaf_nodes() if node[0] == 'materials']


class RenameKeepsAdminPermissionsTests(TransactionTestCase):
    """Walks the real migrations back to before the rename and forward again, on a database that has staff users
    with permissions on the two models, and checks every permission stays attached to the right model."""

    def tearDown(self):
        _migrate(_latest())   # leave the test database fully migrated for whatever runs next

    def test_permissions_follow_the_renamed_models(self):
        _migrate(BEFORE)
        # Before the rename the CODE model was called producttype and the TYPE model productcategory.
        code_ct = ContentType.objects.get(app_label='materials', model='producttype')
        type_ct = ContentType.objects.get(app_label='materials', model='productcategory')
        code_perm = Permission.objects.get(content_type=code_ct, codename='change_producttype')
        type_perm = Permission.objects.get(content_type=type_ct, codename='change_productcategory')
        user = User.objects.create_user('perm_user', password='pw', is_staff=True)
        user.user_permissions.add(code_perm, type_perm)

        _migrate(_latest())

        code_perm.refresh_from_db()
        type_perm.refresh_from_db()
        self.assertEqual(ContentType.objects.get(pk=code_ct.pk).model, 'productcode')
        self.assertEqual(code_perm.codename, 'change_productcode')
        self.assertEqual(ContentType.objects.get(pk=type_ct.pk).model, 'producttype')
        self.assertEqual(type_perm.codename, 'change_producttype')
        held = set(User.objects.get(pk=user.pk).user_permissions.values_list('pk', flat=True))
        self.assertEqual(held, {code_perm.pk, type_perm.pk})   # the user keeps both permissions

    def test_the_data_tables_keep_their_old_names(self):
        tables = connection.introspection.table_names()
        self.assertIn('materials_producttype', tables)       # the code table
        self.assertIn('materials_productcategory', tables)   # the type table
        self.assertNotIn('materials_productcode', tables)
