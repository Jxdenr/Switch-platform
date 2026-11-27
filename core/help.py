"""Plain-English help for every page: a one-line summary, a key of the words used on the page,
and next steps. Also the shared glossary for column tooltips, the empty-state prompts, and the
"Get set up" checklist on the dashboard.

Keep it short. Every line here is read by someone who is new to the platform and in a hurry.
Entries marked "client" are what a client login sees instead (client logins see far less).
"""

# Short definitions shown when hovering or tapping a small (i) next to a column or figure.
GLOSSARY = {
    "due_in": "What suppliers owe us for SMS and calls to our numbers in this period.",
    "due_out": "What we owe clients for the traffic we passed on to them.",
    "margin": "What we keep: what we charge minus what it costs us.",
    "delivered_pct": "Of messages with a final result, the share the network confirmed as delivered. "
                     "Taken from the real delivery receipts only.",
    "receipt": "A delivery receipt (DLR): the network's report of whether a message reached the phone.",
    "no_receipt": "Messages that never got a delivery receipt within the time limit. A high figure means "
                  "the route is losing messages, or its receipts don't reach us.",
    "receipt_delay": "Typical time from handing a message to the vendor until its receipt comes back. "
                     "Range in brackets is the fastest 10% to the slowest 10%.",
    "waiting": "Sent to a vendor, still waiting for its receipt.",
    "refused": "Messages we turned away before sending, e.g. no credit, blocked sender or over a cap. "
               "The customer is not charged.",
    "balance": "Money on account. Prepaid customers stop sending at 0.",
    "credit_limit": "How far below 0 a customer may go before traffic stops. 0 means prepaid.",
    "charge_on": "On vendor accept: charged as soon as a vendor takes the message. "
                 "On delivery: charged only for messages delivered (refunded otherwise).",
    "tps": "Maximum messages per second on this connection.",
    "peak": "The busiest single minute in the last 24 hours.",
    "bind": "A live connection (SMPP or HTTP) between us and another platform.",
    "unmatched": "Receipts the vendor sent whose message ID matched none of ours. Usually an ID format problem.",
    "route_plan": "Which vendors a customer's traffic may use, and how one is picked.",
    "share": "Split mode: the vendor's slice of the traffic. 70 and 30 sends about 70% to one and 30% to the other.",
    "priority": "Priority mode: 1 is tried first, then 2, and so on.",
    "warnings": "Signs a vendor's receipts may be generated rather than real. A reason to test the route "
                "with real phones, not proof.",
    "allocated": "Numbers given to a client. SMS to them are passed to that client.",
    "unrouted": "Inbound SMS to numbers with no client, so nobody received them.",
    "rate_in": "What the supplier pays us per SMS or per minute.",
    "rate_out": "What we pay the client per SMS or per minute.",
    "asr": "Answer-seizure ratio: the share of calls that were answered.",
    "acd": "Average call duration of answered calls.",
    "test": "Test traffic is never sent to a network. Receipts are simulated and labelled test.",
    "failure_codes": "The vendor's own error codes for failed messages (stat and err from the receipt).",
}


def _p(title, summary, key=(), steps=(), client=None):
    return {"title": title, "summary": summary, "key": list(key), "steps": list(steps), "client": client}


PAGES = {
    "dashboard": _p(
        "Dashboard",
        "A quick read of money and traffic for the period you pick at the top right.",
        [("Due in", GLOSSARY["due_in"]), ("Due out", GLOSSARY["due_out"]),
         ("Delivered to clients", "Inbound SMS we passed on to a client without an error."),
         ("Unrouted", GLOSSARY["unrouted"]),
         ("Needs attention", "Anything that needs a person: binds down, failed deliveries, unused numbers.")],
        [("Allocate numbers", "/numbers/allocate/"), ("See SMS records", "/sms/records/")],
        client=_p("Dashboard", "What you've received and earned in the period picked at the top right.",
                  [("Traffic", "SMS and calls to your numbers."),
                   ("Earned", "What you're owed for that traffic, shown in Statements.")],
                  [("My numbers", "/numbers/"), ("Statements", "/statements/")])),
    "suppliers": _p(
        "Suppliers",
        "Companies that send SMS and calls to our numbers. They pay us for that traffic.",
        [("Binds", GLOSSARY["bind"]), ("Billing groups", "The number ranges we hold from this supplier, with their rates."),
         ("Currency / terms", "What they're invoiced in, and how often.")],
        [("Add a supplier", "/accounts/new/?kind=supplier"), ("Check bind status", "/binds/")]),
    "clients": _p(
        "Clients",
        "Companies we pass traffic on to. We pay them for it.",
        [("Allocated", GLOSSARY["allocated"]), ("HTTP endpoint", "The web address we POST each SMS to."),
         ("Currency / terms", "What their statement is in, and how often.")],
        [("Add a client", "/accounts/new/?kind=client"), ("Allocate numbers", "/numbers/allocate/")]),
    "account": _p(
        "Account",
        "Everything about one company. Use the tabs: connections, voice, wholesale, details and statement.",
        [("SMS connections", "SMPP users (they bind to us), SMSCs (we bind to them) and HTTP endpoints."),
         ("Wholesale", "Turn this account into a customer that sends us traffic, or a vendor we send traffic to."),
         ("Statement", "Monthly invoice or statement, ready to email.")]),
    "binds": _p(
        "Bind status",
        "Every connection in one list. Red means it should be up and isn't.",
        [("Bound", "Connected right now."), ("Not bound", "Enabled but not connected. Check with the other side."),
         ("Disabled", "Switched off on purpose. Not counted as down."),
         ("SMPP user", "They connect to us with a username and password."),
         ("SMSC", "We connect to them.")]),
    "groups": _p(
        "Billing groups",
        "Each group is one supplier range: who it's from, the country, what it's for and the rates.",
        [("Rate in", GLOSSARY["rate_in"]), ("Rate out", GLOSSARY["rate_out"]),
         ("SMS / Voice / Both", "Which traffic the numbers in this group take."),
         ("Allocated", GLOSSARY["allocated"])],
        [("New billing group", "/billing-groups/new/"), ("Add numbers", "/numbers/add/")]),
    "group_form": _p(
        "Billing group",
        "One supplier range. The title appears on client statements.",
        [("Payment terms", "Up to three rate slots, so a supplier on weekly terms can pay a different rate from monthly."),
         ("Rules", "Blocking rules that apply to every number in this group.")]),
    "numbers": _p(
        "Numbers",
        "Every number we hold. Filter, tick some (or all matching), then allocate, move, block or export.",
        [("Free", "Not given to any client yet."), ("Allocated", GLOSSARY["allocated"]),
         ("Blocked", "SMS to this number are recorded but not passed on or paid."),
         ("Select all matching", "Acts on every number the filters match, not only this page.")],
        [("Allocate a quantity", "/numbers/allocate/"), ("Add numbers", "/numbers/add/")],
        client=_p("My numbers", "The numbers allocated to you. SMS to them come straight to your endpoint.",
                  [("Allocated on", "When the number became yours.")])),
    "addnumbers": _p(
        "Add numbers",
        "Put numbers into a billing group by range, pasted list or CSV. Duplicates are skipped.",
        [("Range", "From and to, e.g. 447700900000 to 447700900999."),
         ("Billing group", "Sets the supplier, country and rates for all of them.")],
        [("New billing group first", "/billing-groups/new/")]),
    "allocate": _p(
        "Allocate numbers",
        "Give a client numbers. Pick a client and a rate; numbers already allocated move across.",
        [("Rate out", GLOSSARY["rate_out"]),
         ("Quantity", "Takes the next free numbers from a range, so there's no need to tick them.")]),
    "records": _p(
        "SMS records",
        "Every inbound SMS. Filter by date, supplier, client or number. Export to CSV.",
        [("Status", "Delivered: passed to the client. Failed: the client's endpoint refused it (we retry). "
                    "Blocked: stopped by a rule. Unrouted: the number has no client."),
         ("Columns", "Pick which columns to show with the Columns button.")],
        [("Blocking rules", "/sms/blocking/"), ("Live ticker", "/sms/ticker/")],
        client=_p("SMS records", "Every SMS to your numbers. Filter by date or number, export to CSV.",
                  [("Status", "Delivered: reached your endpoint. Failed: your endpoint refused it and we're retrying.")])),
    "ticker": _p("Live ticker", "SMS as they arrive, newest at the top. Updates every 3 seconds.",
                 [("Status", "Delivered, failed, blocked or unrouted, as on SMS records.")]),
    "stats": _p(
        "SMS stats",
        "Totals by supplier, client, bind or day, for the dates you pick.",
        [("Due in", GLOSSARY["due_in"]), ("Due out", GLOSSARY["due_out"]), ("Margin", GLOSSARY["margin"])]),
    "blocking": _p(
        "Blocking rules",
        "Stop SMS by sender, number, text or volume. Blocked SMS are kept on record but not passed on or paid.",
        [("Applies to", "Everyone, one supplier, one client, one outbound customer or one billing group."),
         ("Limit", "At most this many SMS to one number per hour. Stops floods."),
         ("Hits", "How many SMS the rule has stopped. Click to see them.")]),
    "cdrs": _p("Call records", "Every call to our numbers, with duration and money.",
               [("ASR", GLOSSARY["asr"]), ("ACD", GLOSSARY["acd"])]),
    "voicestats": _p("Voice stats", "Call totals by supplier, client or day.",
                     [("ASR", GLOSSARY["asr"]), ("ACD", GLOSSARY["acd"]), ("Margin", GLOSSARY["margin"])]),
    "statements": _p("Statements", "Monthly statements of what you're owed.",
                     [("Statement", "One per month, with every number and its traffic.")]),
    # Outbound SMS (A2P)
    "ws_overview": _p(
        "Outbound SMS",
        "Customers send messages through us, we pass them to vendors and send back the vendors' real receipts.",
        [("Delivered", GLOSSARY["delivered_pct"]), ("Refused", GLOSSARY["refused"]),
         ("Peak", GLOSSARY["peak"]), ("Receipts to send", "Receipts waiting for the customer to collect them."),
         ("Warnings", GLOSSARY["warnings"])],
        [("Set up a vendor", "/suppliers/"), ("Make a route plan", "/wholesale/routes/"),
         ("Vendor quality", "/wholesale/quality/")]),
    "ws_messages": _p(
        "Outbound messages",
        "Every message customers sent, with where it went and the receipt that came back.",
        [("Status", "With vendor: waiting for its receipt. Delivered / Undelivered / Expired: from the receipt. "
                    "Unknown: no receipt came back in time. Refused: we didn't send it."),
         ("Receipt", "Whether we've passed the receipt back to the customer."),
         ("Test, not sent", GLOSSARY["test"])]),
    "ws_quality": _p(
        "Vendor quality",
        "How well each vendor delivers, from their real receipts. Use it to pick vendors and spot bad routes.",
        [("Delivered", GLOSSARY["delivered_pct"]), ("No receipt", GLOSSARY["no_receipt"]),
         ("Receipt delay", GLOSSARY["receipt_delay"]), ("Top failure codes", GLOSSARY["failure_codes"]),
         ("Warnings", GLOSSARY["warnings"])]),
    "ws_routes": _p(
        "Route plans",
        "Decide which vendors carry a customer's traffic. Give each customer a plan on their Outbound SMS tab.",
        [("Least cost", "Cheapest vendor first."), ("Priority order", GLOSSARY["priority"]),
         ("Best delivery rate", "Vendor with the best real delivered % on that country in the last 24 hours first."),
         ("Split by percentage", GLOSSARY["share"]),
         ("Skip vendors under %", "Leaves out vendors delivering below this, once they've sent 50+ messages there."),
         ("Failover", "If a vendor refuses a message, the next one in the plan is tried automatically.")]),
    "ws_networks": _p(
        "Networks",
        "Which number prefixes belong to which mobile network. Lets you price and route per network.",
        [("MCC / MNC", "The country and network codes every mobile network has."),
         ("Prefix", "The start of the number, in international format.")]),
    "ws_account": _p(
        "Outbound SMS for this account",
        "Settings, credit, connections and rates for this customer or vendor.",
        [("Balance", GLOSSARY["balance"]), ("Credit limit", GLOSSARY["credit_limit"]),
         ("Charge", GLOSSARY["charge_on"]), ("Sell rates", "What this customer pays us per message, by country or network."),
         ("Buy rates", "What we pay this vendor per message."), ("Test mode", GLOSSARY["test"])]),
}

# Empty tables: a friendly line and the one button that fixes it.
EMPTY = {
    "suppliers": ("No suppliers yet", "Add the first company that sends traffic to your numbers.",
                  "Add your first supplier", "/accounts/new/?kind=supplier"),
    "clients": ("No clients yet", "Add the first company you'll pass traffic on to.",
                "Add your first client", "/accounts/new/?kind=client"),
    "groups": ("No billing groups", "Create one for each supplier range, then add its numbers.",
               "Create a billing group", "/billing-groups/new/"),
    "numbers": ("No numbers here", "Add numbers to a billing group to get started.", "Add numbers", "/numbers/add/"),
    "records": ("No SMS in this range", "Try a wider date range, or check your binds are up.", "Bind status", "/binds/"),
    "stats": ("No traffic in this range", "Try a wider date range.", "", ""),
    "blocking": ("No rules yet", "Add one below to stop spam senders, keywords or floods.", "", ""),
    "cdrs": ("No calls in this range", "Try a wider date range.", "", ""),
    "ws_customers": ("No outbound customers yet",
                     "Open any account, choose the Outbound SMS tab and press “Make wholesale customer”.",
                     "Choose an account", "/clients/"),
    "ws_vendors": ("No vendors yet",
                   "Open the account of a company with network connections, choose the Outbound SMS tab and press "
                   "“Make vendor”.", "Choose an account", "/suppliers/"),
    "ws_messages": ("No messages in this range", "Try a wider date range, or send a test from a test bind.", "", ""),
    "ws_quality": ("No vendor traffic yet", "Quality appears here once vendors have carried live messages.",
                   "Messages", "/wholesale/messages/"),
    "ws_plans": ("No route plans yet",
                 "Without a plan, customers use every vendor with a rate, cheapest first. "
                 "Add a plan below to choose vendors yourself.", "", ""),
    "ws_networks": ("No networks yet", "Rates and routes work per country until you import some.", "", ""),
}


def page(key, is_client=False):
    p = PAGES.get(key)
    if p and is_client:
        p = p["client"] or {**p, "steps": []}
    return p


def setup_steps():
    """The "Get set up" checklist on the dashboard: (label, done, url, why)."""
    from crm.models import Account, SmppUser, Smsc
    from inventory.models import Allocation, BillingGroup, Number
    from wholesale.models import BuyRate, Customer, SellRate, Vendor, VendorBind
    suppliers = Account.objects.filter(is_supplier=True)
    inbound = [
        ("Add a supplier", suppliers.exists(), "/accounts/new/?kind=supplier",
         "The company that sends traffic to your numbers."),
        ("Connect them", SmppUser.objects.filter(account__in=suppliers).exists()
         or Smsc.objects.filter(account__in=suppliers).exists(), "/suppliers/",
         "Open the supplier and add an SMPP user or SMSC."),
        ("Create a billing group", BillingGroup.objects.exists(), "/billing-groups/new/",
         "Their number range, country and rates."),
        ("Add numbers", Number.objects.exists(), "/numbers/add/", "By range, list or CSV."),
        ("Add a client", Account.objects.filter(is_client=True).exists(), "/accounts/new/?kind=client",
         "The company you pass the traffic on to."),
        ("Allocate numbers", Allocation.objects.filter(ends_at__isnull=True).exists(), "/numbers/allocate/",
         "Give the client numbers and a rate."),
    ]
    outbound = [
        ("Make a vendor", Vendor.objects.exists(), "/suppliers/", "Open an account, Outbound SMS tab, “Make vendor”."),
        ("Connect to the vendor", VendorBind.objects.exists(), "/wholesale/",
         "On the vendor's Outbound SMS tab, add a bind."),
        ("Add buy rates", BuyRate.objects.exists(), "/wholesale/", "What the vendor charges, by country."),
        ("Make a customer", Customer.objects.exists(), "/clients/",
         "Open an account, Outbound SMS tab, “Make wholesale customer”."),
        ("Add sell rates", SellRate.objects.exists(), "/wholesale/", "What the customer pays you."),
        ("Add credit", Customer.objects.exclude(balance=0).exists() or Customer.objects.exclude(credit_limit=0).exists(),
         "/wholesale/", "Prepaid customers can't send until they have a balance."),
    ]
    return [{"title": "Receive SMS on your numbers", "steps": inbound},
            {"title": "Send SMS for customers", "steps": outbound}]
