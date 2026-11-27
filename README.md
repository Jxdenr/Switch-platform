# Switch platform

An SMS (and later voice) number platform: a CRM of suppliers and clients, their SMPP and HTTP
connections, billing groups of numbers from suppliers, allocation of those numbers to clients,
and a full record of every inbound SMS with rates, stats and exports.

The app is Django + PostgreSQL. SMPP is handled by [Jasmin](https://github.com/jookies/jasmin),
an open-source SMS gateway; this app configures Jasmin and receives every inbound SMS from it.

## How a message flows

1. A supplier delivers an SMS to one of your numbers, either over a bind they make to you
   (an **SMPP user**) or a bind you make to them (an **SMSC**).
2. Jasmin hands it to `POST /ingest/jasmin/`. The app records it, finds the number's billing group
   (rate in) and its current allocation (client and rate out), and checks blocking rules.
3. The `worker` process pushes it to the client's **HTTP endpoint**, retrying failures with back-off.
4. SMS records, stats (Due in / Due out / Margin) and exports read from the same table.

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py demo_data          # a "Demo Telecom" portal with fake suppliers, clients, numbers, SMS
python manage.py create_portal "Demo Telecom" you@example.com --site http://127.0.0.1:8000
python manage.py runserver
```

Open the printed link to choose a password, then sign in. Run the tests with `python manage.py test tests`.

## Run it on a server

1. A Linux VPS with Docker and a fixed public IP. Point a domain at it.
2. `cp .env.example .env` and change every value marked CHANGE.
3. `docker compose up -d --build`
4. Create the platform admin (only manages portals, must use an authenticator app):
   `docker compose exec web python manage.py createsuperuser`
5. Create your own portal and its owner login, and open the printed link to set the password:
   `docker compose exec web python manage.py create_portal "Your Company" you@example.com --site https://switch.example.com`
6. Put a TLS proxy (Caddy is simplest) in front of port 8000. Keep `TRUST_PROXY=1` behind it.
7. Open port 2775 to your suppliers' IPs only, for their SMPP binds.
8. After adding binds in the CRM: `docker compose exec web python manage.py sync_jasmin`.

## Portals and logins

One switch serves several separate businesses (**portals**). Each portal has its own suppliers,
clients, numbers, traffic, statements, company and bank details, and logins, and can't see any
other portal. Your own business is a portal like any other.

| Login | Can do |
| --- | --- |
| Platform admin (`createsuperuser`) | Create, rename, brand and suspend portals; invite or reset their owners. Sees no portal's data. Two-factor required. |
| Portal owner | Everything in their portal, including its logins and its company/bank details (Settings). |
| Team member | Only the areas the owner ticks (Customers, Numbers, SMS, Voice, Wholesale, Money), view-only unless allowed to make changes. |
| Client portal | One client account's own numbers, SMS, calls and statements. Never sees suppliers or buy rates. |

How isolation works (`core/tenancy.py`): every business table has a non-null `tenant` column, and
its default manager adds `tenant = <current portal>` when the query runs. A web request opens
exactly one portal (from the signed-in user); with none open, any query raises instead of returning
data. Saving a record into another portal, or pointing it at another portal's record, raises too.
Workers and management commands run across portals and narrow to one per message. New business
models must subclass `TenantOwned`. `tests/test_portals.py` checks that every URL needs a login and
that one portal can't read or write another's data.

Security: Argon2 password hashing, lockout after 5 failed sign-ins per email and IP (30 per IP),
optional authenticator-app two-factor for every login (required for platform admins, and for
owners too with `REQUIRE_2FA_ROLES=owner`), invite links instead of emailed passwords, 12-hour
sessions, secure cookies and HSTS when `DEBUG=0`. SMPP and HTTP bind credentials are separate
from web logins and never work as one.

Shared across all portals: SMPP bind usernames (one SMPP port), phone numbers (one route per
number), countries, currencies, payment terms and networks.

## Status

| Area | State |
| --- | --- |
| CRM, contacts, addresses, bank details | Done |
| SMPP users, SMSCs, HTTP endpoints, prefixes, notifications | Done (stored and shown) |
| Billing groups with 3 payment-term rate slots | Done |
| Add numbers by range, pasted list or CSV | Done |
| Allocate / unallocate / move / block / delete, incl. "all matching" | Done, with confirm step |
| Inbound SMS ingest, routing, blocking, HTTP forwarding with retries | Done, tested |
| SMS records, column picker, CSV export, live ticker, resend | Done |
| SMS stats by supplier, client, bind, day | Done |
| Client portal (clients see only their own numbers and SMS) | Done |
| Colourways (Harbour, Graphite, Paper) and 3 navigation layouts | Done |
| Built-in SMPP server (`smpp_server`): suppliers bind to send, clients bind to receive | Done, tested with a real SMPP client; **test with one real supplier and one real client before go-live** |
| Supplier HTTP bind (`/api/sms/inbound/`), client HTTP delivery | Done, tested |
| Statements tab, monthly invoices (suppliers) and statements (clients), auto email | Done, tested |
| Dashboard: due in/out per currency, traffic, delivery rate (ASR/ACD for voice), unrouted traffic, top suppliers/clients | Done, tested |
| Bulk allocation: allocate a quantity (e.g. next 5,000 free), select all matching, page size up to 1,000, streaming CSV export | Done, tested (10k in ~1.5s on SQLite) |
| Blocking rules/limits per supplier, client or billing group, set from the group form or allocate step | Done, tested |
| Client portal: dashboard with their numbers and delivery health, Statements page (own statements + volumes) | Done, tested |
| Jasmin sync for binds we make out to supplier SMSCs (`sync_jasmin`) | Written, **needs testing against a live Jasmin** |
| REST API for clients | Next |
| AI assistant | After v1 (see docs/platform-knowledge.md) |
| Numbers for SMS, voice or both (uploaded once, allocated per channel) | Done, tested |
| Voice call records (FreeSWITCH CDR ingest), billing increments, voice stats, dashboard minutes | Done, tested; **needs testing against a live FreeSWITCH** |
| Live voice calls (routing calls to clients through FreeSWITCH) | Later |

Deliberately not included: fake ("random") delivery receipts and randomised call durations.

## Voice call records

Point FreeSWITCH's `mod_json_cdr` at `https://<host>/ingest/freeswitch/?token=<INGEST_TOKEN>` (one POST per call;
JSON or form-encoded `cdr=` both work). Each call is routed on the number's active **voice** allocation, billed with
the billing group's increments (e.g. 60/60), minimum and maximum duration (the allocation's own min/max override the
group's), and the per-minute voice rates are copied onto the call. Retries with the same call UUID are ignored.

## Binds: sending and receiving

| | SMPP | HTTP |
|---|---|---|
| Supplier sends SMS to us | Binds to our SMPP server (port 2775) with an SMPP user, role "Supplier sends", and uses submit_sm. Or we bind out to their SMSC (Jasmin). | Calls `/api/sms/inbound/` with their HTTP user's key and `to`, `from`, `message`. |
| Client receives SMS from us | Binds to our SMPP server with an SMPP user, role "Client receives", and gets deliver_sm. | We POST/GET to their HTTP endpoint. |

If a client has both, HTTP is used. Known v1 limits of the SMPP server: multi-part (UDH) messages are stored one
record per part, and delivery receipts are not yet sent back to suppliers.

## Statements and invoices

Each account has a **Statement** tab: SMS and voice volumes and amounts for any date range, broken down by month, day,
billing group, country or number, with CSV export. Suppliers get an **invoice** (what they owe us, from Due In);
clients get a **statement** (what we owe them, from Due Out). Add a billing email and tick "Email monthly statement /
invoice automatically" on the account, and `send_statements --loop` emails last month's document on
`STATEMENT_SEND_DAY` (HTML in the email + a daily CSV). A sent document is frozen and never sent twice. Set
`COMPANY_NAME`, `COMPANY_DETAILS` (address, VAT number, bank details) and the `EMAIL_*` settings first.

Client-portal users get a **Statements** page with their own monthly statements and volumes. They cannot see
invoices, other accounts, or a breakdown by billing group.

## Dashboard

Money in different currencies is never added together. The headline shows the currency with the most traffic
in the period (labelled, e.g. GBP), lists any others underneath, and the chart and top lists use the headline
currency. `DEFAULT_CURRENCY` (default GBP) is used when there is no traffic. "Delivered" is the share of routed
SMS that reached the client; "Unrouted" counts SMS and calls to numbers with no client (blocked SMS are listed
separately).

## Wholesale SMS

Customers bind to the SMPP server (role "Wholesale customer") or use `POST /api/wholesale/send/`; the
`router` service (`manage.py wholesale_router`, run exactly one) binds out to vendors, sends with failover
and passes the vendors' real receipts back. Rules are in `docs/platform-knowledge.md` (Wholesale section).
`manage.py demo_wholesale` adds fake example customers, vendors and a week of traffic.
