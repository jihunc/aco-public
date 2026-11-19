#!/bin/sh
# Local infrastructure initialization. curl provides native AWS SigV4 signing.
# Application services must depend on storage-init: service_completed_successfully.
set -eu

: "${AWS_ACCESS_KEY_ID:?Missing local storage access key}"
: "${AWS_SECRET_ACCESS_KEY:?Missing local storage secret key}"
: "${S3_BUCKET:?Missing local storage bucket}"

case "$S3_BUCKET" in
  *[!a-z0-9.-]*|'') echo "Invalid local bucket name" >&2; exit 1 ;;
esac

signed_request() {
  curl --silent --show-error --fail \
    --connect-timeout 3 --max-time 15 \
    --aws-sigv4 'aws:amz:us-east-1:s3' \
    --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" "$@"
}

bucket_url="http://storage-core:8333/$S3_BUCKET"
if ! signed_request --head --no-show-error --output /dev/null "$bucket_url"; then
  signed_request --request PUT --output /dev/null "$bucket_url"
fi

signed_request --request PUT --output /dev/null \
  --header 'Content-Type: application/xml' \
  --data-binary @/storage/default-encryption.xml "$bucket_url?encryption"

encryption=$(signed_request "$bucket_url?encryption")
case "$encryption" in
  *'<SSEAlgorithm>AES256</SSEAlgorithm>'*) ;;
  *) echo "Local bucket encryption initialization failed" >&2; exit 1 ;;
esac

echo "Local private storage bucket initialized with default AES256 encryption."
