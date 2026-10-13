# AccDivide integration

Accounting reads the current AccDivide catalog through a small authenticated,
read-only API. It never connects to or changes AccDivide's database.

Set the same long random value in both applications:

```dotenv
# AccDivide acc_V3.1/.env
ACCOUNTING_CATALOG_TOKEN=replace-with-a-long-random-secret

# Accounting/.env
ACCDIVIDE_CATALOG_URL=https://your-accdivide-host/api/integrations/accounting/catalog
ACCDIVIDE_CATALOG_TOKEN=replace-with-the-same-long-random-secret
```

Accounting caches a successful catalog response for five minutes by default
(`ACCDIVIDE_CATALOG_CACHE_SECONDS`). If AccDivide is temporarily unavailable,
the most recently successful product list remains available instead of
silently replacing it with an empty list.

## Confirmed-payment importer (optional)

`POST /api/integrations/confirmed-payment` is an idempotent server-to-server
endpoint prepared for a later AccDivide webhook outbox. Enable it only with:

```dotenv
# Accounting/.env
ACCOUNTING_INGEST_TOKEN=another-long-random-secret
```

It rejects any payload whose `status` is not exactly `confirmed`; browser
return and success pages cannot create entries. A caller must send the token
as `X-Accounting-Token` and include a stable provider `event_id`, allowing
retries without duplicate revenue or fee entries.

The AccDivide sender should be invoked only after its signed Stripe, PayPal,
or NOWPayments webhook has been verified and the canonical invoice is marked
confirmed. It should not be called from checkout completion or return routes.
