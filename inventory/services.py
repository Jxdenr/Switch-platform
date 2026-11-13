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
from .models import SMS, VOICE, Allocation, AllocationRate, BillingGroup, Number

DIGITS = re.compile(r"\D+")


def normalise(raw: str) -> str:
    """'+44 7700 900112' -> '447700900112'. Leading 00 is treated as international."""
    n = DIGITS.sub("", raw or "")
    if n.startswith("00"):
        n = n[2:]
    return n


SCI = re.compile(r"^\s*\d+(\.\d+)?[eE]\+?\d+\s*$")


def check_text(text: str):
    """Catch Excel's scientific notation (4.47701E+11) before it turns into wrong numbers."""
    for cell in re.split(r"[,\n\r;\t]", text or ""):
        if SCI.match(cell):
            raise ValueError(f"“{cell.strip()}” looks like Excel scientific notation, so the real digits are lost. "
                             "In Excel, format the number column as Text (or Number with 0 decimals), save, and "
                             "upload again.")


def to_international(numbers: list[str], dial_code: str) -> list[str]:
    """Local format with a trunk 0 (07700 900123) -> international (447700900123) for the group's country."""
    if not dial_code:
        return numbers
    out = []
    for n in numbers:
        if n.startswith("0") and not n.startswith(dial_code):
            n = dial_code + n[1:]
        out.append(n)
    return out


def rejected_lines(text: str) -> list[str]:
    """Non-empty lines that contain no usable phone number (for telling the user what was skipped)."""
    bad = []
    for line in (text or "").splitlines():
        if line.strip() and not parse_number_list(line):
            bad.append(line.strip()[:40])
    return bad


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
    check_text(start)
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
             min_duration=None, max_duration=None, channels=None, user=None, via="ui"):
    """Allocate numbers to a client for one or more channels ("sms", "voice").

    channels: which channels to allocate; default = every channel the number's billing group
    supports. A channel the group doesn't support is skipped for that number.
    Numbers already allocated on a channel are moved: the old allocation ends now.
    rates: list of dicts {slot, payment_terms, time_of_day, rate_out}, applied to every channel.
    If empty, the billing group's default rate_out for that channel and terms is used.
    Returns (numbers allocated, allocations moved from another client or channel holder).

    Works in bulk (a handful of queries per 2,000 numbers) so 10k+ numbers allocate in seconds.
    """
    now = timezone.now()
    plan = []  # (number, [channels])
    for num in numbers:
        if num.blocked:
            continue
        chans = [c for c in (channels or num.billing_group.channels) if c in num.billing_group.channels]
        if chans:
            plan.append((num, chans))
    moved = 0
    default_cache = {}
    for i in range(0, len(plan), 2000):
        chunk = plan[i:i + 2000]
        for ch in (SMS, VOICE):
            ids = [n.pk for n, chans in chunk if ch in chans]
            if ids:
                moved += Allocation.objects.filter(number_id__in=ids, channel=ch, ends_at__isnull=True).update(ends_at=now)
        new = [Allocation(number=num, client=client, channel=ch, payment_terms=payment_terms, all_terms=all_terms,
                          service=service if ch == VOICE else "", starts_at=now, min_duration=min_duration,
                          max_duration=max_duration)
               for num, chans in chunk for ch in chans]
        Allocation.objects.bulk_create(new)
        if not all(a.pk for a in new):  # database can't return ids from bulk insert
            lookup = {(a.number_id, a.channel): a.pk for a in Allocation.objects.filter(
                number_id__in=[n.pk for n, _ in chunk], ends_at__isnull=True, starts_at=now)}
            for a in new:
                a.pk = lookup[(a.number_id, a.channel)]
        rate_rows = []
        for a in new:
            if rates:
                chosen = rates
            else:
                key = (a.number.billing_group_id, a.channel)
                if key not in default_cache:
                    default_cache[key] = _default_rates(a.number.billing_group, payment_terms, all_terms, a.channel)
                chosen = default_cache[key]
            rate_rows += [AllocationRate(allocation_id=a.pk, **r) for r in chosen]
        AllocationRate.objects.bulk_create(rate_rows, batch_size=2000)
    created = len(plan)
    log(user, "numbers.allocate", client, via=via, allocated=created, channels=channels or "group default",
        moved=moved)
    return created, moved


def _default_rates(group, payment_terms, all_terms, channel=SMS):
    qs = group.rates.filter(channel=channel)
    if payment_terms and not all_terms:
        qs = qs.filter(payment_terms=payment_terms)
    return [dict(slot=r.slot, payment_terms=r.payment_terms, time_of_day=r.time_of_day, rate_out=r.rate_out)
            for r in qs]


@transaction.atomic
def unallocate(numbers, channels=None, user=None, via="ui"):
    """End active allocations. channels=None ends both SMS and voice."""
    ids = [n.pk for n in numbers]
    qs = Allocation.objects.filter(number_id__in=ids, ends_at__isnull=True)
    if channels:
        qs = qs.filter(channel__in=channels)
    count = qs.values("number").distinct().count()
    qs.update(ends_at=timezone.now())
    log(user, "numbers.unallocate", f"{count} numbers", via=via, count=count, channels=channels or "all")
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
