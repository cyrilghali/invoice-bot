"""
Invoice classifier using Claude Haiku (Anthropic).

Analyses attachment content and determines whether a file is an invoice,
credit note, or receipt — or something else (contract, mandate, photo, etc.).

Supported formats:
  - PDF       : text extracted with pdfplumber (first 2 pages)
  - Images    : sent as base64 to Claude vision
  - XLSX/XLS  : cell text extracted with openpyxl
  - ZIP       : unpacked in pipeline.py before reaching this module; each member
                is classified individually as its own file

Return values from is_invoice():
  "invoice"  — Claude is confident this is an invoice. Upload to normal folder.
  "review"   — Uncertain (low confidence), classifier crashed, unsupported type,
               or no text could be extracted. Upload to _a_verifier/ for manual check.
  "rejected" — Claude is confident this is NOT an invoice. Upload to _a_verifier/.
"""

import base64
import io
import json
import logging

import anthropic
import pdfplumber

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_TEXT_CHARS = 3000  # ~800 tokens — enough for a typical invoice header

# Names of the owner's own businesses — these are the BUYER on invoices, not the supplier.
# If Claude extracts one of these as the supplier, discard it and fall back to sender domain.
_OWNER_BUSINESS_NAMES: set[str] = {
    "saint cyril", "st cyril", "saint-cyril", "st-cyril",
    "la favola", "colisee", "le colisee", "colisée", "le colisée",
    "sci saint karas", "saint karas",
}

SYSTEM_PROMPT = (
    "Tu es un assistant comptable expert. "
    "Ton rôle est de déterminer si un document est une facture, un avoir ou un reçu "
    "(= document commercial émis par un fournisseur indiquant un montant dû ou payé), "
    "d'extraire la date du document (date de facturation, pas la date d'échéance), "
    "d'extraire le nom du fournisseur/émetteur (la société qui a émis la facture), "
    "et d'extraire les montants HT, TVA et TTC ainsi que la devise. "
    "Pour les avoirs, retourne les montants en négatif. "
    "Réponds UNIQUEMENT en JSON valide, sans texte autour : "
    '{"is_invoice": true/false, "confidence": 0.0-1.0, "reason": "...", '
    '"invoice_date": "YYYY-MM-DD or null", "supplier": "nom du fournisseur or null", '
    '"amount_ht": <number or null>, "amount_tva": <number or null>, '
    '"amount_ttc": <number or null>, "currency": "EUR or null"}'
)

USER_PROMPT_TEXT = (
    "Voici le contenu extrait d'un document. "
    "Est-ce une facture, un avoir ou un reçu ?\n\n{text}"
)

USER_PROMPT_IMAGE = (
    "Voici une image d'un document. "
    "Est-ce une facture, un avoir ou un reçu ?"
)

_HINT_SUFFIX = (
    "\n\nLe document provient probablement du fournisseur : «{hint}». "
    "Confirme ce nom ou corrige-le si le document mentionne explicitement un nom différent."
)


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _extract_pdf_text(data: bytes) -> str:
    """Extract text from the first 2 pages of a PDF."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            total_pages = len(pdf.pages)
            pages = pdf.pages[:2]
            text = "\n".join(
                (page.extract_text() or "") for page in pages
            ).strip()
        truncated = text[:MAX_TEXT_CHARS]
        logger.debug(
            "PDF extraction: total_pages=%d pages_read=%d chars_extracted=%d chars_sent=%d",
            total_pages, min(2, total_pages), len(text), len(truncated),
        )
        return truncated
    except Exception as e:
        logger.warning("PDF text extraction failed: %s", e)
        return ""


def _extract_xlsx_text(data: bytes) -> str:
    """Extract cell text from an Excel file (first sheet, first 100 rows)."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        lines = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):  # type: ignore[union-attr]
            if i >= 100:
                break
            row_text = " ".join(str(c) for c in row if c is not None)
            if row_text.strip():
                lines.append(row_text)
        text = "\n".join(lines)[:MAX_TEXT_CHARS]
        logger.debug("XLSX extraction: rows_read=%d chars_sent=%d", len(lines), len(text))
        return text
    except Exception as e:
        logger.warning("XLSX text extraction failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Claude API calls
# ---------------------------------------------------------------------------

# Return type: (is_invoice, confidence, reason, invoice_date, supplier,
#               amount_ht, amount_tva, amount_ttc, currency)
_ClassifyResult = tuple[bool, float, str, str | None, str | None, float | None, float | None, float | None, str | None]


def _classify_text(client: anthropic.Anthropic, text: str, hint_supplier: str | None = None) -> _ClassifyResult:
    """
    Send extracted text to Claude Haiku and parse the JSON response.
    Returns (is_invoice, confidence, reason, invoice_date, supplier, amount_ht, amount_tva, amount_ttc, currency).
    """
    if not text.strip():
        logger.debug("No text to classify — returning review result immediately")
        return False, 0.0, "No text extracted — sending to review", None, None, None, None, None, None

    prompt = USER_PROMPT_TEXT.format(text=text)
    if hint_supplier:
        prompt += _HINT_SUFFIX.format(hint=hint_supplier)

    logger.debug("Sending %d chars to Claude (%s) for text classification (hint=%r)", len(text), MODEL, hint_supplier)
    message = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    logger.debug("Claude raw response: %s", raw)
    return _parse_response(raw)


def _classify_image(
    client: anthropic.Anthropic, data: bytes, media_type: str, hint_supplier: str | None = None
) -> _ClassifyResult:
    """
    Send an image to Claude Haiku vision and parse the JSON response.
    Returns (is_invoice, confidence, reason, invoice_date, supplier, amount_ht, amount_tva, amount_ttc, currency).
    """
    logger.debug(
        "Sending image to Claude (%s) for vision classification: media_type=%s size=%d bytes (hint=%r)",
        MODEL, media_type, len(data), hint_supplier,
    )
    b64 = base64.standard_b64encode(data).decode("utf-8")
    image_prompt = USER_PROMPT_IMAGE
    if hint_supplier:
        image_prompt += _HINT_SUFFIX.format(hint=hint_supplier)
    message = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": image_prompt},
                ],
            }
        ],
    )
    raw = message.content[0].text.strip()
    logger.debug("Claude raw response: %s", raw)
    return _parse_response(raw)


def _parse_amount(value) -> float | None:
    """Parse a JSON amount value to float, returning None if invalid or non-finite."""
    if value is None:
        return None
    try:
        f = float(value)
        import math
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _parse_response(raw: str) -> _ClassifyResult:
    """Parse Claude's JSON response.
    Returns (is_invoice, confidence, reason, invoice_date, supplier, amount_ht, amount_tva, amount_ttc, currency).
    """
    try:
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(clean)
        is_inv = bool(data.get("is_invoice", True))
        conf = float(data.get("confidence", 0.5))
        reason = str(data.get("reason", ""))

        # invoice_date — validate YYYY-MM-DD format
        raw_date = data.get("invoice_date")
        invoice_date = str(raw_date).strip() if raw_date and str(raw_date).strip().lower() != "null" else None
        if invoice_date:
            import re as _re
            if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", invoice_date):
                logger.debug("invoice_date %r ignored — not YYYY-MM-DD format", invoice_date)
                invoice_date = None

        # supplier — clean up whitespace, cap length, reject "null" strings
        raw_supplier = data.get("supplier")
        if raw_supplier and str(raw_supplier).strip().lower() not in ("null", "none", "n/a", ""):
            supplier: str | None = str(raw_supplier).strip()[:80]
        else:
            supplier = None

        # Discard supplier if it matches one of the owner's own business names
        if supplier:
            sup_lower = supplier.lower()
            if any(owned in sup_lower for owned in _OWNER_BUSINESS_NAMES):
                logger.debug("Discarding owner business name as supplier: %r", supplier)
                supplier = None

        # Amounts — accept any finite number (negative for credit notes)
        amount_ht = _parse_amount(data.get("amount_ht"))
        amount_tva = _parse_amount(data.get("amount_tva"))
        amount_ttc = _parse_amount(data.get("amount_ttc"))

        # Currency — default to EUR if not specified or null
        raw_currency = data.get("currency")
        if raw_currency and str(raw_currency).strip().upper() not in ("NULL", "NONE", ""):
            currency: str | None = str(raw_currency).strip().upper()[:8]
        else:
            currency = None

        return is_inv, conf, reason, invoice_date, supplier, amount_ht, amount_tva, amount_ttc, currency

    except Exception as e:
        logger.warning("Failed to parse classifier response %r: %s", raw, e)
        return True, 0.0, "Parse error — sending to review", None, None, None, None, None, None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def is_invoice(
    attachment,
    config: dict,
    hint_supplier: str | None = None,
) -> tuple[str, str | None, str | None, float | None, float | None, float | None, str | None]:
    """
    Classify an attachment using Claude Haiku.
    Extracts invoice date, supplier, and amounts from the document.

    Args:
        attachment:     Attachment dataclass with .name, .content_type, .content_bytes
        config:         Full app config dict (needs config["classifier"]["api_key"])
        hint_supplier:  Optional canonical supplier name hint (from sender_suppliers map).

    Returns:
        (status, invoice_date, supplier, amount_ht, amount_ttc, amount_tva, currency)
          status:       "invoice" / "review" / "rejected"
          invoice_date: "YYYY-MM-DD" or None
          supplier:     Supplier name or None
          amount_ht:    Pre-tax total (float) or None
          amount_ttc:   Total incl. tax (float) or None
          amount_tva:   Tax amount (float) or None
          currency:     e.g. "EUR" or None
    """
    classifier_cfg = config.get("classifier", {})
    api_key = classifier_cfg.get("api_key", "")
    threshold = float(classifier_cfg.get("confidence_threshold", 0.5))

    if not api_key or api_key == "YOUR_ANTHROPIC_API_KEY_HERE":
        logger.warning("Classifier API key not configured — sending to review by default")
        return "review", None, None, None, None, None, None

    client = anthropic.Anthropic(api_key=api_key)

    name_lower = attachment.name.lower()
    ct = attachment.content_type.split(";")[0].strip().lower()
    data = attachment.content_bytes
    size_kb = len(data) / 1024

    logger.info(
        "Classifying: file=%r type=%s size=%.1f KB threshold=%.2f hint=%r",
        attachment.name, ct, size_kb, threshold, hint_supplier,
    )

    try:
        if ct == "application/pdf" or name_lower.endswith(".pdf"):
            text = _extract_pdf_text(data)
            is_inv, conf, reason, invoice_date, supplier, amount_ht, amount_tva, amount_ttc, currency = _classify_text(client, text, hint_supplier)

        elif ct in ("image/jpeg", "image/jpg") or name_lower.endswith((".jpg", ".jpeg")):
            is_inv, conf, reason, invoice_date, supplier, amount_ht, amount_tva, amount_ttc, currency = _classify_image(client, data, "image/jpeg", hint_supplier)

        elif ct == "image/png" or name_lower.endswith(".png"):
            is_inv, conf, reason, invoice_date, supplier, amount_ht, amount_tva, amount_ttc, currency = _classify_image(client, data, "image/png", hint_supplier)

        elif ct == "image/tiff" or name_lower.endswith(".tiff"):
            is_inv, conf, reason, invoice_date, supplier, amount_ht, amount_tva, amount_ttc, currency = _classify_image(client, data, "image/tiff", hint_supplier)

        elif ct in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        ) or name_lower.endswith((".xlsx", ".xls")):
            text = _extract_xlsx_text(data)
            is_inv, conf, reason, invoice_date, supplier, amount_ht, amount_tva, amount_ttc, currency = _classify_text(client, text, hint_supplier)

        else:
            logger.info(
                "Classifying: file=%r — unsupported type %s, routing to review",
                attachment.name, ct,
            )
            return "review", None, None, None, None, None, None

        if is_inv and conf >= threshold:
            status = "invoice"
        elif not is_inv and conf >= threshold:
            status = "rejected"
        else:
            status = "review"

        logger.info(
            "Classification result: file=%r status=%s is_invoice=%s confidence=%.2f "
            "invoice_date=%r supplier=%r amount_ht=%s amount_ttc=%s amount_tva=%s currency=%r reason=%r",
            attachment.name, status, is_inv, conf, invoice_date, supplier,
            amount_ht, amount_ttc, amount_tva, currency, reason,
        )

        return status, invoice_date, supplier, amount_ht, amount_ttc, amount_tva, currency

    except Exception as e:
        logger.warning(
            "Classifier failed for %r: %s — routing to review",
            attachment.name, e,
        )
        return "review", None, None, None, None, None, None
