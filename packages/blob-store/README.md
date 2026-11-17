# eap-blob-store

`BlobStore` for chat attachment bytes (attachments r2, D0). Two backends chosen by `EAP_BLOB_BACKEND`:

- `fs` (default): a directory, `EAP_BLOB_DIR` (default `./.blobs`). Dev and tests.
- `s3`: any S3-compatible endpoint, Cloudflare R2 in the deployment. `EAP_BLOB_S3_ENDPOINT`,
  `EAP_BLOB_S3_BUCKET`, `EAP_BLOB_S3_REGION` (default `us-east-1`; `auto` for R2),
  `EAP_BLOB_S3_ACCESS_KEY`, `EAP_BLOB_S3_SECRET_KEY`. Path-style addressing.

Keys are `attachments/{tenant_id}/{attachment_id}` (`attachment_key`). The tenant id in the key is
informational; authorisation stays in the API.
