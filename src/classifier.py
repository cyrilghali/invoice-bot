"""
Invoice classifier using Claude CLI (claude -p).

Analyses attachment content and determines whether a file is an invoice,
credit note, or receipt — or something else (contract, mandate, photo, etc.).

Supported formats:
  - PDF       : text extracted with pdfplumber (first 2 pages); if too little
                text is found (scanned PDF), falls back to Claude vision via Read tool
  - Images    : saved to temp file, read by Claude via Read tool
  - XLSX/XLS  : cell text extracted with openpyxl
  - ZIP       : unpacked in pipeline.py before reaching this module; each member
                is classified individually as its own file

Return values from is_invoice():
  "invoice"  — Claude is confident this is an invoice. Upload to normal folder.
  "review"   — Uncertain (low confidence), classifier crashed, unsupported type,
               or no text could be extracted. Upload to _a_verifier/ for manual check.
  "rejected" — Claude is confident this is NOT an invoice. Upload to _a_verifier/.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import subprocess
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from poller import Attachment

import pdfplumber

from utils import normalize_content_type

logger = logging.getLogger(__name__)

MODEL = "opus"
MAX_TEXT_CHARS = 6000  # ~1500 tokens — Opus handles large context well
MIN_TEXT_CHARS = 50  # below this, PDF is likely scanned — fall back to vision
CLI_TIMEOUT = 60  # seconds — CLI has startup overhead + Opus is slower than Haiku
RETRY_CONFIDENCE_LOW = 0.3  # below this on first pass, retry with explicit prompt
RETRY_CONFIDENCE_HIGH = 0.5  # above this, first-pass result is accepted

SYSTEM_PROMPT = (
    "Tu es un assistant comptable expert. "
    "Ton rôle est de déterminer si un document est un document comptable : "
    "facture, avoir, reçu, ou relevé de factures "
    "(= tout document commercial émis par un fournisseur lié à la facturation). "
    "Extrais la date du document (date de facturation ou date du relevé, pas la date d'échéance), "
    "le nom du fournisseur/émetteur, "
    "et les montants HT, TVA et TTC ainsi que la devise. "
    "Pour le fournisseur, retourne UNIQUEMENT le nom commercial court en casse titre "
    "(ex: «Fresca», pas «FRESCA», pas «S.A.S. au capital de...», pas «ROUQUETTE SARL SAINT CYRIL» mais «Rouquette»). "
    "Pour les avoirs, retourne les montants en négatif. "
    "Réponds UNIQUEMENT en JSON valide, sans texte autour : "
    '{"is_invoice": true/false, "confidence": 0.0-1.0, "reason": "...", '
    '"invoice_date": "YYYY-MM-DD or null", "supplier": "nom commercial court or null", '
    '"amount_ht": <number or null>, "amount_tva": <number or null>, '
    '"amount_ttc": <number or null>, "currency": "EUR or null"} '
    "is_invoice=true pour les factures, avoirs, reçus ET relevés de factures. "
    "is_invoice=false pour les CGU, CGV, contrats, mandats, devis, bons de commande, "
    "courriers, publicités, et tout document non lié à la facturation."
)

_HINT_SUFFIX = (
    "\n\nLe document provient probablement du fournisseur : «{hint}». "
    "Confirme ce nom ou corrige-le si le document mentionne explicitement un nom différent."
)

_EXT_MAP = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/png": ".png", "image/tiff": ".tiff",
}


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _extract_pdf_text(data: bytes) -> str:
    """Extract text from the first 2 pages of a PDF.

    Returns empty string if extraction fails or produces only garbled
    text (e.g., (cid:XXXX) from embedded fonts pdfplumber can't decode).
    """
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages[:2]
            text = "\n".join((page.extract_text() or "") for page in pages).strip()
        # Strip garbled cid references — sign of embedded fonts that couldn't be decoded
        clean = re.sub(r"\(cid:\d+\)", "", text).strip()
        if len(clean) < len(text) * 0.8:
            logger.info("PDF text is mostly garbled cid references (%d/%d chars) — treating as empty",
                        len(text) - len(clean), len(text))
            return ""
        return clean[:MAX_TEXT_CHARS]
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
        return "\n".join(lines)[:MAX_TEXT_CHARS]
    except Exception as e:
        logger.warning("XLSX text extraction failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Claude CLI
# ---------------------------------------------------------------------------

# (is_invoice, confidence, reason, invoice_date, supplier, amount_ht, amount_ttc, amount_tva, currency)
# NOTE: order is HT, TTC, TVA — consistent with the public is_invoice() API.
_ClassifyResult = tuple[bool, float, str, str | None, str | None, float | None, float | None, float | None, str | None]

_EMPTY_RESULT: _ClassifyResult = (False, 0.0, "No text extracted — sending to review", None, None, None, None, None, None)


def _run_claude_cli(
    prompt: str,
    system_prompt: str,
    model: str = MODEL,
    tools: str = "",
    timeout: int = CLI_TIMEOUT,
) -> str:
    """Run claude -p and return raw stdout. Raises on failure.

    Passes the prompt via stdin and system prompt via temp file to avoid
    shell argument length limits and special character issues.
    """
    # Write system prompt to temp file to avoid shell escaping issues
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as spf:
        spf.write(system_prompt)
        sp_path = spf.name
    try:
        cmd = ["claude", "-p", "--model", model, "--system-prompt-file", sp_path,
               "--tools", tools or "", "--permission-mode", "bypassPermissions"]
        logger.debug("Running claude CLI: model=%s tools=%r timeout=%ds prompt_len=%d",
                     model, tools, timeout, len(prompt))
        for attempt in range(3):
            result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                return result.stdout.strip()
            logger.warning("claude CLI attempt %d/3 failed (code %d) stderr=%r stdout=%r",
                           attempt + 1, result.returncode, result.stderr.strip()[:200], result.stdout.strip()[:200])
            if attempt < 2:
                import time
                time.sleep(5)
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    finally:
        os.unlink(sp_path)


def _classify_prompt(
    prompt: str,
    model: str = MODEL,
    tools: str = "",
    hint_supplier: str | None = None,
    owner_names: set[str] | None = None,
) -> _ClassifyResult:
    """Send a prompt to Claude and parse the JSON response."""
    if hint_supplier:
        prompt += _HINT_SUFFIX.format(hint=hint_supplier)
    raw = _run_claude_cli(prompt, SYSTEM_PROMPT, model=model, tools=tools)
    logger.debug("Claude raw response: %s", raw)
    return _parse_response(raw, owner_names=owner_names)


def _classify_text(text: str, model: str = MODEL, hint_supplier: str | None = None,
                   owner_names: set[str] | None = None) -> _ClassifyResult:
    """Classify extracted text."""
    if not text.strip():
        return _EMPTY_RESULT
    prompt = f"Voici le contenu extrait d'un document. Est-ce une facture, un avoir ou un reçu ?\n\n{text}"
    return _classify_prompt(prompt, model=model, hint_supplier=hint_supplier, owner_names=owner_names)


def _classify_file(data: bytes, suffix: str, prompt_template: str, model: str = MODEL,
                   hint_supplier: str | None = None, owner_names: set[str] | None = None) -> _ClassifyResult:
    """Save data to temp file, ask Claude to read it, clean up."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        prompt = prompt_template.format(path=tmp_path)
        return _classify_prompt(prompt, model=model, tools="Read",
                                hint_supplier=hint_supplier, owner_names=owner_names)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _classify_image(data: bytes, media_type: str, model: str = MODEL,
                    hint_supplier: str | None = None, owner_names: set[str] | None = None) -> _ClassifyResult:
    """Classify an image file via Claude vision."""
    ext = _EXT_MAP.get(media_type, ".jpg")
    return _classify_file(data, ext, "Lis le fichier image situé à {path} et analyse-le. "
                          "Est-ce une facture, un avoir ou un reçu ?",
                          model=model, hint_supplier=hint_supplier, owner_names=owner_names)


def _classify_pdf_vision(data: bytes, model: str = MODEL,
                         hint_supplier: str | None = None, owner_names: set[str] | None = None) -> _ClassifyResult:
    """Classify a scanned PDF via Claude vision."""
    return _classify_file(data, ".pdf", "Lis le fichier PDF situé à {path} et analyse-le visuellement. "
                          "Est-ce une facture, un avoir ou un reçu ?",
                          model=model, hint_supplier=hint_supplier, owner_names=owner_names)


def _retry_classify(text: str, model: str = MODEL, hint_supplier: str | None = None,
                    owner_names: set[str] | None = None) -> _ClassifyResult:
    """Second-pass classification with a more explicit prompt."""
    prompt = ("Tu as analysé ce document et tu n'étais pas sûr. "
              "Réexamine attentivement le contenu ci-dessous. "
              "Même si le document est incomplet ou mal formaté, donne ta meilleure estimation. "
              "Un document avec un montant, une date et un émetteur est très probablement une facture."
              f"\n\n{text}")
    return _classify_prompt(prompt, model=model, hint_supplier=hint_supplier, owner_names=owner_names)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_amount(value) -> float | None:
    """Parse a JSON amount value to float, returning None if invalid or non-finite."""
    if value is None:
        return None
    try:
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _parse_response(raw: str, owner_names: set[str] | None = None) -> _ClassifyResult:
    """Parse Claude's JSON response."""
    try:
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(clean)
        is_inv = bool(data.get("is_invoice", True))
        conf = float(data.get("confidence", 0.5))
        reason = str(data.get("reason", ""))

        # invoice_date
        raw_date = data.get("invoice_date")
        invoice_date = str(raw_date).strip() if raw_date and str(raw_date).strip().lower() != "null" else None
        if invoice_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", invoice_date):
            invoice_date = None

        # supplier
        raw_supplier = data.get("supplier")
        supplier: str | None = None
        if raw_supplier and str(raw_supplier).strip().lower() not in ("null", "none", "n/a", ""):
            supplier = str(raw_supplier).strip()[:80]

        # Discard supplier if it matches owner's business name
        names = owner_names or set()
        if supplier and any(owned in supplier.lower() for owned in names):
            supplier = None

        # amounts & currency
        amount_ht = _parse_amount(data.get("amount_ht"))
        amount_tva = _parse_amount(data.get("amount_tva"))
        amount_ttc = _parse_amount(data.get("amount_ttc"))

        raw_currency = data.get("currency")
        currency: str | None = None
        if raw_currency and str(raw_currency).strip().upper() not in ("NULL", "NONE", ""):
            currency = str(raw_currency).strip().upper()[:8]

        return is_inv, conf, reason, invoice_date, supplier, amount_ht, amount_ttc, amount_tva, currency

    except Exception as e:
        logger.warning("Failed to parse classifier response %r: %s", raw, e, exc_info=True)
        return True, 0.0, "Parse error — sending to review", None, None, None, None, None, None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

_PDF_TYPES = {"application/pdf", "application/x-pdf"}
_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/tiff"}
_XLSX_TYPES = {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               "application/vnd.ms-excel"}


def is_invoice(
    attachment: Attachment,
    config: dict,
    hint_supplier: str | None = None,
) -> tuple[str, str | None, str | None, float | None, float | None, float | None, str | None]:
    """
    Classify an attachment using Claude via the claude CLI.

    Returns:
        (status, invoice_date, supplier, amount_ht, amount_ttc, amount_tva, currency)
          status: "invoice" / "review" / "rejected"
    """
    classifier_cfg = config.get("classifier", {})
    model = classifier_cfg.get("model", MODEL)
    threshold = float(classifier_cfg.get("confidence_threshold", 0.5))

    raw_names: list[str] = classifier_cfg.get("owner_business_names") or []
    owner_names = {n.lower().strip() for n in raw_names} if raw_names else set()

    name_lower = attachment.name.lower()
    ct = normalize_content_type(attachment.content_type)
    data = attachment.content_bytes

    logger.info("Classifying: file=%r type=%s size=%.1f KB hint=%r model=%s",
                attachment.name, ct, len(data) / 1024, hint_supplier, model)

    try:
        text = None  # track extracted text for potential retry

        if ct in _PDF_TYPES or name_lower.endswith(".pdf"):
            text = _extract_pdf_text(data)
            if len(text) < MIN_TEXT_CHARS:
                logger.info("PDF text too short (%d chars) — falling back to vision", len(text))
                result = _classify_pdf_vision(data, model, hint_supplier, owner_names)
                text = None
            else:
                result = _classify_text(text, model, hint_supplier, owner_names)

        elif ct in _IMAGE_TYPES or name_lower.endswith((".jpg", ".jpeg", ".png", ".tiff")):
            media = ct if ct in _IMAGE_TYPES else {
                "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "tiff": "image/tiff",
            }.get(name_lower.rsplit(".", 1)[-1], "image/jpeg")
            result = _classify_image(data, media, model, hint_supplier, owner_names)

        elif ct in _XLSX_TYPES or name_lower.endswith((".xlsx", ".xls")):
            text = _extract_xlsx_text(data)
            result = _classify_text(text, model, hint_supplier, owner_names)

        else:
            logger.info("Unsupported type %s for %r, routing to review", ct, attachment.name)
            return "review", None, None, None, None, None, None

        is_inv, conf, reason, invoice_date, supplier, amount_ht, amount_ttc, amount_tva, currency = result

        # Two-pass retry: borderline confidence with text available
        if RETRY_CONFIDENCE_LOW <= conf < RETRY_CONFIDENCE_HIGH and text:
            logger.info("Borderline confidence %.2f for %r — retrying", conf, attachment.name)
            is_inv, conf, reason, invoice_date, supplier, amount_ht, amount_ttc, amount_tva, currency = (
                _retry_classify(text, model, hint_supplier, owner_names))

        status = "invoice" if is_inv and conf >= threshold else "rejected" if not is_inv and conf >= threshold else "review"

        logger.info("Result: file=%r status=%s confidence=%.2f supplier=%r date=%r",
                    attachment.name, status, conf, supplier, invoice_date)

        return status, invoice_date, supplier, amount_ht, amount_ttc, amount_tva, currency

    except Exception as e:
        logger.warning("Classifier failed for %r: %s — routing to review", attachment.name, e)
        return "review", None, None, None, None, None, None
