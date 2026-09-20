"""Order operations: placing, paying and cancelling purchases."""
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Book, Member, MemberTier, Order, OrderItem, OrderStatus
from app.schemas import OrderCreate
from app.services.books import get_book
from app.services.members import ensure_can_access_restricted, get_member

# Percentage discount granted by each membership tier.
TIER_DISCOUNT_PERCENT: Dict[str, int] = {
    MemberTier.APPRENTICE.value: 0,
    MemberTier.ADEPT.value: 5,
    MemberTier.MASTER.value: 10,
    MemberTier.SUPREME.value: 15,
}

# Extra discount when the total quantity across all items reaches the threshold.
BULK_QUANTITY_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5


def calculate_discount_percent(member: Member, total_quantity: int) -> int:
    """Tier discount, plus the bulk discount when total quantity >= threshold."""
    percent = TIER_DISCOUNT_PERCENT[member.tier]
    if total_quantity >= BULK_QUANTITY_THRESHOLD:
        percent += BULK_DISCOUNT_PERCENT
    return percent


def create_order(db: Session, data: OrderCreate, now: datetime) -> Order:
    """Place a pending order and reserve stock.

    Checks, in order (422 for empty items / bad quantity / duplicate books is done by the schema):
    1. 404 member not found; 404 any book not found
    2. 403 any book restricted and member tier below master
    3. 409 any book has insufficient stock (all-or-nothing: nothing is changed)
    Then stock is decremented for every item and prices are snapshotted.
    Pricing: discount_cents = subtotal * percent // 100; total = subtotal - discount.
    """
    member = get_member(db, data.member_id)
    books = {item.book_id: get_book(db, item.book_id) for item in data.items}
    if any(book.restricted for book in books.values()):
        ensure_can_access_restricted(member)

    # Reserve stock with a conditional UPDATE, so the database itself refuses to go below zero.
    # Books are locked in id order so two orders touching the same books cannot deadlock.
    # If any book is short, the rollback also undoes the decrements already made (all-or-nothing).
    for item in sorted(data.items, key=lambda item: item.book_id):
        reserved = db.execute(
            update(Book)
            .where(Book.id == item.book_id, Book.stock >= item.quantity)
            .values(stock=Book.stock - item.quantity)
            .execution_options(synchronize_session=False)
        )
        if reserved.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=409, detail=f"Insufficient stock for book {item.book_id}")

    subtotal = sum(item.quantity * books[item.book_id].price_cents for item in data.items)
    total_quantity = sum(item.quantity for item in data.items)
    discount_percent = calculate_discount_percent(member, total_quantity)
    discount_cents = subtotal * discount_percent // 100

    order = Order(
        member_id=member.id,
        status=OrderStatus.PENDING.value,
        subtotal_cents=subtotal,
        discount_percent=discount_percent,
        discount_cents=discount_cents,
        total_cents=subtotal - discount_cents,
        created_at=now,
        items=[
            OrderItem(
                book_id=item.book_id,
                quantity=item.quantity,
                unit_price_cents=books[item.book_id].price_cents,
            )
            for item in data.items
        ],
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, order_id: int) -> Order:
    """Return an order by id, or raise 404."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def pay_order(db: Session, order_id: int) -> Order:
    """Mark a pending order as paid. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"Cannot pay an order that is {order.status}")
    order.status = OrderStatus.PAID.value
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: int) -> Order:
    """Cancel a pending order and restore the reserved stock. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"Cannot cancel an order that is {order.status}")
    order.status = OrderStatus.CANCELLED.value
    db.commit()
    db.refresh(order)
    return order
