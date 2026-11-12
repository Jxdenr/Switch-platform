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
python manage.py demo_data          # fake suppliers, clients, 900 numbers, 1,500 SMS
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000 and sign in. Run the tests with `python manage.py test tests`.

## Run it on a server

1. A Linux VPS with Docker and a fixed public IP. Point a domain at it.
2. `cp .env.example .env` and change every value marked CHANGE.
3. `docker compose up -d --build`
4. `docker compose exec web python manage.py createsuperuser`
5. Put a TLS proxy (Caddy is simplest) in front of port 8000.
6. Open port 2775 to your suppliers' IPs only, for their SMPP binds.
7. After adding binds in the CRM: `docker compose exec web python manage.py sync_jasmin`.

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
| Jasmin sync and bind status (`sync_jasmin`) | Written, **needs testing against a live Jasmin and a test supplier bind** |
| REST API for clients | Next |
| AI assistant | After v1 (see docs/platform-knowledge.md) |
| Voice (FreeSWITCH, CDRs, live calls) | Later; data model already has the fields |

Deliberately not included: fake ("random") delivery receipts and randomised call durations.
