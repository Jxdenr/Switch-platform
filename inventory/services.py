"""Business actions on numbers. Views, the API and (later) the assistant all call these,
so the rules live in one place.
"""
import csv
import io
import re
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from core.models import log
from .models import Allocation, AllocationRate, BillingGroup, Number

DIGITS = re.compile(r"\D+")


def normalise(raw: str) -> str:
    """'+44 7700 900112' -> '447700900112'. Leading 00 is treated as international."""
    n = DIGITS.sub("", raw or "")
    if n.startswith("00"):
        n = n[2:]
    return n


def parse_number_list(text: str) -> list[str]:
    """Numbers from pasted text or CSV content: one per line or comma separated; the first
    column that looks like a number is used. Header rows are skipped."""
    out = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        cells = [c for c in row if c.strip()]
        found = [normalise(c) for c in cells if 6 <= len(normalise(c)) <= 15]
        if cells and len(found) == len(cells):
            out.extend(found)  # a row of numbers: "447700900200, 447700900201"
        elif found:
            out.append(found[0])  # a CSV row: first column that looks like a number
    seen, unique = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


def range_numbers(start: str, quantity: int) -> list[str]:
    s = normalise(start)
    if not s:
        raise ValueError("Enter a starting number.")
    if quantity < 1 or quantity > 100_000:
        raise ValueError("Quantity must be between 1 and 100,000.")
    width = len(s)
    first = int(s)
    nums = [str(first + i).zfill(width) for i in range(quantity)]
    if len(nums[-1]) > width:
        raise ValueError("The range runs past the end of the number block.")
    return nums


@dataclass
class AddResult:
    created: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)


@transaction.atomic
def add_numbers(group: BillingGroup, numbers: list[str], user=None, via="ui") -> AddResult:
    res = AddResult()
    existing = set(Number.objects.filter(number__in=numbers).values_list("number", flat=True))
    new = [Number(number=n, billing_group=group) for n in numbers if n not in existing]
    Number.objects.bulk_create(new, batch_size=2000)
    res.created = [n.number for n in new]
    res.duplicates = sorted(existing)
    log(user, "numbers.add", group, via=via, created=len(res.created), duplicates=len(res.duplicates))
    return res


@transaction.atomic
def allocate(numbers, client, payment_terms=None, all_terms=False, rates=None, service="",
             min_duration=None, max_duration=None, user=None, via="ui"):
    """Allocate numbers to a client. Numbers that are already allocated are moved:
    the old allocation is ended and a new one starts now.

    rates: list of dicts {slot, payment_terms, time_of_day, rate_out}. If empty the
    billing group's default rate_out for the chosen terms is used.
    """
    now = timezone.now()
    numbers = list(numbers)
    ids = [n.pk for n in numbers]
    moved = Allocation.objects.filter(number_id__in=ids, ends_at__isnull=True).update(ends_at=now)
    created = 0
    for num in numbers:
        if num.blocked:
            continue
        a = Allocation.objects.create(number=num, client=client, payment_terms=payment_terms,
                                      all_terms=all_terms, service=service, starts_at=now,
                                      min_duration=min_duration, max_duration=max_duration)
        chosen = rates or _default_rates(num.billing_group, payment_terms, all_terms)
        AllocationRate.objects.bulk_create([AllocationRate(allocation=a, **r) for r in chosen])
        created += 1
    log(user, "numbers.allocate", client, via=via, allocated=created, moved_from_other_client=moved)
    return created, moved


def _default_rates(group, payment_terms, all_terms):
    qs = group.rates.all()
    if payment_terms and not all_terms:
        qs = qs.filter(payment_terms=payment_terms)
    return [dict(slot=r.slot, payment_terms=r.payment_terms, time_of_day=r.time_of_day, rate_out=r.rate_out)
            for r in qs]


@transaction.atomic
def unallocate(numbers, user=None, via="ui"):
    ids = [n.pk for n in numbers]
    count = Allocation.objects.filter(number_id__in=ids, ends_at__isnull=True).update(ends_at=timezone.now())
    log(user, "numbers.unallocate", f"{count} numbers", via=via, count=count)
    return count


@transaction.atomic
def set_blocked(numbers, blocked: bool, user=None, via="ui"):
    count = Number.objects.filter(pk__in=[n.pk for n in numbers]).update(blocked=blocked)
    log(user, "numbers.block" if blocked else "numbers.unblock", f"{count} numbers", via=via, count=count)
    return count


@transaction.atomic
def move_to_group(numbers, group: BillingGroup, user=None, via="ui"):
    count = Number.objects.filter(pk__in=[n.pk for n in numbers]).update(billing_group=group)
    log(user, "numbers.move", group, via=via, count=count)
    return count


@transaction.atomic
def delete_numbers(numbers, user=None, via="ui"):
    """Only numbers with no message history can be deleted; others should be blocked."""
    from sms.models import Message
    ids = [n.pk for n in numbers]
    with_history = set(Message.objects.filter(number_id__in=ids).values_list("number_id", flat=True))
    deletable = [i for i in ids if i not in with_history]
    Number.objects.filter(pk__in=deletable).delete()
    log(user, "numbers.delete", f"{len(deletable)} numbers", via=via, deleted=len(deletable),
        kept_with_history=len(with_history))
    return len(deletable), len(with_history)
