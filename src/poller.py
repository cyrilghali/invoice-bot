"""
Microsoft Graph API inbox poller.

Fetches unread emails (or all recent emails) from the inbox,
extracts attachments and invoice download links from the body,
and returns them for further processing.
"""

import base64
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from auth_setup import get_access_token
from utils import GRAPH_BASE

logger = logging.getLogger(__name__)

# MIME types considered as invoice attachments
INVOICE_MIME_TYPES = {
    "application/pdf",
    "application/x-pdf",  # non-standard alias used by some mail servers
    "image/jpeg",
    "image/png",
    "image/tiff",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "application/vnd.ms-excel",  # xls
    "application/zip",
    "application/x-zip-compressed",
}

# Fallback extension map for download filename guessing
_MIME_TO_EXT = {
    "application/pdf": ".pdf",
    "application/x-pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
}


# ---------------------------------------------------------------------------
# HTML link extractor (stdlib html.parser — no extra dependency)
# ---------------------------------------------------------------------------

class _AnchorExtractor(HTMLParser):
    """Minimal HTML parser that collects all href values from <a> tags."""

    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for attr, value in attrs:
                if attr == "href" and value:
                    self.links.append(value)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    name: str
    content_type: str
    content_bytes: bytes


@dataclass
class Email:
    email_id: str
    sender: str
    subject: str
    received_at: str  # ISO 8601
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def received_datetime(self) -> datetime:
        return datetime.fromisoformat(self.received_at.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Graph client
# ---------------------------------------------------------------------------

class GraphClient:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self._token: str | None = None

    def _get_token(self) -> str:
        if self._token is None:
            self._token = get_access_token(self.client_id)
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def _refresh_token(self) -> None:
        """Force a token refresh on 401."""
        self._token = get_access_token(self.client_id)

    def _get(self, url: str, **kwargs) -> dict:
        for attempt in range(2):
            resp = requests.get(url, headers=self._headers(), timeout=30, **kwargs)
            if resp.status_code == 401 and attempt == 0:
                logger.warning("Token expired, refreshing...")
                self._refresh_token()
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("Graph API request failed after token refresh")

    # -----------------------------------------------------------------------
    # Public fetch method
    # -----------------------------------------------------------------------

    def fetch_emails_with_attachments(
        self,
        whitelisted_senders: list[str] | None = None,
        since: str | None = None,
        link_keywords: list[str] | None = None,
        subject_keywords: list[str] | None = None,
        max_results: int | None = 50,
    ) -> list[Email]:
        """
        Fetch emails from inbox (and junk folder for whitelisted senders)
        that contain invoice file attachments or invoice download links.

        Args:
            whitelisted_senders:  Optional sender whitelist. None = all senders.
            since:                ISO 8601 datetime; only fetch emails after this.
            link_keywords:        Keywords to identify invoice download URLs in body.
            subject_keywords:     Optional list of keywords to filter by subject.
                                  If set, only emails whose subject contains at least
                                  one keyword are processed. Emails with an empty
                                  subject are always passed through.
                                  None = all subjects accepted.
            max_results:          Max emails per page (None = no cap).
        """
        keywords = [k.lower() for k in (link_keywords or [])]
        sender_filter = set(whitelisted_senders) if whitelisted_senders else None
        subject_filter = [k.lower() for k in subject_keywords] if subject_keywords else None

        emails: list[Email] = []

        # Scan inbox (all normal filtering rules apply)
        inbox_emails, inbox_pages = self._scan_folder(
            folder="inbox",
            sender_filter=sender_filter,
            subject_filter=subject_filter,
            keywords=keywords,
            since=since,
            max_results=max_results,
            whitelisted_only=False,
        )
        emails.extend(inbox_emails)

        # Scan additional folders for whitelisted senders only
        # (junk and archive can contain legitimate invoices mis-routed by Outlook)
        extra_pages: dict[str, int] = {}
        if sender_filter:
            for folder in ("junkemail", "archive"):
                folder_emails, folder_pages = self._scan_folder(
                    folder=folder,
                    sender_filter=sender_filter,
                    subject_filter=subject_filter,
                    keywords=keywords,
                    since=since,
                    max_results=max_results,
                    whitelisted_only=True,
                )
                emails.extend(folder_emails)
                extra_pages[folder] = folder_pages

        total_pages = inbox_pages + sum(extra_pages.values())
        parts = f"inbox={inbox_pages}"
        for f, p in extra_pages.items():
            label = "junk" if f == "junkemail" else f
            parts += f" {label}={p}"
        logger.info(
            "Poll complete: pages=%d (%s) emails_with_attachments=%d",
            total_pages, parts, len(emails),
        )
        return emails

    def _scan_folder(
        self,
        folder: str,
        sender_filter: set[str] | None,
        subject_filter: list[str] | None,
        keywords: list[str],
        since: str | None,
        max_results: int | None,
        whitelisted_only: bool,
    ) -> tuple[list[Email], int]:
        """
        Scan a single mail folder and return qualifying emails.

        Args:
            folder:           Graph API well-known folder name (e.g. "inbox", "junkemail").
            sender_filter:    Set of whitelisted sender addresses (lowercase).
            subject_filter:   List of subject keywords (lowercase).
            keywords:         Link detection keywords (lowercase).
            since:            ISO 8601 date floor.
            max_results:      Page size cap.
            whitelisted_only: If True, only process emails from whitelisted senders
                              (used for junk folder to avoid processing spam).

        Returns:
            Tuple of (list of qualifying emails, number of pages fetched).
        """
        folder_label = "junk" if folder == "junkemail" else folder

        # Filtering logic:
        #   - If sender is whitelisted -> always accept (skip subject check)
        #   - Else if whitelisted_only -> skip (junk folder safety)
        #   - Else if subject_filter set and subject non-empty -> must match a keyword
        #   - Else (no filters set, or empty subject) -> accept

        # Build OData filter
        filters: list[str] = []
        if since:
            filters.append(f"receivedDateTime gt {since}")

        filter_clause = f"&$filter={' and '.join(filters)}" if filters else ""

        page_size = min(max_results, 1000) if max_results is not None else 1000

        url: str | None = (
            f"{GRAPH_BASE}/me/mailFolders/{folder}/messages"
            f"?$select=id,sender,subject,receivedDateTime,hasAttachments,body"
            f"{filter_clause}"
            f"&$orderby=receivedDateTime desc"
            f"&$top={page_size}"
        )

        emails: list[Email] = []
        page_count = 0

        while url:
            page_count += 1
            logger.info("Fetching %s page %d", folder_label, page_count)
            data = self._get(url)
            messages = data.get("value", [])
            logger.debug("%s page %d: %d message(s) returned", folder_label, page_count, len(messages))

            for msg in messages:
                sender_address = (
                    msg.get("sender", {})
                    .get("emailAddress", {})
                    .get("address", "")
                    .lower()
                    .strip()
                )

                subject = msg.get("subject") or ""
                sender_whitelisted = sender_filter is not None and sender_address in sender_filter

                if not sender_whitelisted:
                    if whitelisted_only or sender_filter is not None:
                        # Junk folder: only whitelisted senders allowed
                        # Inbox with whitelist: sender not in whitelist -> skip
                        logger.debug(
                            "Skipping email from %s in %s (not whitelisted)", sender_address, folder_label
                        )
                        continue
                    # No whitelist: apply subject keyword filter if configured
                    if subject_filter and subject.strip():
                        subject_lower = subject.lower()
                        if not any(kw in subject_lower for kw in subject_filter):
                            logger.debug(
                                "Skipping email from %s — subject %r matched no keywords",
                                sender_address, subject,
                            )
                            continue

                email = Email(
                    email_id=msg["id"],
                    sender=sender_address,
                    subject=subject,
                    received_at=msg["receivedDateTime"],
                )

                # --- Step 1: file attachments (skip inline images / logos) ---
                file_attachments: list[Attachment] = []
                if msg.get("hasAttachments"):
                    raw_attachments = self._fetch_attachments(msg["id"])
                    file_attachments = [
                        a for a in raw_attachments
                        if a.content_type in INVOICE_MIME_TYPES
                    ]

                # --- Step 2: download links from email body ---
                link_attachments: list[Attachment] = []
                if keywords:
                    body = msg.get("body", {})
                    candidate_urls = self._extract_invoice_links(body, keywords)
                    for url_str in candidate_urls:
                        att = self._download_link(url_str)
                        if att:
                            link_attachments.append(att)

                email.attachments = file_attachments + link_attachments

                if email.attachments:
                    emails.append(email)
                    logger.info(
                        "Email queued: from=%s subject=%r received=%s "
                        "file_attachments=%d link_attachments=%d source=%s",
                        email.sender,
                        email.subject,
                        email.received_at,
                        len(file_attachments),
                        len(link_attachments),
                        folder_label,
                    )
                else:
                    logger.debug(
                        "Email from %s subject=%r in %s — no supported attachments or invoice links, skipping",
                        sender_address,
                        subject,
                        folder_label,
                    )

            # Handle pagination
            url = data.get("@odata.nextLink")

        return emails, page_count

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _fetch_attachments(self, message_id: str) -> list[Attachment]:
        """Fetch all file attachments for a given message, skipping inline ones.

        Graph API does not allow $select=contentBytes on the list endpoint —
        we first list attachments (metadata only), then fetch contentBytes
        individually for each qualifying attachment.
        """
        list_url = (
            f"{GRAPH_BASE}/me/messages/{message_id}/attachments"
            f"?$select=id,name,contentType,size,isInline"
        )
        try:
            data = self._get(list_url)
        except requests.HTTPError as e:
            logger.error("Failed to list attachments for message %s: %s", message_id, e)
            return []

        attachments = []
        for att in data.get("value", []):
            # Only process binary file attachments
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue

            # Skip inline attachments (company logos, banners embedded in body)
            if att.get("isInline"):
                logger.debug("Skipping inline attachment: %s", att.get("name"))
                continue

            # Skip unsupported MIME types early (avoid unnecessary downloads)
            if att.get("contentType", "").split(";")[0].strip() not in INVOICE_MIME_TYPES:
                logger.debug("Skipping attachment %s: unsupported type %s", att.get("name"), att.get("contentType"))
                continue

            # Skip very large attachments (> 20 MB)
            if att.get("size", 0) > 20 * 1024 * 1024:
                logger.warning(
                    "Skipping attachment %s: too large (%d bytes)",
                    att.get("name"),
                    att.get("size"),
                )
                continue

            # Fetch contentBytes individually
            # Note: $select cannot be used here — contentBytes is not a property on
            # the base microsoft.graph.attachment type, so Graph returns 400 if you
            # try to select it. Fetch the full attachment object instead.
            att_id = att["id"]
            try:
                att_detail = self._get(
                    f"{GRAPH_BASE}/me/messages/{message_id}/attachments/{att_id}"
                )
            except requests.HTTPError as e:
                logger.warning("Failed to fetch attachment %s content: %s", att.get("name"), e)
                continue

            content_b64 = att_detail.get("contentBytes", "")
            try:
                content_bytes = base64.b64decode(content_b64)
            except Exception:
                logger.warning("Could not decode attachment %s", att.get("name"))
                continue

            att_name = att_detail.get("name", att.get("name", "attachment"))
            att_ct = att_detail.get("contentType", "application/octet-stream")
            logger.info(
                "Attachment fetched: name=%r type=%s size=%d bytes",
                att_name, att_ct, len(content_bytes),
            )
            attachments.append(
                Attachment(
                    name=att_name,
                    content_type=att_ct,
                    content_bytes=content_bytes,
                )
            )

        logger.debug("Fetched %d attachment(s) for message %s", len(attachments), message_id)
        return attachments

    def _extract_invoice_links(self, body: dict, keywords: list[str]) -> list[str]:
        """
        Extract candidate invoice download URLs from an email body.

        Parses HTML bodies with html.parser; falls back to regex for plain text.
        Only returns URLs whose string contains at least one of the given keywords.

        Args:
            body: Graph API body object {"contentType": "html"|"text", "content": "..."}.
            keywords: Lowercase keywords to match against each URL.

        Returns:
            Deduplicated list of matching URLs.
        """
        content_type = body.get("contentType", "text").lower()
        content = body.get("content", "")

        if not content:
            return []

        raw_urls: list[str] = []

        if content_type == "html":
            parser = _AnchorExtractor()
            try:
                parser.feed(content)
                raw_urls = parser.links
            except Exception as e:
                logger.warning("HTML parsing failed, falling back to regex: %s", e)
                raw_urls = re.findall(r'https?://[^\s"\'<>]+', content)
        else:
            # Plain text: extract raw URLs with a simple regex
            raw_urls = re.findall(r'https?://[^\s"\'<>]+', content)

        # Filter by keywords and deduplicate (preserve order)
        seen: set[str] = set()
        filtered: list[str] = []
        for url in raw_urls:
            url_lower = url.lower()
            if any(kw in url_lower for kw in keywords) and url not in seen:
                seen.add(url)
                filtered.append(url)

        if filtered:
            logger.debug(
                "Extracted %d candidate invoice URL(s) from email body: %s",
                len(filtered),
                filtered,
            )

        return filtered

    def _download_link(self, url: str) -> Attachment | None:
        """
        Download a file from a URL and return it as an Attachment.

        Follows redirects (up to requests' default of 30, capped at 5 by max_redirects).
        Returns None if the content-type is unsupported or the download fails.

        Args:
            url: URL to download.

        Returns:
            Attachment on success, None on failure.
        """
        logger.info("Attempting to download invoice from link: %s", url)
        try:
            resp = requests.get(
                url,
                allow_redirects=True,
                timeout=30,
                stream=True,
                headers={"User-Agent": "Mozilla/5.0 (invoice-bot)"},
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Failed to download invoice link %s: %s", url, e)
            return None

        # Determine content-type (strip charset/boundary suffixes)
        raw_ct = resp.headers.get("Content-Type", "")
        content_type = raw_ct.split(";")[0].strip().lower()

        if content_type not in INVOICE_MIME_TYPES:
            logger.warning(
                "Skipping download from %s: unsupported content-type %r",
                url,
                content_type,
            )
            return None

        # Derive filename
        filename = _filename_from_response(resp, url, content_type)

        try:
            content_bytes = resp.content
        except Exception as e:
            logger.warning("Failed to read response body from %s: %s", url, e)
            return None

        logger.info(
            "Downloaded invoice from link: filename=%r content_type=%r size=%d bytes",
            filename,
            content_type,
            len(content_bytes),
        )
        return Attachment(name=filename, content_type=content_type, content_bytes=content_bytes)


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _filename_from_response(resp: requests.Response, url: str, content_type: str) -> str:
    """
    Derive a filename for a downloaded file.

    Priority:
      1. Content-Disposition header  (filename= or filename*=)
      2. Last path segment of the final (post-redirect) URL
      3. Generic name with extension guessed from MIME type
    """
    # 1. Content-Disposition
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        # filename*=UTF-8''encoded-name  (RFC 5987)
        m = re.search(r"filename\*\s*=\s*(?:[^']*'[^']*')?(.+)", cd, re.IGNORECASE)
        if m:
            from urllib.parse import unquote
            return unquote(m.group(1).strip().strip('"'))
        # filename="name.pdf"
        m = re.search(r'filename\s*=\s*"?([^";\r\n]+)"?', cd, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # 2. URL path segment (use final URL after redirects)
    final_url = resp.url or url
    path = urlparse(final_url).path
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    if segment and "." in segment:
        return segment

    # 3. Fallback: generic name + extension from MIME type
    ext = _MIME_TO_EXT.get(content_type) or mimetypes.guess_extension(content_type) or ""
    return f"invoice{ext}"
