# Vendor Dashboard Fast

Streamlit dashboard for importing vendor documents, matching company folders, maintaining checklists and exporting company records.

## Large ZIP import

Choose **Upload documents → Google Drive ZIP (large files)** and paste a downloadable Drive file link. Downloads go to temporary disk in small chunks, then documents are processed one at a time. Keep the tab open until the summary appears.

Limits: 5 GiB downloaded ZIP, 5 GiB expanded batch, 5,000 entries and 128 MiB per document. Browser uploads are limited to 64 MB per file. Drive originals and browser ZIPs over 32 MiB are not copied into Uploaded ZIPs; retain them at their source. Drive links must allow downloading without signing in. Speed depends on Drive, disk, CPU and database resources.

## Render

Build: `pip install -r requirements.txt`

Start: `streamlit run app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true`

Set `APP_PASSWORD` in Render's environment settings. Set `DATABASE_URL` to an existing PostgreSQL connection string for permanent storage. Without it, documents use temporary local disk and can be lost on restart or redeployment. Never commit passwords, connection strings or vendor documents. A free-plan Blueprint is included in `render.yaml`.

## Tests

Install requirements and pytest, then run `PYTHONPATH=. python -m pytest tests -q`. The prepared fix passed 87 tests with synthetic documents, mocked Drive responses and local SQLite. Production Drive and PostgreSQL throughput are not benchmarked.

## Shared storage rollout

See [SHARED_STORAGE_SETUP.md](SHARED_STORAGE_SETUP.md) for the proposed database and migration checks. On Render, uploads are blocked until DATABASE_URL is configured. The Render per-document import cap is now 32 MiB; concurrent imports are rejected. Use **Refresh shared data** to see documents saved by other users. The prepared update passes 93 tests; production database persistence still requires provisioning and verification.
