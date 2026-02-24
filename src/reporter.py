"""
Monthly invoice report builder.

Builds an HTML email summarizing all invoices collected during a given month
and saves it as a draft in the father's Outlook account via Graph API.
The draft is addressed to the accountant but NOT sent automatically — it must
be reviewed and sent manually from Outlook.

Attachment strategy:
- If total size of all PDFs <= 20MB: attach them all directly.
- Otherwise: include only Drive links in the email body.
"""

import base64
import logging
from collections import defaultdict
from datetime import datetime

from poller import GraphClient
from utils import MONTH_NAMES_FR

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB


def _month_label(year: int, month: int) -> str:
    return f"{MONTH_NAMES_FR[month]} {year}"


def _fmt_amount(value: float | None, currency: str = "EUR") -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} {currency}".replace(",", "\u202f")  # narrow no-break space as thousands sep


def _build_supplier_summary(invoices: list[dict]) -> str:
    """
    Build the per-supplier summary HTML table and the grand total row text.
    Returns (summary_table_html, grand_total_line).
    """
    totals: dict[str, dict] = defaultdict(lambda: {"count": 0, "ht": 0.0, "tva": 0.0, "ttc": 0.0, "has": False, "currency": "EUR"})

    for inv in invoices:
        sup = inv.get("supplier") or inv.get("sender", "?")
        currency = inv.get("currency") or "EUR"
        t = totals[sup]
        t["count"] += 1
        t["currency"] = currency
        if inv.get("amount_ht") is not None:
            t["ht"] += inv["amount_ht"]; t["has"] = True
        if inv.get("amount_tva") is not None:
            t["tva"] += inv["amount_tva"]; t["has"] = True
        if inv.get("amount_ttc") is not None:
            t["ttc"] += inv["amount_ttc"]; t["has"] = True

    td = 'style="padding:6px 12px;border-bottom:1px solid #eee;'
    th = 'style="padding:8px 12px;text-align:'

    rows = ""
    grand_ht = grand_tva = grand_ttc = 0.0
    has_any = False
    ref_currency = "EUR"
    for sup, t in sorted(totals.items()):
        ht_s  = _fmt_amount(t["ht"],  t["currency"]) if t["has"] else "—"
        tva_s = _fmt_amount(t["tva"], t["currency"]) if t["has"] else "—"
        ttc_s = _fmt_amount(t["ttc"], t["currency"]) if t["has"] else "—"
        rows += f"""
        <tr>
          <td {td}text-align:left;">{sup}</td>
          <td {td}text-align:center;">{t['count']}</td>
          <td {td}text-align:right;">{ht_s}</td>
          <td {td}text-align:right;">{tva_s}</td>
          <td {td}text-align:right;">{ttc_s}</td>
        </tr>"""
        grand_ht  += t["ht"]
        grand_tva += t["tva"]
        grand_ttc += t["ttc"]
        if t["has"]:
            has_any = True
            ref_currency = t["currency"]

    g_ht  = _fmt_amount(grand_ht,  ref_currency) if has_any else "—"
    g_tva = _fmt_amount(grand_tva, ref_currency) if has_any else "—"
    g_ttc = _fmt_amount(grand_ttc, ref_currency) if has_any else "—"

    total_row = f"""
        <tr style="background:#2c5f8a;color:white;font-weight:bold;">
          <td style="padding:8px 12px;">TOTAL</td>
          <td style="padding:8px 12px;text-align:center;">{len(invoices)}</td>
          <td style="padding:8px 12px;text-align:right;">{g_ht}</td>
          <td style="padding:8px 12px;text-align:right;">{g_tva}</td>
          <td style="padding:8px 12px;text-align:right;">{g_ttc}</td>
        </tr>"""

    table = f"""
  <h3 style="color:#2c5f8a;margin-top:30px;">Récapitulatif par fournisseur</h3>
  <table style="border-collapse:collapse;width:100%;margin:10px 0;">
    <thead>
      <tr style="background:#2c5f8a;color:white;">
        <th {th}left;">Fournisseur</th>
        <th {th}center;">Nb</th>
        <th {th}right;">Total HT</th>
        <th {th}right;">Total TVA</th>
        <th {th}right;">Total TTC</th>
      </tr>
    </thead>
    <tbody>
      {rows}
      {total_row}
    </tbody>
  </table>"""

    return table


def _build_html_body(
    invoices: list[dict],
    year: int,
    month: int,
    drive_folder_link: str,
    attachments_included: bool,
) -> str:
    month_label = _month_label(year, month)
    rows = ""
    for inv in invoices:
        # Prefer invoice date extracted from the document; fall back to received date
        raw_date = inv.get("invoice_date") or inv["received_at"]
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            date_str = dt.strftime("%d/%m/%Y")
        except Exception:
            date_str = raw_date

        drive_link = inv.get("drive_web_link", "")
        link_cell = (
            f'<a href="{drive_link}">Voir</a>' if drive_link else "—"
        )
        supplier_label = inv.get("supplier") or inv["sender"]
        currency = inv.get("currency") or "EUR"
        ht_cell  = _fmt_amount(inv.get("amount_ht"),  currency)
        ttc_cell = _fmt_amount(inv.get("amount_ttc"), currency)

        rows += f"""
        <tr>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;">{date_str}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;">{supplier_label}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;">{inv['filename']}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:right;">{ht_cell}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:right;">{ttc_cell}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:center;">{link_cell}</td>
        </tr>"""

    attachment_note = (
        "<p>Les fichiers sont joints à cet email.</p>"
        if attachments_included
        else f'<p>Les fichiers sont trop volumineux pour être joints. Accédez-y via le dossier OneDrive : <a href="{drive_folder_link}">{drive_folder_link}</a></p>'
    )

    summary_table = _build_supplier_summary(invoices)

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;color:#333;max-width:860px;margin:auto;">
  <h2 style="color:#2c5f8a;">Factures - {month_label}</h2>
  <p>Bonjour,</p>
  <p>Veuillez trouver ci-dessous le récapitulatif des factures reçues en {month_label} ({len(invoices)} facture(s)).</p>

  <table style="border-collapse:collapse;width:100%;margin:20px 0;">
    <thead>
      <tr style="background:#2c5f8a;color:white;">
        <th style="padding:8px 12px;text-align:left;">Date</th>
        <th style="padding:8px 12px;text-align:left;">Fournisseur</th>
        <th style="padding:8px 12px;text-align:left;">Fichier</th>
        <th style="padding:8px 12px;text-align:right;">HT</th>
        <th style="padding:8px 12px;text-align:right;">TTC</th>
        <th style="padding:8px 12px;text-align:center;">Drive</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>

  {summary_table}

  {attachment_note}

  <p style="margin-top:30px;">
    Dossier OneDrive du mois :
    <a href="{drive_folder_link}">{drive_folder_link}</a>
  </p>

  <hr style="border:none;border-top:1px solid #eee;margin-top:40px;">
  <p style="font-size:12px;color:#999;">
    Cet email a été généré automatiquement par le bot de gestion des factures.
  </p>
</body>
</html>
"""


def _build_message(
    to_address: str,
    subject: str,
    html_body: str,
    invoice_attachments: list[dict],  # [{name, content_bytes, content_type}]
) -> dict:
    """Build a Microsoft Graph message object (suitable for createDraft or sendMail)."""
    graph_attachments = []
    for att in invoice_attachments:
        graph_attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att["name"],
                "contentType": att.get("content_type", "application/pdf"),
                "contentBytes": base64.b64encode(att["content_bytes"]).decode(),
            }
        )

    message = {
        "subject": subject,
        "body": {
            "contentType": "HTML",
            "content": html_body,
        },
        "toRecipients": [
            {"emailAddress": {"address": to_address}}
        ],
    }

    if graph_attachments:
        message["attachments"] = graph_attachments

    return message


def create_monthly_report_draft(
    graph_client: GraphClient,
    accountant_email: str,
    invoices: list[dict],
    year: int,
    month: int,
    drive_folder_link: str,
    attachment_bytes_map: dict[int, bytes],  # invoice_id -> raw bytes (optional)
    excel_bytes: bytes | None = None,
) -> str:
    """
    Build the monthly report email and save it as a draft in Outlook.

    The draft is addressed to the accountant but must be sent manually.

    Args:
        graph_client: Authenticated Graph client.
        accountant_email: Destination email address (pre-filled in the draft To field).
        invoices: List of invoice dicts from the DB.
        year: Report year.
        month: Report month.
        drive_folder_link: Link to the OneDrive month folder.
        attachment_bytes_map: Map of invoice id -> PDF bytes for attaching.
        excel_bytes: Optional Excel summary bytes to attach to the draft.

    Returns:
        The draft message id.
    """
    if not invoices:
        logger.info("No invoices for %d/%02d — skipping draft.", year, month)
        return ""

    month_label = _month_label(year, month)
    subject = f"Factures - {month_label}"
    logger.info(
        "Building monthly report draft: period=%d/%02d invoices=%d recipient=%s",
        year, month, len(invoices), accountant_email,
    )

    # Decide whether to attach files directly
    total_size = sum(len(b) for b in attachment_bytes_map.values())
    attach_files = total_size <= MAX_ATTACHMENT_BYTES

    if attach_files:
        logger.info(
            "Attachment strategy: direct (total=%.1f KB)",
            total_size / 1024,
        )
    else:
        logger.info(
            "Attachment strategy: Drive links only (total=%.1f MB > 20 MB limit)",
            total_size / (1024 * 1024),
        )

    html_body = _build_html_body(
        invoices=invoices,
        year=year,
        month=month,
        drive_folder_link=drive_folder_link,
        attachments_included=attach_files,
    )

    # Build attachment list for the draft
    email_attachments = []
    if attach_files:
        for inv in invoices:
            inv_id = inv["id"]
            raw_bytes = attachment_bytes_map.get(inv_id)
            if raw_bytes:
                email_attachments.append(
                    {
                        "name": inv["filename"],
                        "content_type": "application/pdf",
                        "content_bytes": raw_bytes,
                    }
                )

    # Always attach the Excel summary if provided
    if excel_bytes:
        excel_filename = f"{year}-{month:02d}_factures_{month_label.replace(' ', '_')}.xlsx"
        email_attachments.append(
            {
                "name": excel_filename,
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "content_bytes": excel_bytes,
            }
        )
        logger.info("Attaching Excel summary to draft: %s", excel_filename)

    message = _build_message(
        to_address=accountant_email,
        subject=subject,
        html_body=html_body,
        invoice_attachments=email_attachments,
    )

    logger.info(
        "Creating draft: to=%s subject=%r attachments=%d",
        accountant_email, subject, len(email_attachments),
    )
    draft_id = graph_client.create_draft(message)
    logger.info(
        "Monthly report draft created: id=%s period=%s invoices=%d",
        draft_id, month_label, len(invoices),
    )
    return draft_id
