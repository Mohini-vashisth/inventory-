"""The product code/type rename migrations rename the records Django keeps for the admin (content types and
permissions) without touching the data tables."""

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class RenameKeepsAdminPermissionsTests(TransactionTestCase):
    """Walks the real migration back and forward on a database that has a staff user with a permission on the
    product-code model, and checks the permission is still attached after the rename."""

    before = [('materials', '0057_gate_bills_and_non_coil_entries')]
    after = [('materials', '0058_rename_producttype_to_productcode')]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self._migrate(self.after)   # leave the test database at the latest state for whatever runs next

    def test_the_permission_follows_the_renamed_model(self):
        self._migrate(self.before)
        content_type, _ = ContentType.objects.get_or_create(app_label='materials', model='producttype')
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type, codename='change_producttype', defaults={'name': 'Can change product code'})
        user = User.objects.create_user('perm_user', password='pw', is_staff=True)
        user.user_permissions.add(permission)

        self._migrate(self.after)

        permission.refresh_from_db()
        self.assertEqual(permission.codename, 'change_productcode')
        self.assertEqual(ContentType.objects.get(pk=content_type.pk).model, 'productcode')
        self.assertTrue(User.objects.get(pk=user.pk).user_permissions.filter(pk=permission.pk).exists())

    def test_the_data_tables_are_not_renamed(self):
        tables = connection.introspection.table_names()
        self.assertIn('materials_producttype', tables)       # the code table keeps its name
        self.assertNotIn('materials_productcode', tables)
