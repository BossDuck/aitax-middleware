"""
app/aitax_models.py

Modelos SQLAlchemy que mapean las tablas existentes de la DB de AITAX.
Alembic NO gestiona estas tablas (usan AitaxBase, separada de Base).
Las tablas ya existen en la DB de AITAX; Django las creó con sus migraciones.

Nomenclatura Django → nombre de tabla PostgreSQL:
  apps/companies  → companies_*
  apps/sat        → sat_*
"""

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime,
    ForeignKey, Integer, Numeric, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


class AitaxBase(DeclarativeBase):
    """
    Base separada de la principal (database.Base).
    Alembic importa solo database.Base, por lo que nunca intentará
    crear ni borrar estas tablas.
    """
    pass


# ─────────────────────────────────────────────────────────────────
# companies_company
# ─────────────────────────────────────────────────────────────────

class Company(AitaxBase):
    __tablename__ = "companies_company"

    id              = Column(BigInteger, primary_key=True)
    rfc             = Column(String(13), unique=True, nullable=False)
    business_name   = Column(String(255), nullable=False)
    is_active       = Column(Boolean, default=True)
    owner_id        = Column(BigInteger, nullable=True)
    syntage_entity_id     = Column(String(80), nullable=True)
    syntage_credential_id = Column(String(80), nullable=True)
    created_at      = Column(DateTime(timezone=True), nullable=True)
    updated_at      = Column(DateTime(timezone=True), nullable=True)


# ─────────────────────────────────────────────────────────────────
# sat_invoicecache
# ─────────────────────────────────────────────────────────────────

class InvoiceCache(AitaxBase):
    __tablename__ = "sat_invoicecache"

    id              = Column(BigInteger, primary_key=True)
    company_id      = Column(BigInteger, ForeignKey("companies_company.id"), nullable=False)
    syntage_id      = Column(String, unique=True, nullable=False)
    uuid            = Column(String, nullable=True)
    iri             = Column(String, nullable=True)
    pac             = Column(String, nullable=True)
    internal_identifier = Column(String, nullable=True)

    type                        = Column(String, nullable=True)
    status                      = Column(String, nullable=True)
    cancellation_status         = Column(String, nullable=True)
    cancellation_process_status = Column(String, nullable=True)
    version                     = Column(Integer, nullable=True)

    issued_at           = Column(DateTime(timezone=True), nullable=True)
    certified_at        = Column(DateTime(timezone=True), nullable=True)
    canceled_at         = Column(DateTime(timezone=True), nullable=True)
    fully_paid_at       = Column(DateTime(timezone=True), nullable=True)
    last_payment_date   = Column(DateTime(timezone=True), nullable=True)
    due_date            = Column(Date, nullable=True)
    syntage_created_at  = Column(DateTime(timezone=True), nullable=True)
    syntage_updated_at  = Column(DateTime(timezone=True), nullable=True)

    issuer_rfc              = Column(String, nullable=True)
    issuer_name             = Column(String, nullable=True)
    issuer_tax_regime       = Column(Integer, nullable=True)
    issuer_blacklist_status = Column(String, nullable=True)

    receiver_rfc              = Column(String, nullable=True)
    receiver_name             = Column(String, nullable=True)
    receiver_tax_regime       = Column(Integer, nullable=True)
    receiver_blacklist_status = Column(String, nullable=True)

    is_issuer   = Column(Boolean, default=False)
    is_receiver = Column(Boolean, default=False)

    subtotal                = Column(Numeric, nullable=True)
    discount                = Column(Numeric, nullable=True)
    tax                     = Column(Numeric, nullable=True)
    total                   = Column(Numeric, nullable=True)
    applied_taxes           = Column(Numeric, nullable=True)
    paid_amount             = Column(Numeric, nullable=True)
    due_amount              = Column(Numeric, nullable=True)
    credited_amount         = Column(Numeric, nullable=True)
    subtotal_credited_amount = Column(Numeric, nullable=True)

    retained_total              = Column(Numeric, nullable=True)
    retained_sin_tax            = Column(Numeric, nullable=True)
    retained_income_tax         = Column(Numeric, nullable=True)
    retained_local_taxes        = Column(Numeric, nullable=True)
    retained_value_added_tax    = Column(Numeric, nullable=True)

    transferred_total           = Column(Numeric, nullable=True)
    transferred_sin_tax         = Column(Numeric, nullable=True)
    transferred_local_taxes     = Column(Numeric, nullable=True)
    transferred_value_added_tax = Column(Numeric, nullable=True)

    currency            = Column(String, nullable=True)
    exchange_rate       = Column(Numeric, nullable=True)
    is_currency_exchange = Column(Boolean, default=False)
    usage               = Column(String, nullable=True)
    payment_type        = Column(String, nullable=True)
    payment_method      = Column(String, nullable=True)
    payment_terms       = Column(String, nullable=True)
    payment_terms_raw   = Column(String, nullable=True)
    place_of_issue      = Column(String, nullable=True)
    reference           = Column(String, nullable=True)
    has_pdf             = Column(Boolean, default=False)
    has_xml             = Column(Boolean, default=False)
    # Django auto_now_add — NOT NULL, debemos proveerlo en cada INSERT
    created_at          = Column(DateTime(timezone=True), nullable=False)


class InvoiceTag(AitaxBase):
    __tablename__ = "sat_invoicetag"

    id         = Column(BigInteger, primary_key=True)
    invoice_id = Column(BigInteger, ForeignKey("sat_invoicecache.id"), nullable=False)
    value      = Column(String(120), nullable=False)


class InvoiceRelation(AitaxBase):
    __tablename__ = "sat_invoicerelation"

    id         = Column(BigInteger, primary_key=True)
    invoice_id = Column(BigInteger, ForeignKey("sat_invoicecache.id"), nullable=False)
    raw_value  = Column(JSONB, nullable=False)


# ─────────────────────────────────────────────────────────────────
# sat_conceptcache
# ─────────────────────────────────────────────────────────────────

class ConceptCache(AitaxBase):
    __tablename__ = "sat_conceptcache"

    id                  = Column(BigInteger, primary_key=True)
    company_id          = Column(BigInteger, ForeignKey("companies_company.id"), nullable=False)
    syntage_id          = Column(String, nullable=False)
    iri                 = Column(String, nullable=True)
    invoice_id          = Column(BigInteger, ForeignKey("sat_invoicecache.id"), nullable=True)
    invoice_syntage_id  = Column(String, nullable=True, default="")

    issued_at           = Column(DateTime(timezone=True), nullable=True)
    syntage_created_at  = Column(DateTime(timezone=True), nullable=True)
    syntage_updated_at  = Column(DateTime(timezone=True), nullable=True)

    description             = Column(Text, nullable=True)
    quantity                = Column(Numeric, nullable=True)
    unit_code               = Column(String, nullable=True)
    unit_amount             = Column(Numeric, nullable=True)
    total_amount            = Column(Numeric, nullable=True)
    discount_amount         = Column(Numeric, nullable=True)
    identification_number   = Column(String, nullable=True)
    product_identification  = Column(String, nullable=True)

    retained_sin_tax            = Column(Numeric, nullable=True)
    retained_income_tax         = Column(Numeric, nullable=True)
    retained_value_added_tax    = Column(Numeric, nullable=True)
    transferred_sin_tax         = Column(Numeric, nullable=True)
    transferred_value_added_tax = Column(Numeric, nullable=True)
    # Django auto_now_add — NOT NULL, debemos proveerlo en cada INSERT
    created_at                  = Column(DateTime(timezone=True), nullable=False)


class ConceptTax(AitaxBase):
    __tablename__ = "sat_concepttax"

    id          = Column(BigInteger, primary_key=True)
    concept_id  = Column(BigInteger, ForeignKey("sat_conceptcache.id"), nullable=False)
    tax         = Column(String, nullable=True)
    type        = Column(String, nullable=True)
    amount      = Column(Numeric, nullable=True)
    factor_type = Column(String, nullable=True)
    factor_amount = Column(Numeric, nullable=True)


# ─────────────────────────────────────────────────────────────────
# sat_batchpaymentcache
# ─────────────────────────────────────────────────────────────────

class BatchPaymentCache(AitaxBase):
    __tablename__ = "sat_batchpaymentcache"

    id                  = Column(BigInteger, primary_key=True)
    company_id          = Column(BigInteger, ForeignKey("companies_company.id"), nullable=False)
    syntage_id          = Column(String, nullable=False)
    iri                 = Column(String, nullable=True)
    date                = Column(DateTime(timezone=True), nullable=True)
    index               = Column(Integer, nullable=True)
    amount              = Column(Numeric, nullable=True)
    currency            = Column(String, nullable=True)
    exchange_rate       = Column(Numeric, nullable=True)
    payment_method      = Column(String, nullable=True)
    operation_number    = Column(String, nullable=True)
    invoice_syntage_id  = Column(String, nullable=True)
    invoice_id          = Column(BigInteger, ForeignKey("sat_invoicecache.id"), nullable=True)
    syntage_created_at  = Column(DateTime(timezone=True), nullable=True)
    syntage_updated_at  = Column(DateTime(timezone=True), nullable=True)
    canceled_at         = Column(DateTime(timezone=True), nullable=True)
    # Django auto_now_add — NOT NULL, debemos proveerlo en cada INSERT
    created_at          = Column(DateTime(timezone=True), nullable=False)


class BatchPaymentBank(AitaxBase):
    __tablename__ = "sat_batchpaymentbank"

    id               = Column(BigInteger, primary_key=True)
    batch_payment_id = Column(BigInteger, ForeignKey("sat_batchpaymentcache.id"), nullable=False)
    role             = Column(String, nullable=False)
    raw_value        = Column(JSONB, nullable=False)


# ─────────────────────────────────────────────────────────────────
# sat_paymentcache
# ─────────────────────────────────────────────────────────────────

class PaymentCache(AitaxBase):
    __tablename__ = "sat_paymentcache"

    id                      = Column(BigInteger, primary_key=True)
    company_id              = Column(BigInteger, ForeignKey("companies_company.id"), nullable=False)
    syntage_id              = Column(String, nullable=False)
    iri                     = Column(String, nullable=True)
    batch_payment_id        = Column(BigInteger, ForeignKey("sat_batchpaymentcache.id"), nullable=True)
    batch_payment_syntage_id = Column(String, nullable=True)
    invoice_id              = Column(BigInteger, ForeignKey("sat_invoicecache.id"), nullable=True)
    invoice_syntage_id      = Column(String, nullable=True)
    invoice_uuid            = Column(String, nullable=True)

    issued_at           = Column(DateTime(timezone=True), nullable=True)
    installment         = Column(Integer, nullable=True)
    amount              = Column(Numeric, nullable=True)
    currency            = Column(String, nullable=True)
    exchange_rate       = Column(Numeric, nullable=True)
    payment_method      = Column(String, nullable=True)
    previous_balance    = Column(Numeric, nullable=True)
    outstanding_balance = Column(Numeric, nullable=True)
    canceled_at         = Column(DateTime(timezone=True), nullable=True)
    syntage_created_at  = Column(DateTime(timezone=True), nullable=True)
    syntage_updated_at  = Column(DateTime(timezone=True), nullable=True)
    # Django auto_now_add — NOT NULL, debemos proveerlo en cada INSERT
    created_at          = Column(DateTime(timezone=True), nullable=False)
