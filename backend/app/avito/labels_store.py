from __future__ import annotations

import hashlib
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from app.infra.db import get_session_factory, set_tenant_context
from app.avito.labels_orm import AvitoLabelDocument, AvitoTransportLabel
from app.avito.labels_pdf import decode_pdf, identity


def save_pdf(organization_id: int, account_id: str, data: bytes) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        existing = session.scalar(select(AvitoLabelDocument).where(
            AvitoLabelDocument.organization_id == organization_id,
            AvitoLabelDocument.account_id == account_id, AvitoLabelDocument.sha256 == digest))
        if existing:
            count = len(session.scalars(select(AvitoTransportLabel.id).where(AvitoTransportLabel.document_id == existing.id)).all())
            return {"ok": True, "documentId": existing.id, "labels": count, "duplicate": True}
    labels, warnings = decode_pdf(data)
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        doc = AvitoLabelDocument(organization_id=organization_id, account_id=account_id, sha256=digest, pdf=data)
        session.add(doc)
        try:
            session.flush()
            for label in labels:
                session.add(AvitoTransportLabel(organization_id=organization_id, account_id=account_id, document_id=doc.id,
                    order_number=label.order_number, page=label.page, barcode=label.barcode,
                    barcode_type=label.barcode_type, barcode_png=label.png))
            session.commit()
        except IntegrityError:
            session.rollback()
            # Concurrent identical upload: return the already committed original.
            set_tenant_context(session, organization_id)
            existing = session.scalar(select(AvitoLabelDocument.id).where(
                AvitoLabelDocument.organization_id == organization_id,
                AvitoLabelDocument.account_id == account_id, AvitoLabelDocument.sha256 == digest))
            if not existing:
                raise
            return {"ok": True, "documentId": existing, "labels": len(labels), "duplicate": True}
    return {"ok": True, "documentId": doc.id, "labels": len(labels), "warnings": warnings, "duplicate": False}


def enrich_labels(rows, organization_id: int) -> None:
    if not rows:
        return
    index = {}
    for row in rows:
        for number in {identity(row.marketplaceId), identity(row.jobNumber), identity(row.orderId)} - {""}:
            index.setdefault((row.accountId or "", number), []).append(row)
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        try:
            # Small indexed metadata query, no original PDF or image on queue reads.
            records = session.execute(select(AvitoTransportLabel.id, AvitoTransportLabel.account_id,
                AvitoTransportLabel.order_number, AvitoTransportLabel.barcode, AvitoTransportLabel.barcode_type,
                AvitoTransportLabel.document_id).where(AvitoTransportLabel.organization_id == organization_id,
                AvitoTransportLabel.order_number.in_({key[1] for key in index}))).all()
        except OperationalError as exc:
            # Test/development DBs predating migration can still display orders.
            if "no such table: avito_transport_labels" in str(exc):
                return
            raise
    matched = {}
    for record in records:
        candidates = index.get((record.account_id, record.order_number), [])
        if len(candidates) == 1:
            matched.setdefault(id(candidates[0]), []).append(record)
    for row in rows:
        records = matched.get(id(row), [])
        if not records:
            continue
        latest = max(record.document_id for record in records)
        current = [r for r in records if r.document_id == latest]
        # Preserve multi-shipment ambiguity rather than assigning one package to
        # all products. Original is retained for a human to resolve.
        if len({r.barcode for r in current}) != 1:
            row.stickerNumberState = "ambiguous"
            row.stickerNumber = None
            row.stickerLabelId = None
            row.stickerDocumentId = latest
            continue
        record = current[0]
        row.stickerNumber = record.barcode
        row.stickerNumberState = "confirmed"
        row.stickerBarcodeType = record.barcode_type
        row.stickerLabelId = record.id
        row.stickerDocumentId = record.document_id


def read_artifact(organization_id: int, artifact_id: int, *, original: bool = False) -> bytes | None:
    model = AvitoLabelDocument if original else AvitoTransportLabel
    column = model.pdf if original else model.barcode_png
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        return session.scalar(select(column).where(model.organization_id == organization_id, model.id == artifact_id))
