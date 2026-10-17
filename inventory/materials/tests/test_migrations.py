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

    def test_the_tables_have_the_names_that_match_the_models(self):
        tables = connection.introspection.table_names()
        self.assertIn('materials_productcode', tables)       # the code table
        self.assertIn('materials_producttype', tables)       # the type table
        self.assertNotIn('materials_productcategory', tables)


class RealRenameKeepsRelationsTests(TransactionTestCase):
    """Migration 0063 really renames the tables and columns (the code table to materials_productcode, the type table
    to materials_producttype, and the foreign-key columns to match). Data built under the OLD schema must still point
    at the right rows afterwards, and going back and forward again must keep working."""

    OLD = [('materials', '0062_coil_thickness_readings')]

    def tearDown(self):
        _migrate(_latest())

    def _old_models(self):
        apps = MigrationExecutor(connection).loader.project_state(self.OLD).apps
        return apps.get_model

    def test_links_survive_the_rename_and_the_undo(self):
        _migrate(self.OLD)
        get = self._old_models()
        customer = get('materials', 'Customer').objects.create(name='Link Co')
        flat = get('materials', 'ProductType').objects.create(name='Flat Test', code='FTT')
        square = get('materials', 'ProductType').objects.create(name='Square Test', code='SQT')
        flat_code = get('materials', 'ProductCode').objects.create(item_code='FTT001', grade='EN8D', product_type=flat)
        square_code = get('materials', 'ProductCode').objects.create(item_code='SQT001', grade='EN8D', product_type=square)
        order = get('materials', 'Order').objects.create(customer=customer, product_code=flat_code, quantity=100, grade='EN8D')
        step = get('materials', 'ProcessStep').objects.create(product_code=square_code, name='Cut', order=1)
        spec = get('materials', 'AllowedCoilSpec').objects.create(product_code=flat_code, size='1.200')
        query = get('materials', 'Query').objects.create(source='other', contact_phone='919000000009')
        item = get('materials', 'QueryItem').objects.create(query=query, product_type=square, product_code=square_code)
        quotation = get('materials', 'Quotation').objects.create(customer=customer, status='draft')
        line = get('materials', 'QuotationLineItem').objects.create(
            quotation=quotation, description='x', product_type=flat, product_code=flat_code)

        _migrate(_latest())

        from ..models import AllowedCoilSpec, Order, ProcessStep, ProductCode, ProductType, QueryItem, QuotationLineItem
        self.assertEqual(ProductCode.objects.get(item_code='FTT001').product_type.name, 'Flat Test')
        self.assertEqual(ProductCode.objects.get(item_code='SQT001').product_type.name, 'Square Test')
        self.assertEqual(Order.objects.get(pk=order.pk).product_code.item_code, 'FTT001')
        self.assertEqual(ProcessStep.objects.get(pk=step.pk).product_code.item_code, 'SQT001')
        self.assertEqual(AllowedCoilSpec.objects.get(pk=spec.pk).product_code.item_code, 'FTT001')
        new_item = QueryItem.objects.get(pk=item.pk)
        self.assertEqual((new_item.product_type.name, new_item.product_code.item_code), ('Square Test', 'SQT001'))
        new_line = QuotationLineItem.objects.get(pk=line.pk)
        self.assertEqual((new_line.product_type.name, new_line.product_code.item_code), ('Flat Test', 'FTT001'))
        self.assertEqual(set(ProductType.objects.filter(code__in=['FTT', 'SQT']).values_list('name', flat=True)),
                         {'Flat Test', 'Square Test'})   # (the standard seven types are there too)
        self.assertEqual(ProductCode.objects.filter(item_code__in=['FTT001', 'SQT001']).count(), 2)

        # the tables and columns really carry the new names
        tables = connection.introspection.table_names()
        self.assertIn('materials_productcode', tables)
        self.assertIn('materials_producttype', tables)
        self.assertNotIn('materials_productcategory', tables)
        with connection.cursor() as cursor:
            def columns(table):
                return {col.name for col in connection.introspection.get_table_description(cursor, table)}
            self.assertIn('product_code_id', columns('materials_order'))
            self.assertNotIn('product_type_id', columns('materials_order'))
            self.assertEqual({'product_type_id', 'product_code_id'} <= columns('materials_queryitem'), True)
            self.assertNotIn('product_category_id', columns('materials_queryitem'))
            self.assertIn('product_type_id', columns('materials_productcode'))
            self.assertNotIn('category_id', columns('materials_productcode'))

        # and the migration can be undone and applied again without losing anything
        _migrate(self.OLD)
        get = self._old_models()
        self.assertEqual(get('materials', 'ProductCode').objects.get(item_code='SQT001').product_type.name, 'Square Test')
        self.assertEqual(get('materials', 'Order').objects.get(pk=order.pk).product_code.item_code, 'FTT001')
        _migrate(_latest())
        self.assertEqual(Order.objects.get(pk=order.pk).product_code.product_type.name, 'Flat Test')


class WhatsAppSavedKeysTests(TransactionTestCase):
    """Migration 0064 converts the product type key the WhatsApp bot saved in conversations (last_asked_field,
    edit_field and the held pending_value) from 'product_category' to 'product_type', and can convert it back."""

    BEFORE = [('materials', '0063_real_table_and_column_names')]

    def tearDown(self):
        _migrate(_latest())

    def test_saved_conversations_are_converted_and_converted_back(self):
        _migrate(self.BEFORE)
        Query = MigrationExecutor(connection).loader.project_state(self.BEFORE).apps.get_model('materials', 'Query')
        mid_question = Query.objects.create(source='whatsapp', contact_phone='919000000011', last_asked_field='product_category@1')
        mid_edit = Query.objects.create(
            source='whatsapp', contact_phone='919000000012', bot_stage='confirm_change', edit_field='item:2:product_category',
            pending_value={'_items': {'product_category': [4], 'grade': ['EN8D']}, '_item_only': 2})
        untouched = Query.objects.create(
            source='whatsapp', contact_phone='919000000013', last_asked_field='width@0', edit_field='item:0:width',
            pending_value={'_items': {'width': ['50']}})

        _migrate(_latest())

        from ..models import Query as CurrentQuery
        self.assertEqual(CurrentQuery.objects.get(pk=mid_question.pk).last_asked_field, 'product_type@1')
        edit = CurrentQuery.objects.get(pk=mid_edit.pk)
        self.assertEqual(edit.edit_field, 'item:2:product_type')
        self.assertEqual(edit.pending_value, {'_items': {'product_type': [4], 'grade': ['EN8D']}, '_item_only': 2})
        other = CurrentQuery.objects.get(pk=untouched.pk)
        self.assertEqual((other.last_asked_field, other.edit_field, other.pending_value),
                         ('width@0', 'item:0:width', {'_items': {'width': ['50']}}))

        _migrate(self.BEFORE)   # and back
        back = Query.objects.get(pk=mid_edit.pk)
        self.assertEqual(back.edit_field, 'item:2:product_category')
        self.assertEqual(back.pending_value['_items'], {'product_category': [4], 'grade': ['EN8D']})
        self.assertEqual(Query.objects.get(pk=mid_question.pk).last_asked_field, 'product_category@1')
