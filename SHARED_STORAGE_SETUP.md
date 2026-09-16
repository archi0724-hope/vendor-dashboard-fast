# Shared durable storage rollout

Status: code prepared; persistent database has not been provisioned or connected.

The running Render service uses temporary SQLite and files because DATABASE_URL is missing. All logins share the same server store, but that store is not durable across Render instance replacements. A code change alone cannot make temporary disk persistent.

## Proposed resource (requires billing approval)

- Workspace: hospkart
- Database: vendor-dashboard-data
- Region: Oregon (same as the existing web service)
- PostgreSQL compute: 0.1c-256mb (legacy Basic-256mb)
- Storage: 10 GB
- Estimated database cost: $6/month compute + $3/month storage = $9/month, excluding taxes and any other usage charges.
- Keep the existing web service on the free plan for this rollout. It may still restart or sleep; committed database records will survive. Larger compute can be considered if observed memory pressure continues.

Pricing checked 2026-09-16: https://render.com/pricing
Free databases have only 1 GB and expire after 30 days: https://render.com/docs/free

## Deployment order

1. Before replacing the current instance, export any surviving documents from the running app if feasible. Full in-memory backups of large datasets can exhaust a 512 MB service; use smaller company exports or retain the original source ZIPs. There is no SSH access to free Render services. Do not assume a redeployment migrates local files automatically.
2. Provision the approved database or use an existing PostgreSQL database with adequate capacity.
3. Put its connection string in the service's DATABASE_URL environment variable. Never commit credentials. This change redeploys the service.
4. Merge the storage fix branch to main; Render auto-deploys the new commit. Existing APP_PASSWORD remains unchanged.
5. Import a small synthetic document, sign in from another browser and verify the same company and document. Redeploy once and verify those records and original document bytes remain. Then import the real ZIP again; documents already committed to the database are deduplicated.
6. Check the import summary for skipped files. On Render each document is capped at 32 MiB to keep per-file allocations bounded. ZIP download/expanded size is still limited to 5 GiB. Only one download/import runs at a time within the single web process. Imports are not durable background jobs: an interruption still requires a retry.

Uploads are blocked on Render until DATABASE_URL is configured. Local development continues to use SQLite normally. The Refresh shared data button reloads the shared store; data are not partitioned by login or browser session.

The 93-test suite passes using mocked Drive responses and local SQLite. Live PostgreSQL persistence and multi-browser verification are pending database provisioning. Sampled metrics and logs show restarts but do not prove an out-of-memory kill; these changes reduce memory risk and stop further saves to known-temporary storage.
