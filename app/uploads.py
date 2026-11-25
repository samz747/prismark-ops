"""Secure document storage: type checked by file signature, size limited, random names,
stored outside the web root and only served through an access checked route."""
import hashlib
import uuid

from fastapi import UploadFile

from . import config
from .deps import Ctx, FormError
from .models import Document

ALLOWED = {
    ".pdf": ("application/pdf", [b"%PDF"]),
    ".jpg": ("image/jpeg", [b"\xff\xd8\xff"]),
    ".jpeg": ("image/jpeg", [b"\xff\xd8\xff"]),
    ".png": ("image/png", [b"\x89PNG\r\n\x1a\n"]),
    ".webp": ("image/webp", [b"RIFF"]),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", [b"PK\x03\x04"]),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", [b"PK\x03\x04"]),
}

DOC_TYPES = {
    "rfq": "Tender / RFQ document",
    "quotation": "Quotation",
    "lpo": "LPO",
    "supplier_invoice": "Supplier proforma / invoice",
    "shipping_docs": "Shipping & clearing documents",
    "delivery_note": "Signed delivery note",
    "tax_invoice": "Tax invoice / EFD receipt",
    "payment_proof": "Payment proof",
    "receipt": "Receipt",
    "contract": "Contract / agreement",
    "plan": "Plan document",
    "payslip": "Payslip / approval",
    "registration": "Company registration",
    "other": "Other",
}


async def save_upload(ctx: Ctx, file: UploadFile | None, entity: str, entity_id: int, doc_type: str,
                      required: bool = False) -> Document | None:
    if file is None or not getattr(file, "filename", ""):
        if required:
            raise FormError("Please attach the supporting document.")
        return None
    if doc_type not in DOC_TYPES:
        raise FormError("Unknown document type.")
    name = file.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:180]
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in ALLOWED:
        raise FormError("Only PDF, JPG, PNG, WEBP, DOCX and XLSX files are accepted.")
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise FormError(f"File is larger than {config.MAX_UPLOAD_MB} MB.")
    if not data:
        raise FormError("The file is empty.")
    mime, signatures = ALLOWED[ext]
    if not any(data.startswith(sig) for sig in signatures):
        raise FormError("The file content does not match its extension.")
    stored = f"{uuid.uuid4().hex}{ext}"
    (config.UPLOAD_DIR / stored).write_bytes(data)
    doc = Document(entity=entity, entity_id=entity_id, doc_type=doc_type, original_name=name,
                   stored_name=stored, mime=mime, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                   uploaded_by_id=ctx.user.id)
    ctx.db.add(doc)
    ctx.db.flush()
    return doc
