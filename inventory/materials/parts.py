"""Splitting a job's material into parts and moving parts through the process steps.

A job starts as one part (the whole picked coil). Completing a step the admin marked "splits the
material" divides a part into smaller ones (1000 + 1000 kg from a 2000 kg coil, later 500 + 500 kg each);
every part then has its own tag, weight and progress. A step marked "all parts together" (packing, shipping)
is done once for all of the job's parts at the same time, and only when every part has reached it. The job is
complete when every part has completed every step.
"""

import string
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import ProductionPart, StepLog

MAX_PARTS_PER_SPLIT = 10
_THOUSANDTH = Decimal('0.001')


class PartError(Exception):
    """Something the employee did that can't be applied, in words fit to show them."""


def _steps(job):
    return list(job.product_type.steps.all())


def completed_step_ids(part):
    return {step_id for step_id, log in part.latest_logs_by_step().items() if log.status == 'completed'}


def unlocked_step_ids(part, steps=None, done=None):
    """Steps this part may work on now: every earlier step is completed for it."""
    steps = steps if steps is not None else _steps(part.job)
    done = done if done is not None else completed_step_ids(part)
    return {step.id for step in steps if all(other.id in done for other in steps if other.order < step.order)}


def _child_labels(parent, count):
    """JOB-0012 -> JOB-0012-A, -B ...; JOB-0012-A -> JOB-0012-A1, A2 ...; then letters again, and so on."""
    depth = parent.depth() + 1
    if depth % 2 == 1:
        return [f"{parent.label}-{string.ascii_uppercase[i]}" if i < 26 else f"{parent.label}-{i + 1}" for i in range(count)]
    return [f"{parent.label}{i + 1}" for i in range(count)]


def parse_weights(raw_values):
    """Weights typed in the split form -> a list of positive Decimals (blank boxes are ignored)."""
    weights = []
    for raw in raw_values:
        raw = (raw or '').strip()
        if not raw:
            continue
        try:
            value = Decimal(raw.replace(',', '.')).quantize(_THOUSANDTH)
        except InvalidOperation:
            raise PartError(f"'{raw}' is not a weight. Enter numbers in kg, for example 1000.")
        if value <= 0:
            raise PartError("Each part's weight must be more than zero.")
        weights.append(value)
    return weights


def _log(part, step, status, user, notes=''):
    return StepLog.objects.create(job=part.job, part=part, step=step, status=status,
                                  updated_by=user if getattr(user, 'is_authenticated', False) else None, notes=notes)


@transaction.atomic
def split_part(part, step, weights, user=None):
    """Complete `step` for `part` and divide it into parts of the given weights (kg). Whatever the parts
    don't add up to is scrap, recorded on `part`. Returns the new parts, whose tags are then printed.
    The new parts start with everything `part` had completed, and carry on from the next step."""
    job = part.job
    steps = _steps(job)
    if not step.splits_material or step not in steps:
        raise PartError("That step doesn't split the material.")
    if part.is_split():
        raise PartError("This part has already been split.")
    if step.id not in unlocked_step_ids(part, steps) or step.id in completed_step_ids(part):
        raise PartError("Complete the earlier steps first." if step.id not in completed_step_ids(part) else "That step is already done.")
    if not 2 <= len(weights) <= MAX_PARTS_PER_SPLIT:
        raise PartError(f"Enter the weight of each new part: between 2 and {MAX_PARTS_PER_SPLIT} parts. "
                        "To finish the step without splitting, use \"Complete without splitting\".")
    total = sum(weights, Decimal('0'))
    if total > part.weight:
        raise PartError(f"The parts add up to {total} kg but this part is only {part.weight} kg.")

    _log(part, step, 'completed', user, notes=f"Split into {len(weights)} parts")
    part.scrap_weight = part.weight - total
    part.split_at_step = step
    part.save(update_fields=['scrap_weight', 'split_at_step'])
    children = []
    inherited = [other for other in steps if other.order <= step.order]
    for label, weight in zip(_child_labels(part, len(weights)), weights):
        child = ProductionPart.objects.create(job=job, parent=part, label=label, weight=weight)
        for other in inherited:
            _log(child, other, 'completed', user, notes=f"Done before the split of {part.label}")
        children.append(child)
    job.recalculate_status()
    return children


def _joined_blockers(job, step, steps):
    """Parts that have not yet completed every step before `step` (so it can't be done for all together)."""
    blockers = []
    for part in job.active_parts():
        done = completed_step_ids(part)
        if not all(other.id in done for other in steps if other.order < step.order):
            blockers.append(part)
    return blockers


def joined_step_waiting_on(job, step):
    """Labels of the parts still holding up an "all parts together" step."""
    return [part.label for part in _joined_blockers(job, step, _steps(job))]


@transaction.atomic
def apply_step_action(part, step, action, user=None):
    """'start' or 'complete' one step for one part (or, for an "all parts together" step, for every part
    of the job at once). Raises PartError when it isn't allowed yet."""
    job = part.job
    steps = _steps(job)
    if step not in steps or action not in ('start', 'complete'):
        raise PartError("That step can't be updated.")
    if part.is_split():
        raise PartError("This part was split into smaller parts: update those instead.")
    status = 'completed' if action == 'complete' else 'in_progress'

    if step.joins_parts:
        waiting = _joined_blockers(job, step, steps)
        if waiting:
            raise PartError("This step is done for all parts together. Still waiting on: " + ", ".join(p.label for p in waiting) + ".")
        for other in job.active_parts():
            if step.id not in completed_step_ids(other):
                _log(other, step, status, user)
    else:
        if step.id not in unlocked_step_ids(part, steps):
            raise PartError("Complete the earlier steps first.")
        _log(part, step, status, user)
    job.recalculate_status()
