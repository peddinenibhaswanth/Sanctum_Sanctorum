"""Book catalogue operations."""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Book
from app.schemas import BookCreate, BookPage, BookSort, BookUpdate


def create_book(db: Session, data: BookCreate) -> Book:
    """Add a book to the catalogue.

    Rules: the (already normalized) ISBN must be unique -> 409 otherwise.
    """
    book = Book(**data.model_dump())
    db.add(book)
    try:
        db.commit()
    except IntegrityError:
        # The unique constraint on ``isbn`` is the source of truth, so this is also safe
        # when two requests try to add the same ISBN at the same moment.
        db.rollback()
        raise HTTPException(status_code=409, detail="A book with this ISBN already exists")
    db.refresh(book)
    return book


def get_book(db: Session, book_id: int) -> Book:
    """Return a book by id, or raise 404."""
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


def update_book(db: Session, book_id: int, data: BookUpdate) -> Book:
    """Apply a partial update. Only fields present in the request are changed; 404 if missing."""
    book = get_book(db, book_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(book, field, value)
    db.commit()
    db.refresh(book)
    return book


def list_books(
    db: Session,
    q: Optional[str] = None,
    restricted: Optional[bool] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    sort: Optional[BookSort] = None,
    limit: int = 20,
    offset: int = 0,
) -> BookPage:
    """Search the catalogue.

    Rules:
    - ``q`` matches title OR author, case-insensitive substring.
    - ``restricted`` filters exactly; ``min_price``/``max_price`` are inclusive.
    - Sorted by ``sort`` (title / price, ``-`` for descending) with ties broken by id;
      default order is id ascending.
    - ``total`` counts all matches before ``limit``/``offset`` are applied.
    """
    filters = []
    if q:
        filters.append(or_(Book.title.icontains(q, autoescape=True), Book.author.icontains(q, autoescape=True)))
    if restricted is not None:
        filters.append(Book.restricted == restricted)
    if min_price is not None:
        filters.append(Book.price_cents >= min_price)
    if max_price is not None:
        filters.append(Book.price_cents <= max_price)

    total = db.scalar(select(func.count()).select_from(Book).where(*filters))
    books = db.scalars(select(Book).where(*filters).order_by(Book.id.asc()).limit(limit).offset(offset)).all()

    return BookPage(items=books, total=total, limit=limit, offset=offset)
