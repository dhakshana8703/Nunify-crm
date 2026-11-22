# Connect an external scraper

Create an external source in **Sources** as an admin. Save the generated key in the scraper's secret store. Send it in the `Authorization: Bearer <source key>` header on every request. The key is scoped to that source. Rotate or revoke it from Sources.

Run the scraper on its own authorized schedule. Use a stable run key, such as the source's occurrence timestamp or import identifier. The CRM worker handles staging and promotion after submission.

## Requests

1. Open a run with `POST /api/ingest/{source_id}/runs` and save the returned `run_id`.

   ```json
   {"idempotency_key":"2026-11-20-snapshot","input_name":"Saved organizations"}
   ```

2. Send batches to `POST /api/ingest/{source_id}/runs/{run_id}/records`. Each request accepts up to 1,000 flat records and 2 MiB. Supply a stable `source_id` per organization or listing when available.

   ```json
   {
     "idempotency_key": "2026-11-20-batch-1",
     "batch_number": 1,
     "records": [
       {
         "source_id": "organization-389",
         "name": "Example Industry Association",
         "website": "https://example.org",
         "source_url": "https://example.org/about",
         "organization_type": "Association"
       }
     ]
   }
   ```

   Review each receipt's `errors` before closing. To correct a row, submit it in a new numbered batch with a new batch key. Reuse the original key and body when retrying the same request. Explicit batch numbers let a collector send batches out of order. The latest numbered version wins for the same source record.

3. Close the run with `POST /api/ingest/{source_id}/runs/{run_id}/complete`. Include the last batch number as `expected_batches` to check that all batches arrived.

   ```json
   {"expected_batches":1}
   ```

   The returned `source_run_id` identifies the preview or worker run in Sources. Sources default to admin review. Admins can enable automatic promotion for a source. Either mode applies the same account matching and suppression.

4. Check status with `GET /api/ingest/{source_id}/runs/{run_id}`. Use backoff for transient failures and honor `Retry-After` on rate limiting. A reused key with different data or a close request with missing batches returns `409`.

The existing single-batch `POST /api/sources/{source_id}/ingest` remains available for established senders.
