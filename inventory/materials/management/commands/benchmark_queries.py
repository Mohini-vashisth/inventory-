"""
Benchmarks the production board view's query pattern: naive (lazy, N+1)
vs. the actual select_related/prefetch_related code in views.production_board.

Seeds a realistic dataset inside a transaction that is always rolled back,
so it never touches real data. Re-runnable, and safe to run against any
database — nothing it creates survives the run.

Usage: python3 manage.py benchmark_queries [--orders N] [--runs N]
"""
import time
import random

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test.utils import CaptureQueriesContext
from django.db import connection

from materials.models import (
    Customer, Order, ProductType, ProcessStep, Material, CoilPart,
    ProductionJob, StepLog,
)


class Command(BaseCommand):
    help = "Benchmark naive vs. optimized query patterns for the production board"

    def add_arguments(self, parser):
        parser.add_argument('--orders', type=int, default=60, help="How many in_production orders to seed.")
        parser.add_argument('--runs', type=int, default=5, help="How many timed runs to average.")

    def handle(self, *args, **options):
        n_orders = options['orders']
        n_runs = options['runs']

        with transaction.atomic():
            sid = transaction.savepoint()
            product_type, steps = self._seed_product_type()
            orders = self._seed_orders(n_orders, product_type, steps)

            self.stdout.write(f"Seeded {n_orders} in_production orders, "
                               f"{n_orders} jobs, {n_orders * len(steps)} step logs "
                               f"(rolled back after this run).\n")

            naive_time, naive_queries = self._benchmark(self._naive_production_board, n_runs)
            opt_time, opt_queries = self._benchmark(self._optimized_production_board, n_runs)

            transaction.savepoint_rollback(sid)

        improvement = (1 - opt_time / naive_time) * 100 if naive_time else 0
        query_cut = (1 - opt_queries / naive_queries) * 100 if naive_queries else 0

        self.stdout.write(self.style.SUCCESS(
            f"\n{'':20}{'time (avg of ' + str(n_runs) + ')':>18}{'queries':>12}\n"
            f"{'naive (no select/prefetch)':20}{naive_time*1000:>15.1f} ms{naive_queries:>12}\n"
            f"{'optimized (actual code)':20}{opt_time*1000:>15.1f} ms{opt_queries:>12}\n"
            f"\nTime:    {improvement:.1f}% faster\n"
            f"Queries: {query_cut:.1f}% fewer ({naive_queries} -> {opt_queries})\n"
        ))

    # ── Seeding ──────────────────────────────────────────────

    def _seed_product_type(self):
        pt = ProductType.objects.create(name='Benchmark Product', grade='BMK', size='9.999')
        steps = [
            ProcessStep.objects.create(product_type=pt, name=f'Step {i}', order=i)
            for i in range(1, 6)
        ]
        return pt, steps

    def _seed_orders(self, n, product_type, steps):
        orders = []
        for i in range(n):
            customer = Customer.objects.create(name=f'Benchmark Co {i}')
            order = Order.objects.create(
                customer=customer, product_type=product_type,
                quantity=100, status='in_production',
            )
            coil = Material.objects.create(quantity=500, grade='BMK', size='9.999')
            part = CoilPart.objects.create(coil=coil, part_no=f'BMK{i:04d}-A', weight=50)
            job = ProductionJob.objects.create(
                part=part, product_type=product_type, order=order,
                job_no=f'BMK-JOB-{i:04d}',
            )
            for step in steps:
                StepLog.objects.create(
                    job=job, step=step,
                    status=random.choice(['pending', 'in_progress', 'completed']),
                )
            orders.append(order)
        return orders

    # ── The two query patterns ──────────────────────────────

    def _naive_production_board(self):
        """No select_related/prefetch_related — every relation access is a fresh query."""
        board = []
        for order in Order.objects.filter(status='in_production'):
            _ = order.customer.name
            _ = order.product_type.name if order.product_type else None
            jobs_data = []
            for job in order.jobs.all():
                steps = list(job.product_type.steps.all())
                logs_by_step = {}
                for log in sorted(job.step_logs.all(), key=lambda l: l.timestamp, reverse=True):
                    logs_by_step.setdefault(log.step_id, log)
                completed = sum(
                    1 for s in steps
                    if logs_by_step.get(s.id) and logs_by_step[s.id].status == 'completed'
                )
                jobs_data.append({'job': job, 'total': len(steps), 'completed': completed})
            board.append({'order': order, 'jobs': jobs_data})
        return board

    def _optimized_production_board(self):
        """Mirrors materials.views.production_board exactly."""
        from django.db.models import Sum
        orders = (Order.objects
                  .filter(status='in_production')
                  .select_related('customer', 'product_type')
                  .prefetch_related(
                      'jobs__part__coil',
                      'jobs__product_type__steps',
                      'jobs__step_logs__step',
                  ))
        board = []
        for order in orders:
            jobs_data = []
            for job in order.jobs.all():
                steps = list(job.product_type.steps.all())
                logs_by_step = {}
                for log in sorted(job.step_logs.all(), key=lambda l: l.timestamp, reverse=True):
                    logs_by_step.setdefault(log.step_id, log)
                completed = sum(
                    1 for s in steps
                    if logs_by_step.get(s.id) and logs_by_step[s.id].status == 'completed'
                )
                jobs_data.append({'job': job, 'total': len(steps), 'completed': completed})
            board.append({'order': order, 'jobs': jobs_data})
        return board

    # ── Timing ───────────────────────────────────────────────

    def _benchmark(self, fn, n_runs):
        times = []
        query_count = 0
        for _ in range(n_runs):
            with CaptureQueriesContext(connection) as ctx:
                start = time.perf_counter()
                fn()
                times.append(time.perf_counter() - start)
            query_count = len(ctx.captured_queries)  # same every run for a given fn
        return sum(times) / len(times), query_count
