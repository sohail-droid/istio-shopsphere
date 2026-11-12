"""ShopSphere Order Service.

Owns the checkout. Flow:

  POST /orders/checkout
     -> verify product, reserve stock, save order as PENDING_PAYMENT
     -> ask Payment Service to create a payment (returns immediately)
     -> respond 202 to the caller

  Later (asynchronously):
     Payment Service publishes payment.succeeded / payment.failed to a Redis
     Stream -> this service consumes it -> commits or releases the stock and
     moves the order to CONFIRMED / PAYMENT_FAILED.

  A reconciler re-checks orders stuck in PENDING_PAYMENT, so a lost event or
  a payment call that timed out does not leave an order hanging.
"""
import logging
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional
from uuid import uuid4

import httpx
import redis
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("order-service")

PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:8000")
INVENTORY_SERVICE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://inventory-service:8000")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8000")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
EVENTS_STREAM = os.getenv("PAYMENT_EVENTS_STREAM", "payments.events")
CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "order-service")
CONSUMER_NAME = os.getenv("HOSTNAME", socket.gethostname())

RECONCILE_INTERVAL = float(os.getenv("RECONCILE_INTERVAL_SECONDS", "30"))
RECONCILE_AFTER = float(os.getenv("RECONCILE_AFTER_SECONDS", "60"))
RECLAIM_IDLE_MS = int(os.getenv("RECLAIM_IDLE_MS", "10000"))

EVENT_TO_STATUS = {
    "payment.succeeded": "CONFIRMED",
    "payment.failed": "PAYMENT_FAILED",
}
PAYMENT_TO_STATUS = {
    "SUCCEEDED": "CONFIRMED",
    "FAILED": "PAYMENT_FAILED",
}

orders: Dict[str, dict] = {}
orders_lock = threading.RLock()
order_locks: Dict[str, threading.Lock] = {}

# Client-supplied Idempotency-Key -> checkout it created.
idempotency: Dict[str, dict] = {}
idempotency_lock = threading.Lock()

stop_event = threading.Event()
threads: Dict[str, threading.Thread] = {}


class SettlementError(Exception):
    pass


class OrderNotFound(Exception):
    pass


class CheckoutRequest(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    # Learning simulation only: makes the fake PSP decline the payment.
    simulate_failure: bool = False


# --------------------------------------------------------------------------
# Settlement: the single place where an order leaves PENDING_PAYMENT.
# Used by the event consumer and by the reconciler. Fully idempotent.
# --------------------------------------------------------------------------
def order_lock(order_id: str) -> threading.Lock:
    with orders_lock:
        return order_locks.setdefault(order_id, threading.Lock())


def settle_order(order_id: str, target_status: str, source: str) -> dict:
    with orders_lock:
        order = orders.get(order_id)
    if order is None:
        raise OrderNotFound(order_id)

    with order_lock(order_id):
        current = order["status"]

        if current == target_status:
            return order

        if current != "PENDING_PAYMENT":
            # e.g. a late "succeeded" for an order already marked failed.
            note = f"{source}: ignored {target_status}, order is {current}"
            order.setdefault("anomalies", []).append(note)
            log.error("order %s needs manual review: %s", order_id, note)
            return order

        action = "commit" if target_status == "CONFIRMED" else "release"

        try:
            response = httpx.post(
                f"{INVENTORY_SERVICE_URL}/inventory/"
                f"{order['product_id']}/{action}",
                params={"reservation_id": order["reservation"]["reservation_id"]},
                timeout=5.0,
            )
            response.raise_for_status()
            settlement = response.json()
        except httpx.HTTPError as exc:
            raise SettlementError(
                f"inventory {action} failed, order stays PENDING_PAYMENT: {exc}"
            )

        order["status"] = target_status
        order["payment_status"] = (
            "SUCCEEDED" if target_status == "CONFIRMED" else "FAILED"
        )
        order["inventory_settlement"] = settlement
        order["settled_by"] = source
        log.info("order %s -> %s (via %s)", order_id, target_status, source)
        return order


# --------------------------------------------------------------------------
# Payment Service calls
# --------------------------------------------------------------------------
def request_payment(order: dict) -> dict:
    """Create the payment. Key is derived from the order, so retrying (even
    from the reconciler) can never create a second payment."""
    response = httpx.post(
        f"{PAYMENT_SERVICE_URL}/payments",
        json={
            "order_id": order["order_id"],
            "amount": order["amount"],
            "simulate_failure": order["_simulate_failure"],
        },
        headers={"Idempotency-Key": f"pay-{order['order_id']}"},
        timeout=5.0,
    )
    response.raise_for_status()
    return response.json()


def create_order(request: CheckoutRequest) -> dict:
    # 1. Verify product and price (server-side, never trust the client).
    try:
        response = httpx.get(
            f"{PRODUCT_SERVICE_URL}/products/{request.product_id}", timeout=5.0
        )
        response.raise_for_status()
        product = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, "Product does not exist")
        raise HTTPException(503, "Product Service returned an error")
    except httpx.HTTPError:
        raise HTTPException(503, "Product Service is unavailable")

    order_id = str(uuid4())
    reservation_id = str(uuid4())

    # 2. Reserve stock.
    try:
        response = httpx.post(
            f"{INVENTORY_SERVICE_URL}/inventory/{request.product_id}/reserve",
            params={"quantity": request.quantity, "reservation_id": reservation_id},
            timeout=5.0,
        )
        response.raise_for_status()
        reservation = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 409):
            raise HTTPException(exc.response.status_code, exc.response.text)
        raise HTTPException(502, f"Inventory Service error: {exc.response.text}")
    except httpx.HTTPError:
        raise HTTPException(503, "Inventory Service is unavailable")

    # 3. Save the order before asking for payment.
    order = {
        "order_id": order_id,
        "product_id": request.product_id,
        "product": product,
        "quantity": request.quantity,
        "amount": round(float(product["price"]) * request.quantity, 2),
        "currency": "INR",
        "reservation": reservation,
        "status": "PENDING_PAYMENT",
        "payment_status": "NOT_REQUESTED",
        "created_at": int(time.time()),
        "_simulate_failure": request.simulate_failure,
    }
    with orders_lock:
        orders[order_id] = order

    # 4. Ask Payment Service to create the payment (does NOT wait for the PSP).
    try:
        payment = request_payment(order)
        order["payment"] = payment
        order["payment_status"] = payment["status"]
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            order["payment_status"] = "REJECTED"
            try:
                settle_order(order_id, "PAYMENT_FAILED", "payment-rejected")
            except SettlementError as err:
                log.warning("will retry via reconciler: %s", err)
        else:
            order["payment_status"] = "UNKNOWN"
    except httpx.HTTPError:
        # Outcome unknown (timeout etc.). Do NOT release stock; reconciler
        # will ask Payment Service what really happened.
        order["payment_status"] = "UNKNOWN"

    return order


def public_order(order: dict) -> dict:
    return {k: v for k, v in order.items() if not k.startswith("_")}


# --------------------------------------------------------------------------
# Background workers
# --------------------------------------------------------------------------
def handle_event(r: redis.Redis, entry_id: str, fields: dict) -> None:
    target = EVENT_TO_STATUS.get(fields.get("type", ""))

    if target is None:
        log.warning("ignoring unknown event %s", fields)
        r.xack(EVENTS_STREAM, CONSUMER_GROUP, entry_id)
        return

    try:
        settle_order(fields["order_id"], target, "event")
    except OrderNotFound:
        # Not retryable (e.g. order lost on restart). Ack, keep the log line.
        log.error("event for unknown order %s: %s", fields.get("order_id"), fields)
    except SettlementError as exc:
        # No ack: stays pending and is reclaimed after RECLAIM_IDLE_MS.
        log.warning("will retry event %s: %s", entry_id, exc)
        return

    r.xack(EVENTS_STREAM, CONSUMER_GROUP, entry_id)


def consume_events() -> None:
    while not stop_event.is_set():
        try:
            r = redis.Redis.from_url(
                REDIS_URL, decode_responses=True, socket_connect_timeout=3
            )
            try:
                r.xgroup_create(EVENTS_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            log.info("event consumer started as %s", CONSUMER_NAME)

            while not stop_event.is_set():
                # Retry anything delivered earlier but never acknowledged.
                claimed = r.xautoclaim(
                    EVENTS_STREAM, CONSUMER_GROUP, CONSUMER_NAME,
                    min_idle_time=RECLAIM_IDLE_MS, start_id="0-0", count=10,
                )[1]
                for entry_id, fields in claimed:
                    handle_event(r, entry_id, fields)

                batches = r.xreadgroup(
                    CONSUMER_GROUP, CONSUMER_NAME, {EVENTS_STREAM: ">"},
                    count=10, block=2000,
                )
                for _stream, entries in batches or []:
                    for entry_id, fields in entries:
                        handle_event(r, entry_id, fields)

        except redis.RedisError as exc:
            log.error("event bus problem, reconnecting in 3s: %s", exc)
            stop_event.wait(3)


def reconcile_order(order_id: str) -> None:
    with orders_lock:
        order = orders.get(order_id)
    if order is None or order["status"] != "PENDING_PAYMENT":
        return

    try:
        response = httpx.get(
            f"{PAYMENT_SERVICE_URL}/payments/by-order/{order_id}", timeout=5.0
        )

        if response.status_code == 404:
            # Payment was never created. Safe to retry (same idempotency key).
            payment = request_payment(order)
            order["payment"] = payment
            order["payment_status"] = payment["status"]
            log.info("reconciler created missing payment for %s", order_id)
            return

        response.raise_for_status()
        payment = response.json()
        order["payment"] = payment
        target = PAYMENT_TO_STATUS.get(payment["status"])
        if target:
            settle_order(order_id, target, "reconciler")

    except (httpx.HTTPError, SettlementError) as exc:
        log.warning("reconcile %s failed, will retry: %s", order_id, exc)


def reconcile_loop() -> None:
    while not stop_event.wait(RECONCILE_INTERVAL):
        cutoff = time.time() - RECONCILE_AFTER
        with orders_lock:
            stale = [
                o["order_id"] for o in orders.values()
                if o["status"] == "PENDING_PAYMENT" and o["created_at"] < cutoff
            ]
        for order_id in stale:
            reconcile_order(order_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop_event.clear()
    for name, target in (("consumer", consume_events), ("reconciler", reconcile_loop)):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        threads[name] = thread
    yield
    stop_event.set()


app = FastAPI(
    title="ShopSphere Order Service",
    description="Owns checkout; settles orders from payment events",
    version="1.2.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "order-service",
        "workers": {n: t.is_alive() for n, t in threads.items()},
    }


@app.get("/orders")
def get_orders():
    with orders_lock:
        return {
            "count": len(orders),
            "orders": [public_order(o) for o in orders.values()],
        }


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    with orders_lock:
        order = orders.get(order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return public_order(order)


@app.post("/orders/checkout", status_code=202)
def checkout(
    request: CheckoutRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Starts a checkout. Returns 202 with the order in PENDING_PAYMENT.
    The final status arrives asynchronously: poll GET /orders/{id}."""
    if not idempotency_key or not (8 <= len(idempotency_key) <= 128):
        raise HTTPException(
            400, "Idempotency-Key header (8-128 chars) is required"
        )

    fingerprint = (request.product_id, request.quantity, request.simulate_failure)

    with idempotency_lock:
        record = idempotency.get(idempotency_key)
        if record:
            if record["fingerprint"] != fingerprint:
                raise HTTPException(
                    409, "Idempotency-Key reused with a different request"
                )
            if record["order_id"] is None:
                raise HTTPException(
                    409, "A request with this Idempotency-Key is still in progress"
                )
            with orders_lock:
                return public_order(orders[record["order_id"]])

        idempotency[idempotency_key] = {"fingerprint": fingerprint, "order_id": None}

    try:
        order = create_order(request)
    except BaseException:
        with idempotency_lock:
            idempotency.pop(idempotency_key, None)
        raise

    with idempotency_lock:
        idempotency[idempotency_key]["order_id"] = order["order_id"]

    return public_order(order)
