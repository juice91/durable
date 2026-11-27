# eap-blob-store

`BlobStore` for chat attachment bytes (attachments r2, D0). Two backends chosen by `EAP_BLOB_BACKEND`:

- `fs` (default): a directory, `EAP_BLOB_DIR` (default `./.blobs`). Dev and tests.
- `s3`: any S3-compatible endpoint (MinIO, AWS S3). `EAP_BLOB_S3_ENDPOINT`,
  `EAP_BLOB_S3_BUCKET`, `EAP_BLOB_S3_REGION` (default `us-east-1`; `auto` for R2),
  `EAP_BLOB_S3_ACCESS_KEY`, `EAP_BLOB_S3_SECRET_KEY`. Path-style addressing.

Keys are `attachments/{tenant_id}/{attachment_id}` (`attachment_key`). The tenant id in the key is
informational; authorisation stays in the API.

## Delete order

The blob and its row live in different systems, so every delete has a fixed order that a crash
cannot turn into a message pointing at nothing:

- **Discarding an unsent upload** (`DELETE /attachments/{id}`): read the row `FOR UPDATE` first and
  check it is not in use (a concurrent `link` waits on the lock), then delete the blob, then the row.
  A crash between the two leaves a row without a blob (a download is 410), which retention removes.
- **Retention of expired unsent uploads**: delete the row first, conditional on `message_id IS NULL`
  (an upload linked since it was listed survives), then the blob. A crash leaves a blob without a row:
  wasted space, never a dangling message reference.

Blobs are streamed: `put` spools the upload to check its size, then uploads from the spool file;
`open` yields chunks. The S3 client is opened per call (an aiobotocore client is bound to its event
loop and one store is shared across loops), with a 5 s connect and 30 s read timeout.
