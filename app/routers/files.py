from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from .. import config
from ..deps import Ctx, F, Forbidden, FormError, audit, get_ctx, redirect, safe_next
from ..models import Allowance, Customer, Document, Expense, Invoice, MonthlyPlan, Order, OrderCost, Payment
from ..services import access, get_or_404, owners
from ..uploads import DOC_TYPES, save_upload

router = APIRouter()

ENTITIES = {
    "customer": (Customer, "customers"),
    "order": (Order, "orders"),
    "order_cost": (OrderCost, "orders"),
    "invoice": (Invoice, "finance"),
    "payment": (Payment, "finance"),
    "expense": (Expense, "expenses"),
    "plan": (MonthlyPlan, "reports"),
    "allowance": (Allowance, "hr"),
}


def can_attach(ctx: Ctx, entity: str, entity_id: int) -> bool:
    if entity not in ENTITIES:
        return False
    model, area = ENTITIES[entity]
    obj = get_or_404(ctx.db, model, entity_id)
    if entity == "plan":
        return ctx.is_admin
    if entity == "allowance":
        return ctx.can("hr", "edit")
    if entity in ("invoice", "payment") and ctx.can("finance", "edit"):
        return True
    return access(ctx, area, owners(obj), write=True)


@router.post("/files/upload")
async def upload(ctx: Ctx = Depends(get_ctx)):
    form = await ctx.request.form()
    f = F(form)
    entity = f.choice("entity", ENTITIES.keys(), label="record type")
    entity_id = f.int("entity_id", required=True)
    doc_type = f.choice("doc_type", DOC_TYPES.keys(), "other", label="document type")
    if not entity or not can_attach(ctx, entity, entity_id):
        raise Forbidden("You cannot attach documents to this record.")
    doc = await save_upload(ctx, form.get("file"), entity, entity_id, doc_type, required=True)
    audit(ctx, "upload", entity, entity_id, f"Uploaded {DOC_TYPES[doc_type]}: {doc.original_name}")
    ctx.db.commit()
    return redirect(safe_next(f.str("back")), "Document attached.")


@router.get("/files/{doc_id}")
def download(doc_id: int, ctx: Ctx = Depends(get_ctx)):
    from ..services import doc_access
    doc = get_or_404(ctx.db, Document, doc_id)
    if not doc_access(ctx, doc):
        raise Forbidden()
    path = config.UPLOAD_DIR / doc.stored_name
    if not path.is_file():
        raise FormError("The stored file could not be found.")
    audit(ctx, "download", doc.entity, doc.entity_id, f"Opened {doc.original_name}")
    ctx.db.commit()
    inline = doc.mime in ("application/pdf", "image/jpeg", "image/png", "image/webp")
    resp = FileResponse(path, media_type=doc.mime, filename=doc.original_name,
                        content_disposition_type="inline" if inline else "attachment")
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    return resp


@router.post("/files/{doc_id}/delete")
async def delete(doc_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_admin()
    doc = get_or_404(ctx.db, Document, doc_id)
    f = F(await ctx.request.form())
    audit(ctx, "delete", doc.entity, doc.entity_id, f"Deleted document {doc.original_name} ({doc.doc_type})",
          {"sha256": [doc.sha256, None]})
    (config.UPLOAD_DIR / doc.stored_name).unlink(missing_ok=True)
    ctx.db.delete(doc)
    ctx.db.commit()
    return redirect(safe_next(f.str("back")), "Document removed.")
