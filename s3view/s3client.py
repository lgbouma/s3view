"""Thin, fast S3 layer built directly on botocore (no boto3 dependency).

Everything here is tuned for interactive browsing: listings are paginated and
cached, the next page is prefetched in the background, and byte ranges are
first-class so nothing ever downloads a whole object just to look at it.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import botocore.session
from botocore.config import Config
from botocore.exceptions import ClientError

# Shared pool for listing prefetch and other background work.
POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="s3view")

# Strided reads get their own pool so a big FITS preview cannot starve listing
# prefetch (and vice versa).
RANGE_POOL = ThreadPoolExecutor(max_workers=48, thread_name_prefix="s3view-range")

# Ranged reads bypass botocore entirely: we presign the object once and then
# issue plain pooled HTTPS GETs with a Range header (a presigned signature
# covers the URL, not the Range, so one URL serves every range). Signing 512
# requests through botocore instead costs tens of seconds of pure CPU.
try:
    import urllib3

    _HTTP = urllib3.PoolManager(
        maxsize=64, num_pools=4, retries=urllib3.Retry(2, backoff_factor=0.2)
    )
except Exception:  # pragma: no cover - urllib3 ships with botocore
    urllib3 = None
    _HTTP = None


class TTLCache:
    """Small thread-safe TTL + LRU cache."""

    def __init__(self, maxsize=512, ttl=90.0):
        self.maxsize = maxsize
        self.ttl = ttl
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if hit is None:
                return None
            expires, value = hit
            if expires < time.time():
                self._d.pop(key, None)
                return None
            return value

    def put(self, key, value):
        with self._lock:
            if len(self._d) >= self.maxsize:
                # Drop the soonest-to-expire entries; cheap and good enough here.
                for k in sorted(self._d, key=lambda k: self._d[k][0])[: self.maxsize // 4]:
                    self._d.pop(k, None)
            self._d[key] = (time.time() + self.ttl, value)

    def clear(self):
        with self._lock:
            self._d.clear()


class S3:
    def __init__(self, profile=None, region=None, endpoint_url=None, page_size=1000):
        self.session = botocore.session.Session(profile=profile)
        self.default_region = region or self.session.get_config_variable("region") or "us-east-1"
        self.endpoint_url = endpoint_url
        self.page_size = page_size
        self._clients = {}
        self._bucket_region = {}
        self._clients_lock = threading.Lock()
        self.list_cache = TTLCache(maxsize=1024, ttl=90.0)
        # Presigned URLs are reused across ranged reads; keep well under expiry.
        self._url_cache = TTLCache(maxsize=256, ttl=1200.0)
        self._inflight = set()
        self._inflight_lock = threading.Lock()

    # -- clients ---------------------------------------------------------
    def _client(self, region=None):
        region = region or self.default_region
        with self._clients_lock:
            c = self._clients.get(region)
            if c is None:
                cfg = Config(
                    region_name=region,
                    signature_version="s3v4",
                    max_pool_connections=64,
                    retries={"max_attempts": 3, "mode": "standard"},
                    s3={"addressing_style": "virtual"},
                    connect_timeout=5,
                    read_timeout=60,
                )
                c = self.session.create_client("s3", config=cfg, endpoint_url=self.endpoint_url)
                self._clients[region] = c
            return c

    def client_for(self, bucket):
        """Client bound to the bucket's own region.

        GetBucketLocation is frequently denied by bucket policy, so the region
        is instead read from the ``x-amz-bucket-region`` header that S3 returns
        on HeadBucket -- including on a 403, which is why the except branch
        matters as much as the success path.
        """
        region = self._bucket_region.get(bucket)
        if region:
            return self._client(region)
        base = self._client()
        if self.endpoint_url:  # custom endpoint (MinIO/R2): no region games
            self._bucket_region[bucket] = self.default_region
            return base
        try:
            resp = base.head_bucket(Bucket=bucket)
            region = resp["ResponseMetadata"]["HTTPHeaders"].get("x-amz-bucket-region")
        except ClientError as exc:
            hdrs = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
            region = hdrs.get("x-amz-bucket-region")
        except Exception:
            region = None
        region = region or self.default_region
        self._bucket_region[bucket] = region
        return self._client(region)

    # -- listing ---------------------------------------------------------
    def list_page(self, bucket, prefix="", token=None, limit=None, delimiter="/"):
        """One page of a prefix: folders (common prefixes) plus objects."""
        limit = limit or self.page_size
        key = (bucket, prefix, token, limit, delimiter)
        cached = self.list_cache.get(key)
        if cached is not None:
            self._prefetch_next(bucket, prefix, cached.get("next_token"), limit, delimiter)
            return cached

        kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": limit}
        if delimiter:
            kwargs["Delimiter"] = delimiter
        if token:
            kwargs["ContinuationToken"] = token
        t0 = time.time()
        resp = self.client_for(bucket).list_objects_v2(**kwargs)

        folders = [
            {"name": p["Prefix"][len(prefix):].rstrip("/"), "prefix": p["Prefix"]}
            for p in resp.get("CommonPrefixes", [])
        ]
        files = []
        for obj in resp.get("Contents", []):
            k = obj["Key"]
            if k == prefix:  # the zero-byte "directory marker" itself
                continue
            files.append(
                {
                    "name": k[len(prefix):],
                    "key": k,
                    "size": obj["Size"],
                    "mtime": obj["LastModified"].timestamp(),
                    "etag": obj.get("ETag", "").strip('"'),
                    "storage": obj.get("StorageClass", "STANDARD"),
                }
            )
        page = {
            "bucket": bucket,
            "prefix": prefix,
            "folders": folders,
            "files": files,
            "truncated": bool(resp.get("IsTruncated")),
            "next_token": resp.get("NextContinuationToken"),
            "ms": round((time.time() - t0) * 1000),
        }
        self.list_cache.put(key, page)
        self._prefetch_next(bucket, prefix, page["next_token"], limit, delimiter)
        return page

    def _prefetch_next(self, bucket, prefix, next_token, limit, delimiter):
        """Warm the following page so infinite scroll feels instant."""
        if not next_token:
            return
        key = (bucket, prefix, next_token, limit, delimiter)
        if self.list_cache.get(key) is not None:
            return
        with self._inflight_lock:
            if key in self._inflight:
                return
            self._inflight.add(key)

        def run():
            try:
                self.list_page(bucket, prefix, next_token, limit, delimiter)
            except Exception:
                pass
            finally:
                with self._inflight_lock:
                    self._inflight.discard(key)

        POOL.submit(run)

    def list_buckets(self):
        try:
            resp = self._client().list_buckets()
        except ClientError:
            return []
        return [
            {"name": b["Name"], "created": b["CreationDate"].timestamp()}
            for b in resp.get("Buckets", [])
        ]

    def search(self, bucket, prefix, query, max_keys=50000, limit=500, deadline=15.0):
        """Recursive substring search with a hard key and time budget."""
        q = query.lower()
        out, scanned, token = [], 0, None
        client = self.client_for(bucket)
        end = time.time() + deadline
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                scanned += 1
                if q in obj["Key"].lower():
                    out.append(
                        {
                            "name": obj["Key"][len(prefix):],
                            "key": obj["Key"],
                            "size": obj["Size"],
                            "mtime": obj["LastModified"].timestamp(),
                            "etag": obj.get("ETag", "").strip('"'),
                        }
                    )
                    if len(out) >= limit:
                        return {"files": out, "scanned": scanned, "complete": False}
            if not resp.get("IsTruncated") or scanned >= max_keys or time.time() > end:
                return {
                    "files": out,
                    "scanned": scanned,
                    "complete": not resp.get("IsTruncated"),
                }
            token = resp["NextContinuationToken"]

    # -- objects ---------------------------------------------------------
    def head(self, bucket, key):
        resp = self.client_for(bucket).head_object(Bucket=bucket, Key=key)
        return {
            "key": key,
            "size": resp["ContentLength"],
            "mtime": resp["LastModified"].timestamp(),
            "etag": resp.get("ETag", "").strip('"'),
            "content_type": resp.get("ContentType", ""),
            "storage": resp.get("StorageClass", "STANDARD"),
            "metadata": resp.get("Metadata", {}),
        }

    def presign(self, bucket, key, expires=3600, disposition=None, content_type=None):
        params = {"Bucket": bucket, "Key": key}
        if disposition:
            params["ResponseContentDisposition"] = disposition
        if content_type:
            params["ResponseContentType"] = content_type
        return self.client_for(bucket).generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires
        )

    def get_range(self, bucket, key, start, end):
        """Inclusive byte range -> bytes."""
        rng = "bytes=%d-%d" % (start, end)
        return self.client_for(bucket).get_object(Bucket=bucket, Key=key, Range=rng)["Body"].read()

    def _signed_url(self, bucket, key, expires=1800):
        """Presign once and reuse; all ranged reads of one object share it."""
        ck = (bucket, key)
        hit = self._url_cache.get(ck)
        if hit is not None:
            return hit
        url = self.presign(bucket, key, expires=expires)
        self._url_cache.put(ck, url)
        return url

    def get_ranges(self, bucket, key, ranges, workers=48):
        """Fetch many byte ranges in parallel, preserving order."""
        if _HTTP is not None:
            try:
                return self._get_ranges_http(bucket, key, ranges)
            except Exception:
                self._url_cache.clear()  # stale/expired signature: re-sign once
                try:
                    return self._get_ranges_http(bucket, key, ranges)
                except Exception:
                    pass  # fall through to the botocore path
        return list(RANGE_POOL.map(lambda r: self.get_range(bucket, key, r[0], r[1]), ranges))

    def _get_ranges_http(self, bucket, key, ranges):
        url = self._signed_url(bucket, key)

        def one(rng):
            resp = _HTTP.request(
                "GET", url,
                headers={"Range": "bytes=%d-%d" % (rng[0], rng[1])},
                preload_content=True,
            )
            if resp.status not in (200, 206):
                raise OSError("range GET returned HTTP %d" % resp.status)
            return resp.data

        return list(RANGE_POOL.map(one, ranges))

    def get_object(self, bucket, key, max_bytes=None):
        kwargs = {"Bucket": bucket, "Key": key}
        if max_bytes:
            kwargs["Range"] = "bytes=0-%d" % (max_bytes - 1)
        resp = self.client_for(bucket).get_object(**kwargs)
        return resp["Body"].read(), resp.get("ContentType", "")

    def get_stream(self, bucket, key, byte_range=None):
        """Streaming body + headers, for the proxy path."""
        kwargs = {"Bucket": bucket, "Key": key}
        if byte_range:
            kwargs["Range"] = byte_range
        resp = self.client_for(bucket).get_object(**kwargs)
        return resp
