"""ShopSphere Payment Service.

Production-style flow (simulated PSP):

  1. POST /payments            -> creates a payment intent, status PENDING.
  2. "PSP" confirms later      -> calls POST /webhooks/psp (HMAC signed).
  3. Webhook handler           -> marks payment SUCCEEDED/FAILED and publishes
                                  payment.succeeded / payment.failed to a
                                  Redis Stream. Order Service consumes it.

In real life step 2 is Razorpay/Stripe/PayU calling your webhook. Here a
background timer plays the PSP so the whole flow can be tested end to end.
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional
from uuid import uuid4

import httpx
import redis
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("payment-service")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
EVENTS_STREAM = os.getenv("PAYMENT_EVENTS_STREAM", "payments.events")
PSP_WEBHOOK_SECRET = os.getenv("PSP_WEBHOOK_SECRET", "dev-only-secret")
PSP_CALLBACK_URL = os.getenv(
    "PSP_CALLBACK_URL", "http://127.0.0.1:8000/webhooks/psp"
)
PSP_DELAY_SECONDS = float(os.getenv("PSP_SIMULATED_DELAY_SECONDS", "3"))

app = FastAPI(
    title="ShopSphere Payment Service",
    description="Creates payments, receives PSP webhooks, publishes events",
    version="1.2.0",
)

payments = {}
payments_by_order = {}
idempotency_records = {}
psp_plans = {}  # payment_id -> should the simulated PSP fail it?
processed_psp_events = set()
payment_lock = threading.Lock()

_redis_client: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=3,
            socket_connect_timeout=3,
        )
    return _redis_client


class PaymentRequest(BaseModel):
    order_id: str = Field(min_length=1)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    # Only for the learning simulation: tells the fake PSP to decline.
    simulate_failure: bool = False


class PSPWebhook(BaseModel):
    psp_event_id: str = Field(min_length=1)
    payment_id: str = Field(min_length=1)
    status: Literal["succeeded", "failed"]


def public_view(payment: dict) -> dict:
    return {k: v for k, v in payment.items() if not k.startswith("_")}


def sign(raw_body: bytes) -> str:
    return hmac.new(
        PSP_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()


def publish_payment_event(payment: dict) -> None:
    event_type = (
        "payment.succeeded"
        if payment["status"] == "SUCCEEDED"
        else "payment.failed"
    )
    fields = {
        "event_id": str(uuid4()),
        "type": event_type,
        "payment_id": payment["payment_id"],
        "order_id": payment["order_id"],
        "amount": payment["amount"],
        "currency": payment["currency"],
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    get_redis().xadd(EVENTS_STREAM, fields, maxlen=10000, approximate=True)
    log.info("published %s for order %s", event_type, payment["order_id"])


# --------------------------------------------------------------------------
# Simulated PSP. Delivers the result later, signed, with retries, exactly the
# way a real PSP retries webhooks until it gets a 2xx.
# --------------------------------------------------------------------------
def simulated_psp_callback(payment_id: str) -> None:
    body = {
        "psp_event_id": str(uuid4()),
        "payment_id": payment_id,
        "status": "failed" if psp_plans.get(payment_id) else "succeeded",
    }
    raw = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "X-PSP-Signature": sign(raw),
    }

    for attempt in range(1, 7):
        try:
            response = httpx.post(
                PSP_CALLBACK_URL, content=raw, headers=headers, timeout=5.0
            )
            if response.status_code < 300:
                return
            log.warning(
                "PSP webhook attempt %d got HTTP %d",
                attempt,
                response.status_code,
            )
        except httpx.HTTPError as exc:
            log.warning("PSP webhook attempt %d failed: %s", attempt, exc)
        time.sleep(min(2 ** attempt, 30))


@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "payment-service"}


@app.post("/payments", status_code=201)
def create_payment(
    request: PaymentRequest,
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    """Create a payment intent. Result arrives later via webhook + event."""
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header is required")

    with payment_lock:
        existing = idempotency_records.get(idempotency_key)

        if existing:
            if (
                existing["order_id"] != request.order_id
                or existing["amount"] != str(request.amount)
            ):
                raise HTTPException(
                    409, "Idempotency key reused with different payment details"
                )
            return public_view(existing)

        if request.order_id in payments_by_order:
            raise HTTPException(409, "A payment already exists for this order")

        payment_id = str(uuid4())
        payment = {
            "payment_id": payment_id,
            "order_id": request.order_id,
            "amount": str(request.amount),
            "currency": "INR",
            "status": "PENDING",
            "message": "Awaiting confirmation from payment provider",
            "_event_published": False,
        }

        payments[payment_id] = payment
        payments_by_order[request.order_id] = payment_id
        idempotency_records[idempotency_key] = payment
        psp_plans[payment_id] = request.simulate_failure

    timer = threading.Timer(
        PSP_DELAY_SECONDS, simulated_psp_callback, args=(payment_id,)
    )
    timer.daemon = True
    timer.start()

    return public_view(payment)


@app.post("/webhooks/psp")
async def psp_webhook(
    request: Request,
    x_psp_signature: Optional[str] = Header(default=None),
):
    raw = await request.body()

    if not x_psp_signature or not hmac.compare_digest(
        sign(raw), x_psp_signature
    ):
        raise HTTPException(401, "Invalid webhook signature")

    try:
        event = PSPWebhook(**json.loads(raw))
    except (ValueError, ValidationError):
        raise HTTPException(400, "Malformed webhook payload")

    return await run_in_threadpool(handle_psp_event, event)


def handle_psp_event(event: PSPWebhook) -> dict:
    new_status = "SUCCEEDED" if event.status == "succeeded" else "FAILED"

    with payment_lock:
        payment = payments.get(event.payment_id)

        if payment is None:
            raise HTTPException(404, "Unknown payment")

        if event.psp_event_id in processed_psp_events:
            # Duplicate delivery. Still make sure the event got published.
            pass
        elif payment["status"] == "PENDING":
            payment["status"] = new_status
            payment["message"] = (
                "Payment successful"
                if new_status == "SUCCEEDED"
                else "Payment declined by provider"
            )
            processed_psp_events.add(event.psp_event_id)
        elif payment["status"] != new_status:
            log.error(
                "Conflicting PSP result for payment %s: have %s, got %s",
                event.payment_id,
                payment["status"],
                new_status,
            )
            return {"received": True, "ignored": "conflicting result"}

        needs_publish = not payment["_event_published"]

    if needs_publish:
        try:
            publish_payment_event(payment)
        except redis.RedisError as exc:
            # 503 makes the PSP retry the webhook, so the event is not lost.
            log.error("could not publish event: %s", exc)
            raise HTTPException(503, "Event bus unavailable, retry later")

        with payment_lock:
            payment["_event_published"] = True

    return {"received": True}


@app.get("/payments/by-order/{order_id}")
def get_payment_by_order(order_id: str):
    with payment_lock:
        payment_id = payments_by_order.get(order_id)
        if payment_id is None:
            raise HTTPException(404, "Payment not found for order")
        return public_view(payments[payment_id])


@app.get("/payments/{payment_id}")
def get_payment(payment_id: str):
    with payment_lock:
        payment = payments.get(payment_id)
        if payment is None:
            raise HTTPException(404, "Payment not found")
        return public_view(payment)
