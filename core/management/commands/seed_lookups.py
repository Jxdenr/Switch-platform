"""Load the reference data the platform needs: currencies, payment terms, times of day,
number types and countries. Safe to run more than once."""
from django.core.management.base import BaseCommand

from core.models import Country, Currency, NumberType, PaymentTerms, TimeOfDay

COUNTRIES = [  # name, iso2, dial code, continent
    ("United Kingdom", "GB", "44", "Europe"), ("Ireland", "IE", "353", "Europe"), ("France", "FR", "33", "Europe"),
    ("Germany", "DE", "49", "Europe"), ("Spain", "ES", "34", "Europe"), ("Italy", "IT", "39", "Europe"),
    ("Netherlands", "NL", "31", "Europe"), ("Belgium", "BE", "32", "Europe"), ("Portugal", "PT", "351", "Europe"),
    ("Poland", "PL", "48", "Europe"), ("Sweden", "SE", "46", "Europe"), ("Latvia", "LV", "371", "Europe"),
    ("Lithuania", "LT", "370", "Europe"), ("Estonia", "EE", "372", "Europe"), ("Austria", "AT", "43", "Europe"),
    ("United States", "US", "1", "North America"), ("Canada", "CA", "1", "North America"),
    ("Indonesia", "ID", "62", "Asia"), ("Bangladesh", "BD", "880", "Asia"), ("India", "IN", "91", "Asia"),
    ("Pakistan", "PK", "92", "Asia"), ("Philippines", "PH", "63", "Asia"), ("Thailand", "TH", "66", "Asia"),
    ("Vietnam", "VN", "84", "Asia"), ("Malaysia", "MY", "60", "Asia"), ("United Arab Emirates", "AE", "971", "Asia"),
    ("Nigeria", "NG", "234", "Africa"), ("Ghana", "GH", "233", "Africa"), ("Kenya", "KE", "254", "Africa"),
    ("South Africa", "ZA", "27", "Africa"), ("Egypt", "EG", "20", "Africa"), ("Morocco", "MA", "212", "Africa"),
    ("Australia", "AU", "61", "Oceania"), ("New Zealand", "NZ", "64", "Oceania"),
    ("Brazil", "BR", "55", "South America"), ("Mexico", "MX", "52", "North America"),
    ("Colombia", "CO", "57", "South America"), ("Argentina", "AR", "54", "South America"),
]


class Command(BaseCommand):
    help = "Create default currencies, payment terms, times of day, number types and countries."

    def handle(self, *args, **opts):
        for code, name, sym in [("GBP", "UK Pound", "£"), ("USD", "USA Dollar", "$"), ("EUR", "Euro", "€")]:
            Currency.objects.get_or_create(code=code, defaults={"name": name, "symbol": sym})
        for title, a, b in [("30/30", 30, 30), ("30/45", 30, 45), ("7/7/7", 7, 7), ("1", 1, 1)]:
            PaymentTerms.objects.get_or_create(title=title, defaults={"interval_1": a, "interval_2": b})
        for title in ["All Times", "Day", "Evening", "Weekend"]:
            TimeOfDay.objects.get_or_create(title=title)
        NumberType.objects.get_or_create(title="Default")
        for name, iso, dial, cont in COUNTRIES:
            Country.objects.get_or_create(name=name, defaults={"iso2": iso, "dial_code": dial, "continent": cont})
        self.stdout.write(self.style.SUCCESS("Lookups ready."))
