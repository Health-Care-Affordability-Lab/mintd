# S3 Storage Setup

mintd supports S3-compatible storage for data versioning with DVC.

## AWS S3

```bash
mintd config setup
# Select: AWS S3
# Region: us-east-1 (or your preferred region)
# Bucket prefix: your-lab-name
```

## Wasabi

```bash
mintd config setup
# Select: S3-compatible
# Endpoint: https://s3.wasabisys.com
# Region: us-east-1
# Bucket prefix: your-lab-name
```

## MinIO

```bash
mintd config setup
# Select: S3-compatible
# Endpoint: https://your-minio-server.com
# Region: us-east-1
# Bucket prefix: your-lab-name
```

## Credentials

Store credentials securely:

```bash
mintd config setup --set-credentials
# Enter your access key and secret key
```

Credentials are stored in your system's secure keychain.
