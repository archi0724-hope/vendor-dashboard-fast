"""Bounded Google Drive ZIP imports with disk and HTTP-range fallbacks."""
from collections import OrderedDict
from contextlib import contextmanager
from html.parser import HTMLParser
import re
import shutil
import tempfile
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import zipfile

import requests

MAX_DOWNLOAD = 5 * 1024**3
CHUNK_SIZE = 1024**2
RANGE_BLOCK_SIZE = 8 * CHUNK_SIZE
RANGE_CACHE_BLOCKS = 8
GOOGLE_HOSTS = {"drive.google.com", "drive.usercontent.google.com", "docs.google.com"}


def validate_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or
            parsed.port not in (None, 443) or
            not (host in GOOGLE_HOSTS or host.endswith(".googleusercontent.com"))):
        raise ValueError("Use an HTTPS Google Drive file link.")
    return parsed


def drive_file_id(link):
    parsed = validate_url(link.strip())
    match = re.search(r"/file/d/([A-Za-z0-9_-]+)", parsed.path)
    file_id = match.group(1) if match else parse_qs(parsed.query).get("id", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
        raise ValueError("Paste the Google Drive link for a ZIP file, not a folder.")
    return file_id


class ConfirmationForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields = {}
        self.active = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "download-form":
            self.active = True
            self.action = attrs.get("action")
        if self.active and tag == "input" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False


def open_response(session, url, headers=None):
    """Open a Drive response while validating every redirect target."""
    request_headers = headers or {}
    for _ in range(6):
        validate_url(url)
        response = session.get(
            url,
            stream=True,
            timeout=(15, 60),
            allow_redirects=False,
            headers=request_headers,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            next_url = urljoin(url, response.headers.get("Location", ""))
            response.close()
            url = next_url
            continue
        try:
            response.raise_for_status()
        except Exception:
            response.close()
            raise ValueError("Drive refused the download. Check file permissions and download quota.") from None
        # Keep the final validated URL available to the HTTP-range fallback.
        try:
            response._vendor_download_url = url
        except Exception:
            pass
        return response
    raise ValueError("Drive redirected too many times. Check the file link.")


class DiskUpload:
    name = "Google_Drive_Import.zip"

    def __init__(self, file):
        self.file = file

    def __getattr__(self, name):
        return getattr(self.file, name)


class DriveRangeFile:
    """Small-memory, seekable view of a large Drive file using HTTP Range requests.

    Python's zipfile module needs a seekable object, but it does not require the
    whole archive to live on local disk. Blocks are fetched on demand and a small
    LRU cache keeps repeated ZIP metadata reads fast.
    """

    def __init__(self, session, url, size, progress=None,
                 block_size=RANGE_BLOCK_SIZE, cache_blocks=RANGE_CACHE_BLOCKS):
        if size <= 0:
            raise ValueError("Drive did not report the ZIP size; large-file streaming is unavailable.")
        self.session = session
        self.url = url
        self.size = int(size)
        self.progress = progress
        self.block_size = int(block_size)
        self.cache_blocks = int(cache_blocks)
        self.position = 0
        self.closed = False
        self.cache = OrderedDict()
        self.fetched_bytes = 0

    def tell(self):
        return self.position

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, offset, whence=0):
        if self.closed:
            raise ValueError("I/O operation on closed Drive stream")
        if whence == 0:
            position = offset
        elif whence == 1:
            position = self.position + offset
        elif whence == 2:
            position = self.size + offset
        else:
            raise ValueError("Invalid seek mode")
        if position < 0:
            raise ValueError("Negative seek position")
        self.position = min(int(position), self.size)
        return self.position

    def _fetch_block(self, index):
        if index in self.cache:
            payload = self.cache.pop(index)
            self.cache[index] = payload
            return payload

        start = index * self.block_size
        end = min(self.size - 1, start + self.block_size - 1)
        if start > end:
            return b""

        headers = {
            "Range": f"bytes={start}-{end}",
            "Accept-Encoding": "identity",
        }
        with open_response(self.session, self.url, headers=headers) as response:
            if response.status_code != 206:
                raise ValueError(
                    "Google Drive did not allow ranged ZIP reads. Split this ZIP into smaller files "
                    "or use a server with more temporary disk."
                )
            content_range = response.headers.get("Content-Range", "")
            match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range.strip(), re.I)
            if match:
                got_start, got_end = int(match.group(1)), int(match.group(2))
                if got_start != start or got_end != end:
                    raise ValueError("Drive returned an unexpected byte range. Retry the import.")
            chunks = []
            received = 0
            expected = end - start + 1
            for chunk in response.iter_content(CHUNK_SIZE):
                if not chunk:
                    continue
                received += len(chunk)
                if received > expected:
                    raise ValueError("Drive returned more data than requested for a ZIP range.")
                chunks.append(chunk)
            payload = b"".join(chunks)
            if len(payload) != expected:
                raise ValueError("Drive returned an incomplete ZIP range. Retry the import.")

        self.cache[index] = payload
        while len(self.cache) > self.cache_blocks:
            self.cache.popitem(last=False)
        self.fetched_bytes += len(payload)
        if self.progress:
            self.progress(min(self.fetched_bytes, self.size))
        return payload

    def read(self, size=-1):
        if self.closed:
            raise ValueError("I/O operation on closed Drive stream")
        if self.position >= self.size:
            return b""
        if size is None or size < 0:
            size = self.size - self.position
        if size == 0:
            return b""
        target = min(self.size, self.position + int(size))
        output = bytearray()
        while self.position < target:
            index = self.position // self.block_size
            block = self._fetch_block(index)
            block_start = index * self.block_size
            offset = self.position - block_start
            take = min(target - self.position, len(block) - offset)
            if take <= 0:
                raise ValueError("Drive ZIP range could not satisfy the requested read.")
            output.extend(block[offset:offset + take])
            self.position += take
        return bytes(output)

    def close(self):
        self.cache.clear()
        self.closed = True


@contextmanager
def download_drive_zip(link, progress=None, max_bytes=MAX_DOWNLOAD):
    file_id = drive_file_id(link)
    url = "https://drive.usercontent.google.com/download?" + urlencode({
        "id": file_id,
        "export": "download",
        "confirm": "t",
    })
    # Small ZIPs use temporary disk. If the host has too little disk, large ZIPs
    # become a seekable HTTP-range stream instead of being downloaded in full.
    with tempfile.TemporaryDirectory(prefix="vendor-import-") as folder, requests.Session() as session:
        for attempt in range(2):
            with open_response(session, url) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    html = response.raw.read(1024**2 + 1, decode_content=True)
                    if len(html) > 1024**2:
                        raise ValueError("Drive returned a web page instead of a ZIP.")
                    form = ConfirmationForm()
                    form.feed(html.decode("utf-8", errors="replace"))
                    if attempt or not form.action:
                        raise ValueError(
                            "Drive did not return a ZIP. Check download permissions or quota; "
                            "sign-in-only links cannot be imported."
                        )
                    action = urljoin(url, form.action)
                    validate_url(action)
                    url = action + ("&" if "?" in action else "?") + urlencode(form.fields)
                    continue

                length = response.headers.get("Content-Length", "")
                total = int(length) if length.isdigit() else 0
                if total > max_bytes:
                    raise ValueError("Drive ZIP exceeds the 5 GB download limit.")

                free = shutil.disk_usage(folder).free
                if total and total + 64 * CHUNK_SIZE > free:
                    range_url = getattr(response, "_vendor_download_url", url)
                    response.close()
                    stream = DriveRangeFile(session, range_url, total, progress=progress)
                    try:
                        if not zipfile.is_zipfile(stream):
                            raise ValueError("The Google Drive file is not a readable ZIP.")
                        stream.seek(0)
                        yield DiskUpload(stream)
                    finally:
                        stream.close()
                    return

                path = folder + "/Google_Drive_Import.zip"
                downloaded = 0
                with open(path, "wb") as output:
                    for chunk in response.iter_content(CHUNK_SIZE):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise ValueError("Drive ZIP exceeds the 5 GB download limit.")
                        output.write(chunk)
                        if progress and (downloaded <= CHUNK_SIZE or downloaded // CHUNK_SIZE % 8 == 0):
                            progress(downloaded)
                if total and downloaded != total:
                    raise ValueError("Drive download was incomplete. Retry the import.")
                if not zipfile.is_zipfile(path):
                    raise ValueError("The downloaded file is not a readable ZIP.")
                with open(path, "rb") as upload:
                    yield DiskUpload(upload)
                return
