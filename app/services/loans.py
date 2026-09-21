"""Library loan operations: borrowing and returning books."""
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from app.models import Book, Loan, MemberTier
from app.schemas import LoanCreate, LoanOut, LoanStatus
from app.services.books import get_book
from app.services.members import ensure_can_access_restricted, get_member

# Maximum concurrent unreturned loans per tier (None = unlimited).
TIER_LOAN_LIMIT: Dict[str, Optional[int]] = {
    MemberTier.APPRENTICE.value: 1,
    MemberTier.ADEPT.value: 3,
    MemberTier.MASTER.value: 5,
    MemberTier.SUPREME.value: None,
}

LOAN_PERIOD = timedelta(days=14)
LATE_FEE_PER_DAY_CENTS = 25


def loan_status(loan: Loan, now: datetime) -> LoanStatus:
    """``returned`` if returned; else ``overdue`` if now > due_at; else ``active``."""
    if loan.returned_at is not None:
        return "returned"
    if now > loan.due_at:
        return "overdue"
    return "active"


def to_loan_out(loan: Loan, now: datetime) -> LoanOut:
    """Serialize a loan, computing its status at read time."""
    return LoanOut(
        id=loan.id,
        member_id=loan.member_id,
        book_id=loan.book_id,
        borrowed_at=loan.borrowed_at,
        due_at=loan.due_at,
        returned_at=loan.returned_at,
        late_fee_cents=loan.late_fee_cents,
        status=loan_status(loan, now),
    )


def calculate_late_fee(due_at: datetime, returned_at: datetime, price_cents: int) -> int:
    """25 cents per started day late (any partial day counts), capped at the book's price; 0 if not late."""
    if returned_at <= due_at:
        return 0
    # divmod on timedeltas stays in exact integer arithmetic (no float rounding).
    whole_days, remainder = divmod(returned_at - due_at, timedelta(days=1))
    days_late = whole_days + (1 if remainder else 0)
    return min(days_late * LATE_FEE_PER_DAY_CENTS, price_cents)


def _unreturned_loans(member_id: int) -> Select:
    """Query for a member's loans that have not been returned yet (overdue ones included)."""
    return select(Loan).where(Loan.member_id == member_id, Loan.returned_at.is_(None))


def _get_loan(db: Session, loan_id: int) -> Loan:
    loan = db.get(Loan, loan_id)
    if loan is None:
        raise HTTPException(status_code=404, detail="Loan not found")
    return loan


def create_loan(db: Session, data: LoanCreate, now: datetime) -> LoanOut:
    """Borrow a book for 14 days.

    Checks, in order:
    1. 404 member not found; 404 book not found
    2. 403 book restricted and member tier below master
    3. 409 member has any overdue loan
    4. 409 member already has an unreturned loan of this book
    5. 409 member is at their tier's loan limit
    6. 409 book is out of stock
    On success: borrowed_at = now, due_at = now + 14 days, returned_at None,
    late_fee_cents 0, and stock is decremented by one.
    """
    member = get_member(db, data.member_id)
    book = get_book(db, data.book_id)
    if book.restricted:
        ensure_can_access_restricted(member)

    unreturned = _unreturned_loans(member.id)
    if db.scalars(unreturned.where(Loan.due_at < now)).first() is not None:
        raise HTTPException(status_code=409, detail="Member has an overdue loan")
    if db.scalars(unreturned.where(Loan.book_id == book.id)).first() is not None:
        raise HTTPException(status_code=409, detail="Member already has this book on loan")
    limit = TIER_LOAN_LIMIT[member.tier]
    if limit is not None and db.scalar(select(func.count()).select_from(unreturned.subquery())) >= limit:
        raise HTTPException(status_code=409, detail=f"Loan limit of {limit} reached for tier '{member.tier}'")

    # Conditional UPDATE: the database refuses to take the last copy twice.
    taken = db.execute(
        update(Book)
        .where(Book.id == book.id, Book.stock >= 1)
        .values(stock=Book.stock - 1)
        .execution_options(synchronize_session=False)
    )
    if taken.rowcount == 0:
        db.rollback()
        raise HTTPException(status_code=409, detail="Book is out of stock")

    loan = Loan(member_id=member.id, book_id=book.id, borrowed_at=now, due_at=now + LOAN_PERIOD)
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return to_loan_out(loan, now)


def get_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a loan by id, or raise 404."""
    return to_loan_out(_get_loan(db, loan_id), now)


def return_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a borrowed book.

    Rules: 404 if missing; 409 if already returned. Sets returned_at = now, restores one copy
    of stock and charges a late fee (see ``calculate_late_fee``).
    """
    loan = _get_loan(db, loan_id)
    if loan.returned_at is not None:
        raise HTTPException(status_code=409, detail="Loan has already been returned")
    loan.late_fee_cents = calculate_late_fee(loan.due_at, now, loan.book.price_cents)
    loan.returned_at = now
    db.execute(
        update(Book)
        .where(Book.id == loan.book_id)
        .values(stock=Book.stock + 1)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(loan)
    return to_loan_out(loan, now)


def list_member_loans(
    db: Session, member_id: int, now: datetime, status: Optional[LoanStatus] = None
) -> List[LoanOut]:
    """A member's loans ordered by id, optionally filtered by computed status; 404 if member missing."""
    member = get_member(db, member_id)
    loans = [to_loan_out(loan, now) for loan in member.loans]
    if status is not None:
        loans = [loan for loan in loans if loan.status == status]
    return loans
