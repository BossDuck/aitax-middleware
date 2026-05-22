"""
app/sync/sync.py

Capa de sincronización Syntage → PostgreSQL (DB de AITAX).
Port de apps/sat/sync.py de AITAX, adaptado para SQLAlchemy (sin Django ORM).

Cada función sync_* abre su propia sesión de DB para que puedan correr
en paralelo con ThreadPoolExecutor sin compartir estado de sesión.
"""

import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone as dt_timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import case, update, select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.aitax_models import (
    Company,
    InvoiceCache, InvoiceTag, InvoiceRelation,
    ConceptCache, ConceptTax,
    BatchPaymentCache, BatchPaymentBank,
    PaymentCache,
)
from app.syntage.data_client import iter_invoices, iter_concepts, iter_payments


logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Helpers de parseo (sin Django)
# ════════════════════════════════════════════════════════════════

def _parse_aware_dt(dt_str):
    if not dt_str:
        return None
    from datetime import datetime
    import dateutil.parser
    try:
        dt = dateutil.parser.parse(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(dt_timezone.utc)
    except Exception:
        return None


def _parse_date(dt_str):
    if not dt_str:
        return None
    from datetime import date
    import dateutil.parser
    try:
        return dateutil.parser.parse(dt_str).date()
    except Exception:
        return None


def _trunc(val, max_len: int):
    """Trunca strings para respetar los VARCHAR(N) de la DB de AITAX."""
    if val is None:
        return None
    s = str(val)
    return s[:max_len] if len(s) > max_len else s


def _to_decimal(val):
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_int(val):
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _extract_uuid_from_iri(iri):
    if not iri or not isinstance(iri, str):
        return None
    return iri.rstrip("/").split("/")[-1]


def _safe_dict(val):
    return val if isinstance(val, dict) else {}


def _safe_list(val):
    return val if isinstance(val, list) else []


# ════════════════════════════════════════════════════════════════
# Helper bulk FK update (1 query con CASE/WHEN)
# ════════════════════════════════════════════════════════════════

def _bulk_update_fk(db: Session, model, pk_to_fk_map: dict, fk_field: str) -> int:
    """
    Actualiza un FK en bulk con 1 sola query CASE/WHEN en lugar de N UPDATEs.
    Crítico para redes con latencia: pasa de ~50s a ~1s por chunk de 500 filas.
    """
    if not pk_to_fk_map:
        return 0

    pks = list(pk_to_fk_map.keys())
    case_expr = case(
        *[(model.id == pk, fk_val) for pk, fk_val in pk_to_fk_map.items()],
    )
    db.execute(
        update(model)
        .where(model.id.in_(pks))
        .values(**{fk_field: case_expr})
    )
    return len(pk_to_fk_map)


# ════════════════════════════════════════════════════════════════
# Mapeo JSON → dicts para upsert
# ════════════════════════════════════════════════════════════════

def _map_invoice(company_id: int, raw: dict) -> dict:
    issuer      = _safe_dict(raw.get("issuer"))
    receiver    = _safe_dict(raw.get("receiver"))
    retained    = _safe_dict(raw.get("retainedTaxes"))
    transferred = _safe_dict(raw.get("transferredTaxes"))

    return {
        "company_id":               company_id,
        "syntage_id":               _trunc(raw.get("id"), 64),
        "uuid":                     _trunc(raw.get("uuid"), 64),
        "iri":                      _trunc(raw.get("@id"), 200),
        "pac":                      _trunc(raw.get("pac"), 20),
        "internal_identifier":      _trunc(raw.get("internalIdentifier"), 80),
        "type":                     _trunc(raw.get("type"), 4),
        "status":                   _trunc(raw.get("status"), 40),
        "cancellation_status":      _trunc(raw.get("cancellationStatus"), 80),
        "cancellation_process_status": _trunc(raw.get("cancellationProcessStatus"), 80),
        "version":                  _to_int(raw.get("version")),
        "issued_at":                _parse_aware_dt(raw.get("issuedAt")),
        "certified_at":             _parse_aware_dt(raw.get("certifiedAt")),
        "canceled_at":              _parse_aware_dt(raw.get("canceledAt")),
        "fully_paid_at":            _parse_aware_dt(raw.get("fullyPaidAt")),
        "last_payment_date":        _parse_aware_dt(raw.get("lastPaymentDate")),
        "due_date":                 _parse_date(raw.get("dueDate")),
        "syntage_created_at":       _parse_aware_dt(raw.get("createdAt")),
        "syntage_updated_at":       _parse_aware_dt(raw.get("updatedAt")),
        "issuer_rfc":               _trunc(issuer.get("rfc"), 20),
        "issuer_name":              _trunc(issuer.get("name"), 255),
        "issuer_tax_regime":        _to_int(issuer.get("taxRegime")),
        "issuer_blacklist_status":  _trunc(issuer.get("blacklistStatus"), 40),
        "receiver_rfc":             _trunc(receiver.get("rfc"), 20),
        "receiver_name":            _trunc(receiver.get("name"), 255),
        "receiver_tax_regime":      _to_int(receiver.get("taxRegime")),
        "receiver_blacklist_status": _trunc(receiver.get("blacklistStatus"), 40),
        "is_issuer":                bool(raw.get("isIssuer")),
        "is_receiver":              bool(raw.get("isReceiver")),
        "subtotal":                 _to_decimal(raw.get("subtotal")),
        "discount":                 _to_decimal(raw.get("discount")),
        "tax":                      _to_decimal(raw.get("tax")),
        "total":                    _to_decimal(raw.get("total")),
        "applied_taxes":            _to_decimal(raw.get("appliedTaxes")),
        "paid_amount":              _to_decimal(raw.get("paidAmount")),
        "due_amount":               _to_decimal(raw.get("dueAmount")),
        "credited_amount":          _to_decimal(raw.get("creditedAmount")),
        "subtotal_credited_amount": _to_decimal(raw.get("subtotalCreditedAmount")),
        "retained_total":           _to_decimal(retained.get("total")),
        "retained_sin_tax":         _to_decimal(retained.get("sinTax")),
        "retained_income_tax":      _to_decimal(retained.get("incomeTax")),
        "retained_local_taxes":     _to_decimal(retained.get("localTaxes")),
        "retained_value_added_tax": _to_decimal(retained.get("valueAddedTax")),
        "transferred_total":        _to_decimal(transferred.get("total")),
        "transferred_sin_tax":      _to_decimal(transferred.get("sinTax")),
        "transferred_local_taxes":  _to_decimal(transferred.get("localTaxes")),
        "transferred_value_added_tax": _to_decimal(transferred.get("valueAddedTax")),
        "currency":                 _trunc(raw.get("currency"), 8),
        "exchange_rate":            _to_decimal(raw.get("exchangeRate")),
        "is_currency_exchange":     bool(raw.get("isCurrencyExchange")),
        "usage":                    _trunc(raw.get("usage"), 20),
        "payment_type":             _trunc(raw.get("paymentType"), 10),
        "payment_method":           _trunc(raw.get("paymentMethod"), 10),
        "payment_terms":            _trunc(raw.get("paymentTerms"), 80),
        "payment_terms_raw":        raw.get("paymentTermsRaw"),
        "place_of_issue":           _trunc(raw.get("placeOfIssue"), 20),
        "reference":                _trunc(raw.get("reference"), 80),
        "has_pdf":                  bool(raw.get("pdf")),
        "has_xml":                  bool(raw.get("xml")),
        "created_at":               datetime.now(dt_timezone.utc),
    }


def _map_concept(company_id: int, raw: dict) -> dict | None:
    invoice_obj = _safe_dict(raw.get("invoice"))
    retained    = _safe_dict(raw.get("retainedTaxes"))
    transferred = _safe_dict(raw.get("transferredTaxes"))

    issued_at = _parse_aware_dt(invoice_obj.get("issuedAt"))
    if not issued_at:
        return None

    return {
        "company_id":           company_id,
        "syntage_id":           raw.get("id"),
        "iri":                  raw.get("@id"),
        "invoice_syntage_id":   invoice_obj.get("id") or "",
        "issued_at":            issued_at,
        "syntage_created_at":   _parse_aware_dt(raw.get("createdAt")),
        "syntage_updated_at":   _parse_aware_dt(raw.get("updatedAt")),
        "description":          raw.get("description"),
        "quantity":             _to_decimal(raw.get("quantity")),
        "unit_code":            raw.get("unitCode"),
        "unit_amount":          _to_decimal(raw.get("unitAmount")),
        "total_amount":         _to_decimal(raw.get("totalAmount")),
        "discount_amount":      _to_decimal(raw.get("discountAmount")),
        "identification_number":    raw.get("identificationNumber"),
        "product_identification":   raw.get("productIdentification"),
        "retained_sin_tax":         _to_decimal(retained.get("sinTax")),
        "retained_income_tax":      _to_decimal(retained.get("incomeTax")),
        "retained_value_added_tax": _to_decimal(retained.get("valueAddedTax")),
        "transferred_sin_tax":      _to_decimal(transferred.get("sinTax")),
        "transferred_value_added_tax": _to_decimal(transferred.get("valueAddedTax")),
        "created_at":               datetime.now(dt_timezone.utc),
    }


def _map_payment(company_id: int, raw: dict) -> dict | None:
    batch       = _safe_dict(raw.get("batchPayment"))
    invoice_iri = raw.get("invoice")

    issued_at = _parse_aware_dt(raw.get("date")) or _parse_aware_dt(raw.get("createdAt"))
    if not issued_at:
        return None

    return {
        "company_id":               company_id,
        "syntage_id":               raw.get("id"),
        "iri":                      raw.get("@id"),
        "batch_payment_syntage_id": batch.get("id"),
        "invoice_syntage_id":       _extract_uuid_from_iri(invoice_iri) if isinstance(invoice_iri, str) else None,
        "invoice_uuid":             raw.get("invoiceUuid"),
        "issued_at":                issued_at,
        "installment":              _to_int(raw.get("installment")),
        "amount":                   _to_decimal(raw.get("amount")),
        "currency":                 raw.get("currency"),
        "exchange_rate":            _to_decimal(raw.get("exchangeRate")),
        "payment_method":           raw.get("paymentMethod"),
        "previous_balance":         _to_decimal(raw.get("previousBalance")),
        "outstanding_balance":      _to_decimal(raw.get("outstandingBalance")),
        "canceled_at":              _parse_aware_dt(raw.get("canceledAt")),
        "syntage_created_at":       _parse_aware_dt(raw.get("createdAt")),
        "syntage_updated_at":       _parse_aware_dt(raw.get("updatedAt")),
        "created_at":               datetime.now(dt_timezone.utc),
    }


def _map_batch_payment(company_id: int, raw_batch: dict) -> dict | None:
    if not raw_batch:
        return None
    invoice_obj = _safe_dict(raw_batch.get("invoice"))
    return {
        "company_id":           company_id,
        "syntage_id":           raw_batch.get("id"),
        "iri":                  raw_batch.get("@id"),
        "date":                 _parse_aware_dt(raw_batch.get("date")),
        "index":                _to_int(raw_batch.get("index")),
        "amount":               _to_decimal(raw_batch.get("amount")),
        "currency":             raw_batch.get("currency"),
        "exchange_rate":        _to_decimal(raw_batch.get("exchangeRate")),
        "payment_method":       raw_batch.get("paymentMethod"),
        "operation_number":     raw_batch.get("operationNumber"),
        "invoice_syntage_id":   invoice_obj.get("id"),
        "syntage_created_at":   _parse_aware_dt(raw_batch.get("createdAt")),
        "syntage_updated_at":   _parse_aware_dt(raw_batch.get("updatedAt")),
        "canceled_at":          _parse_aware_dt(raw_batch.get("canceledAt")),
        "created_at":           datetime.now(dt_timezone.utc),
    }


# ════════════════════════════════════════════════════════════════
# SYNC INVOICES
# ════════════════════════════════════════════════════════════════

def _flush_invoices(db: Session, company_id: int, fields_list: list, raw_list: list) -> int:
    if not fields_list:
        return 0

    # created_at es auto_now_add en Django: se pone solo en INSERT, nunca se actualiza
    update_fields = [k for k in fields_list[0] if k not in ("company_id", "syntage_id", "created_at")]

    stmt = pg_insert(InvoiceCache).values(fields_list)
    stmt = stmt.on_conflict_do_update(
        index_elements=["syntage_id"],
        set_={k: getattr(stmt.excluded, k) for k in update_fields},
    )
    db.execute(stmt)
    db.flush()

    syntage_ids = [f["syntage_id"] for f in fields_list]
    existing = {
        row.syntage_id: row.id
        for row in db.execute(
            select(InvoiceCache.syntage_id, InvoiceCache.id)
            .where(InvoiceCache.syntage_id.in_(syntage_ids))
        ).fetchall()
    }

    invoice_pks = list(existing.values())
    db.execute(delete(InvoiceTag).where(InvoiceTag.invoice_id.in_(invoice_pks)))
    db.execute(delete(InvoiceRelation).where(InvoiceRelation.invoice_id.in_(invoice_pks)))

    tags = []
    relations = []
    for raw in raw_list:
        inv_id = existing.get(raw.get("id"))
        if not inv_id:
            continue
        for tag in _safe_list(raw.get("tags")):
            tags.append({"invoice_id": inv_id, "value": str(tag)[:120]})
        for rel in _safe_list(raw.get("relations")):
            relations.append({"invoice_id": inv_id, "raw_value": rel})

    if tags:
        db.execute(pg_insert(InvoiceTag).values(tags))
    if relations:
        db.execute(pg_insert(InvoiceRelation).values(relations))

    db.commit()
    return len(fields_list)


def sync_invoices(company: Company, filters=None, chunk_db=500, progress_callback=None) -> dict:
    db = SessionLocal()
    try:
        fields_buffer = []
        raw_buffer    = []
        processed     = 0
        upserted      = 0

        for batch in iter_invoices(company.rfc, filters=filters):
            for raw in batch:
                mapped = _map_invoice(company.id, raw)
                if not mapped["syntage_id"] or not mapped["issued_at"]:
                    continue
                fields_buffer.append(mapped)
                raw_buffer.append(raw)
                processed += 1

                if len(fields_buffer) >= chunk_db:
                    count = _flush_invoices(db, company.id, fields_buffer, raw_buffer)
                    upserted += count
                    if progress_callback:
                        try:
                            progress_callback(count)
                        except Exception as exc:
                            logger.warning("[invoices] progress_callback falló: %s", exc)
                    fields_buffer.clear()
                    raw_buffer.clear()

            if processed > 0 and processed % 5000 < len(batch):
                logger.info("[invoices] procesadas: %d | upserted: %d", processed, upserted)

        if fields_buffer:
            count = _flush_invoices(db, company.id, fields_buffer, raw_buffer)
            upserted += count
            if progress_callback:
                try:
                    progress_callback(count)
                except Exception as exc:
                    logger.warning("[invoices] progress_callback falló: %s", exc)

        logger.info("[invoices] SYNC TERMINADO -> procesadas: %d | upserted: %d", processed, upserted)
        return {"processed": processed, "upserted": upserted}
    finally:
        db.close()


def sync_invoices_by_year(
    company: Company,
    start_year: int = 2014,
    end_year: int | None = None,
    workers_per_year: int = 3,
    progress_callback=None,
) -> dict:
    if not end_year:
        end_year = datetime.now().year

    years = list(range(start_year, end_year + 1))
    results = {}

    def _sync_year(year):
        logger.info("[invoices] Iniciando sync año %d", year)
        filters = {
            "issuedAt[after]":          f"{year}-01-01T00:00:00",
            "issuedAt[strictly_before]": f"{year + 1}-01-01T00:00:00",
        }
        return sync_invoices(company, filters=filters, progress_callback=progress_callback)

    with ThreadPoolExecutor(max_workers=workers_per_year) as executor:
        futures = {executor.submit(_sync_year, y): y for y in years}
        for future in as_completed(futures):
            year = futures[future]
            try:
                results[year] = future.result()
                logger.info("[invoices] Año %d terminado -> %s", year, results[year])
            except Exception as exc:
                results[year] = {"error": str(exc)}
                logger.error("[invoices] Error año %d: %s", year, exc)

    logger.info("[invoices] SYNC POR AÑO COMPLETO")
    return results


# ════════════════════════════════════════════════════════════════
# SYNC CONCEPTS
# ════════════════════════════════════════════════════════════════

def _flush_concepts(db: Session, company_id: int, fields_list: list, raw_list: list) -> int:
    if not fields_list:
        return 0

    update_fields = [k for k in fields_list[0] if k not in ("company_id", "syntage_id", "created_at")]

    stmt = pg_insert(ConceptCache).values(fields_list)
    stmt = stmt.on_conflict_do_update(
        index_elements=["company_id", "syntage_id"],
        set_={k: getattr(stmt.excluded, k) for k in update_fields},
    )
    db.execute(stmt)
    db.flush()

    syntage_ids = [f["syntage_id"] for f in fields_list]
    existing_concepts = {
        row.syntage_id: row.id
        for row in db.execute(
            select(ConceptCache.syntage_id, ConceptCache.id)
            .where(ConceptCache.company_id == company_id)
            .where(ConceptCache.syntage_id.in_(syntage_ids))
        ).fetchall()
    }

    # Resolver FK invoice_id con 1 query masivo
    invoice_syntage_ids = {f["invoice_syntage_id"] for f in fields_list if f.get("invoice_syntage_id")}
    invoice_map = {
        row.syntage_id: row.id
        for row in db.execute(
            select(InvoiceCache.syntage_id, InvoiceCache.id)
            .where(InvoiceCache.syntage_id.in_(invoice_syntage_ids))
        ).fetchall()
    }

    pk_to_fk = {
        existing_concepts[f["syntage_id"]]: invoice_map[f["invoice_syntage_id"]]
        for f in fields_list
        if f.get("invoice_syntage_id") in invoice_map and f["syntage_id"] in existing_concepts
    }
    _bulk_update_fk(db, ConceptCache, pk_to_fk, "invoice_id")

    # Reinsertar taxes
    concept_pks = list(existing_concepts.values())
    db.execute(delete(ConceptTax).where(ConceptTax.concept_id.in_(concept_pks)))

    taxes = []
    for raw in raw_list:
        concept_id = existing_concepts.get(raw.get("id"))
        if not concept_id:
            continue
        for t in _safe_list(raw.get("taxes")):
            factor = _safe_dict(t.get("factor"))
            taxes.append({
                "concept_id":   concept_id,
                "tax":          t.get("tax"),
                "type":         t.get("type"),
                "amount":       _to_decimal(t.get("amount")),
                "factor_type":  factor.get("type"),
                "factor_amount": _to_decimal(factor.get("amount")),
            })

    if taxes:
        db.execute(pg_insert(ConceptTax).values(taxes))

    db.commit()
    return len(fields_list)


def sync_concepts(company: Company, filters=None, chunk_db=500, progress_callback=None) -> dict:
    db = SessionLocal()
    try:
        fields_buffer = []
        raw_buffer    = []
        processed     = 0
        upserted      = 0

        for batch in iter_concepts(company.rfc, filters=filters):
            for raw in batch:
                mapped = _map_concept(company.id, raw)
                if not mapped or not mapped.get("syntage_id"):
                    continue
                fields_buffer.append(mapped)
                raw_buffer.append(raw)
                processed += 1

                if len(fields_buffer) >= chunk_db:
                    count = _flush_concepts(db, company.id, fields_buffer, raw_buffer)
                    upserted += count
                    if progress_callback:
                        try:
                            progress_callback(count)
                        except Exception as exc:
                            logger.warning("[concepts] progress_callback falló: %s", exc)
                    fields_buffer.clear()
                    raw_buffer.clear()

            if processed > 0 and processed % 5000 < len(batch):
                logger.info("[concepts] procesados: %d | upserted: %d", processed, upserted)

        if fields_buffer:
            count = _flush_concepts(db, company.id, fields_buffer, raw_buffer)
            upserted += count
            if progress_callback:
                try:
                    progress_callback(count)
                except Exception as exc:
                    logger.warning("[concepts] progress_callback falló: %s", exc)

        logger.info("[concepts] SYNC TERMINADO -> procesados: %d | upserted: %d", processed, upserted)
        return {"processed": processed, "upserted": upserted}
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════
# SYNC PAYMENTS
# ════════════════════════════════════════════════════════════════

def _flush_payments(db: Session, company_id: int, fields_list: list, raw_list: list) -> int:
    if not fields_list:
        return 0

    # ── 1) Batch payments únicos del chunk ──
    batch_fields_seen: dict[str, tuple[dict, dict]] = {}
    for raw in raw_list:
        batch_raw = _safe_dict(raw.get("batchPayment"))
        if not batch_raw or not batch_raw.get("id"):
            continue
        if batch_raw["id"] not in batch_fields_seen:
            mapped_batch = _map_batch_payment(company_id, batch_raw)
            if mapped_batch:
                batch_fields_seen[batch_raw["id"]] = (mapped_batch, batch_raw)

    existing_batches: dict[str, int] = {}

    if batch_fields_seen:
        batch_field_dicts = [t[0] for t in batch_fields_seen.values()]
        batch_update_fields = [k for k in batch_field_dicts[0] if k not in ("company_id", "syntage_id", "created_at")]

        stmt = pg_insert(BatchPaymentCache).values(batch_field_dicts)
        stmt = stmt.on_conflict_do_update(
            index_elements=["company_id", "syntage_id"],
            set_={k: getattr(stmt.excluded, k) for k in batch_update_fields},
        )
        db.execute(stmt)
        db.flush()

        existing_batches = {
            row.syntage_id: row.id
            for row in db.execute(
                select(BatchPaymentCache.syntage_id, BatchPaymentCache.id)
                .where(BatchPaymentCache.company_id == company_id)
                .where(BatchPaymentCache.syntage_id.in_(list(batch_fields_seen.keys())))
            ).fetchall()
        }

        # FK invoice_id en batch payments
        batch_invoice_ids = {f.get("invoice_syntage_id") for f in batch_field_dicts if f.get("invoice_syntage_id")}
        inv_map_batches = {
            row.syntage_id: row.id
            for row in db.execute(
                select(InvoiceCache.syntage_id, InvoiceCache.id)
                .where(InvoiceCache.syntage_id.in_(batch_invoice_ids))
            ).fetchall()
        }

        batch_pk_to_inv = {
            existing_batches[f["syntage_id"]]: inv_map_batches[f["invoice_syntage_id"]]
            for f in batch_field_dicts
            if f.get("invoice_syntage_id") in inv_map_batches and f["syntage_id"] in existing_batches
        }
        _bulk_update_fk(db, BatchPaymentCache, batch_pk_to_inv, "invoice_id")

        # Reinsertar bancos
        batch_pks = list(existing_batches.values())
        db.execute(delete(BatchPaymentBank).where(BatchPaymentBank.batch_payment_id.in_(batch_pks)))

        banks = []
        for batch_id, (_, batch_raw) in batch_fields_seen.items():
            bp_pk = existing_batches.get(batch_id)
            if not bp_pk:
                continue
            for bank in _safe_list(batch_raw.get("payerBank")):
                banks.append({"batch_payment_id": bp_pk, "role": "payer", "raw_value": bank})
            for bank in _safe_list(batch_raw.get("beneficiaryBank")):
                banks.append({"batch_payment_id": bp_pk, "role": "beneficiary", "raw_value": bank})

        if banks:
            db.execute(pg_insert(BatchPaymentBank).values(banks))

    # ── 2) Payments ──
    update_fields = [k for k in fields_list[0] if k not in ("company_id", "syntage_id", "created_at")]
    stmt = pg_insert(PaymentCache).values(fields_list)
    stmt = stmt.on_conflict_do_update(
        index_elements=["company_id", "syntage_id"],
        set_={k: getattr(stmt.excluded, k) for k in update_fields},
    )
    db.execute(stmt)
    db.flush()

    payment_syntage_ids = [f["syntage_id"] for f in fields_list]
    existing_payments = {
        row.syntage_id: row.id
        for row in db.execute(
            select(PaymentCache.syntage_id, PaymentCache.id)
            .where(PaymentCache.company_id == company_id)
            .where(PaymentCache.syntage_id.in_(payment_syntage_ids))
        ).fetchall()
    }

    # Resolver batches que puedan faltar del chunk
    all_batch_ids = {f.get("batch_payment_syntage_id") for f in fields_list if f.get("batch_payment_syntage_id")}
    missing_batches = all_batch_ids - set(existing_batches.keys())
    if missing_batches:
        extra = {
            row.syntage_id: row.id
            for row in db.execute(
                select(BatchPaymentCache.syntage_id, BatchPaymentCache.id)
                .where(BatchPaymentCache.company_id == company_id)
                .where(BatchPaymentCache.syntage_id.in_(missing_batches))
            ).fetchall()
        }
        existing_batches.update(extra)

    # FK invoice_id en payments
    inv_syntage_ids = {f.get("invoice_syntage_id") for f in fields_list if f.get("invoice_syntage_id")}
    pay_inv_map = {
        row.syntage_id: row.id
        for row in db.execute(
            select(InvoiceCache.syntage_id, InvoiceCache.id)
            .where(InvoiceCache.syntage_id.in_(inv_syntage_ids))
        ).fetchall()
    }

    pay_pk_to_batch: dict[int, int] = {}
    pay_pk_to_inv:   dict[int, int] = {}
    for f in fields_list:
        pay_pk = existing_payments.get(f["syntage_id"])
        if not pay_pk:
            continue
        if f.get("batch_payment_syntage_id") in existing_batches:
            pay_pk_to_batch[pay_pk] = existing_batches[f["batch_payment_syntage_id"]]
        if f.get("invoice_syntage_id") in pay_inv_map:
            pay_pk_to_inv[pay_pk] = pay_inv_map[f["invoice_syntage_id"]]

    _bulk_update_fk(db, PaymentCache, pay_pk_to_batch, "batch_payment_id")
    _bulk_update_fk(db, PaymentCache, pay_pk_to_inv,   "invoice_id")

    db.commit()
    return len(fields_list)


def sync_payments(company: Company, params=None, chunk_db=500, progress_callback=None) -> dict:
    db = SessionLocal()
    try:
        fields_buffer = []
        raw_buffer    = []
        processed     = 0
        upserted      = 0

        for batch in iter_payments(company.rfc, params=params):
            for raw in batch:
                mapped = _map_payment(company.id, raw)
                if not mapped or not mapped.get("syntage_id"):
                    continue
                fields_buffer.append(mapped)
                raw_buffer.append(raw)
                processed += 1

                if len(fields_buffer) >= chunk_db:
                    count = _flush_payments(db, company.id, fields_buffer, raw_buffer)
                    upserted += count
                    if progress_callback:
                        try:
                            progress_callback(count)
                        except Exception as exc:
                            logger.warning("[payments] progress_callback falló: %s", exc)
                    fields_buffer.clear()
                    raw_buffer.clear()

            if processed > 0 and processed % 5000 < len(batch):
                logger.info("[payments] procesados: %d | upserted: %d", processed, upserted)

        if fields_buffer:
            count = _flush_payments(db, company.id, fields_buffer, raw_buffer)
            upserted += count
            if progress_callback:
                try:
                    progress_callback(count)
                except Exception as exc:
                    logger.warning("[payments] progress_callback falló: %s", exc)

        logger.info("[payments] SYNC TERMINADO -> procesados: %d | upserted: %d", processed, upserted)
        return {"processed": processed, "upserted": upserted}
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════
# ORQUESTADOR
# ════════════════════════════════════════════════════════════════

def sync_all(
    company: Company,
    start_year: int = 2014,
    end_year: int | None = None,
    invoices_callback=None,
    concepts_callback=None,
    payments_callback=None,
) -> dict:
    """
    Corre los 3 syncs en paralelo (facturas, conceptos, pagos).
    Al terminar llama rebind_orphan_fks para completar FKs que quedaron
    pendientes por el orden no determinista del paralelismo.
    """
    results: dict = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                sync_invoices_by_year, company, start_year, end_year,
                progress_callback=invoices_callback,
            ): "invoices",
            executor.submit(
                sync_concepts, company,
                progress_callback=concepts_callback,
            ): "concepts",
            executor.submit(
                sync_payments, company,
                progress_callback=payments_callback,
            ): "payments",
        }

        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
                logger.info("[sync_all] %s terminado", key)
            except Exception as exc:
                results[key] = {"error": str(exc)}
                logger.error("[sync_all] Error en %s: %s", key, exc)

    try:
        results["rebind"] = rebind_orphan_fks(company)
    except Exception as exc:
        results["rebind"] = {"error": str(exc)}
        logger.error("[sync_all] Error en rebind: %s", exc)

    logger.info("[sync_all] COMPLETO para RFC=%s", company.rfc)
    return results


# ════════════════════════════════════════════════════════════════
# REBIND DE FKs HUÉRFANAS
# ════════════════════════════════════════════════════════════════

def rebind_orphan_fks(company: Company) -> dict:
    """
    Completa FKs nulas que quedaron tras el sync paralelo.
    Conceptos/pagos/batch payments cuyo padre llegó después se corrigen aquí.
    """
    db = SessionLocal()
    try:
        fixed_concepts      = 0
        fixed_payments_inv  = 0
        fixed_payments_bp   = 0
        fixed_batches       = 0

        # ── Concepts sin FK a invoice ──
        orphan_concepts = db.execute(
            select(ConceptCache.id, ConceptCache.invoice_syntage_id)
            .where(ConceptCache.company_id == company.id)
            .where(ConceptCache.invoice_id.is_(None))
            .where(ConceptCache.invoice_syntage_id != "")
            .where(ConceptCache.invoice_syntage_id.is_not(None))
        ).fetchall()

        if orphan_concepts:
            ids = {row.invoice_syntage_id for row in orphan_concepts}
            inv_map = {
                row.syntage_id: row.id
                for row in db.execute(
                    select(InvoiceCache.syntage_id, InvoiceCache.id)
                    .where(InvoiceCache.syntage_id.in_(ids))
                ).fetchall()
            }
            pk_to_fk = {
                row.id: inv_map[row.invoice_syntage_id]
                for row in orphan_concepts
                if row.invoice_syntage_id in inv_map
            }
            fixed_concepts = _bulk_update_fk(db, ConceptCache, pk_to_fk, "invoice_id")

        # ── Payments sin FK a invoice ──
        orphan_payments = db.execute(
            select(PaymentCache.id, PaymentCache.invoice_syntage_id)
            .where(PaymentCache.company_id == company.id)
            .where(PaymentCache.invoice_id.is_(None))
            .where(PaymentCache.invoice_syntage_id.is_not(None))
        ).fetchall()

        if orphan_payments:
            ids = {row.invoice_syntage_id for row in orphan_payments}
            inv_map = {
                row.syntage_id: row.id
                for row in db.execute(
                    select(InvoiceCache.syntage_id, InvoiceCache.id)
                    .where(InvoiceCache.syntage_id.in_(ids))
                ).fetchall()
            }
            pk_to_fk = {
                row.id: inv_map[row.invoice_syntage_id]
                for row in orphan_payments
                if row.invoice_syntage_id in inv_map
            }
            fixed_payments_inv = _bulk_update_fk(db, PaymentCache, pk_to_fk, "invoice_id")

        # ── Payments sin FK a batch payment ──
        orphan_payments_bp = db.execute(
            select(PaymentCache.id, PaymentCache.batch_payment_syntage_id)
            .where(PaymentCache.company_id == company.id)
            .where(PaymentCache.batch_payment_id.is_(None))
            .where(PaymentCache.batch_payment_syntage_id.is_not(None))
        ).fetchall()

        if orphan_payments_bp:
            ids = {row.batch_payment_syntage_id for row in orphan_payments_bp}
            bp_map = {
                row.syntage_id: row.id
                for row in db.execute(
                    select(BatchPaymentCache.syntage_id, BatchPaymentCache.id)
                    .where(BatchPaymentCache.company_id == company.id)
                    .where(BatchPaymentCache.syntage_id.in_(ids))
                ).fetchall()
            }
            pk_to_fk = {
                row.id: bp_map[row.batch_payment_syntage_id]
                for row in orphan_payments_bp
                if row.batch_payment_syntage_id in bp_map
            }
            fixed_payments_bp = _bulk_update_fk(db, PaymentCache, pk_to_fk, "batch_payment_id")

        # ── BatchPayments sin FK a invoice ──
        orphan_batches = db.execute(
            select(BatchPaymentCache.id, BatchPaymentCache.invoice_syntage_id)
            .where(BatchPaymentCache.company_id == company.id)
            .where(BatchPaymentCache.invoice_id.is_(None))
            .where(BatchPaymentCache.invoice_syntage_id.is_not(None))
        ).fetchall()

        if orphan_batches:
            ids = {row.invoice_syntage_id for row in orphan_batches}
            inv_map = {
                row.syntage_id: row.id
                for row in db.execute(
                    select(InvoiceCache.syntage_id, InvoiceCache.id)
                    .where(InvoiceCache.syntage_id.in_(ids))
                ).fetchall()
            }
            pk_to_fk = {
                row.id: inv_map[row.invoice_syntage_id]
                for row in orphan_batches
                if row.invoice_syntage_id in inv_map
            }
            fixed_batches = _bulk_update_fk(db, BatchPaymentCache, pk_to_fk, "invoice_id")

        db.commit()

        logger.info(
            "[rebind] concepts=%d, payments(invoice)=%d, payments(batch)=%d, batchpayments(invoice)=%d",
            fixed_concepts, fixed_payments_inv, fixed_payments_bp, fixed_batches,
        )
        return {
            "concepts":           fixed_concepts,
            "payments_invoice":   fixed_payments_inv,
            "payments_batch":     fixed_payments_bp,
            "batchpayments_invoice": fixed_batches,
        }
    finally:
        db.close()
