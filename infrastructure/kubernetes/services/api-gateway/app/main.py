"""ShopSphere API Gateway.

Edge layer only: CORS, rate limiting, request validation and routing.
It holds NO checkout business logic and never calls Payment Service for a
checkout. Checkout is handed to Order Service, which owns the whole flow.
"""
import os
import threading
import time
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

PRODUCT_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:8000")
INVENTORY_URL = os.getenv("INVENTORY_SERVICE_URL", "http://inventory-service:8000")
ORDER_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8000")
PAYMENT_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8000")  # health only

CHECKOUT_LIMIT = int(os.getenv("CHECKOUT_RATE_LIMIT", "10"))
CHECKOUT_WINDOW = int(os.getenv("CHECKOUT_RATE_WINDOW_SECONDS", "60"))

app = FastAPI(
    title="ShopSphere API Gateway",
    description="Single entry point for the ShopSphere storefront",
    version="1.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your storefront origin in production
    allow_methods=["*"],
    allow_headers=["*"],
)


class CheckoutRequest(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    simulate_failure: bool = False


# --- simple in-memory sliding-window rate limiter (per client IP) ---------
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def check_rate_limit(client_ip: str) -> None:
    now = time.time()
    with _hits_lock:
        window = _hits[client_ip]
        while window and window[0] <= now - CHECKOUT_WINDOW:
            window.popleft()
        if len(window) >= CHECKOUT_LIMIT:
            raise HTTPException(429, "Too many checkout requests, slow down")
        window.append(now)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def get_json(client, url):
    try:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            raise HTTPException(exc.response.status_code, exc.response.text)
        raise HTTPException(503, f"Backend service error: {url}")
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"Backend service unavailable: {url}. {exc}")


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "api-gateway"}


@app.get("/api/health")
async def services_health():
    services = {
        "product": PRODUCT_URL,
        "inventory": INVENTORY_URL,
        "order": ORDER_URL,
        "payment": PAYMENT_URL,
    }
    results = {}

    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, base_url in services.items():
            try:
                response = await client.get(f"{base_url}/health")
                results[name] = (
                    "healthy" if response.status_code == 200 else "unhealthy"
                )
            except httpx.HTTPError:
                results[name] = "unreachable"

    return {"gateway": "healthy", "services": results}


@app.get("/api/products")
async def products():
    async with httpx.AsyncClient(timeout=10.0) as client:
        product_data = await get_json(client, f"{PRODUCT_URL}/products")
        inventory_data = await get_json(client, f"{INVENTORY_URL}/inventory")

    stock_by_id = {
        item["product_id"]: item for item in inventory_data.get("inventory", [])
    }

    enriched = []
    for product in product_data.get("products", []):
        stock = stock_by_id.get(product["id"], {})
        enriched.append({
            **product,
            "available": stock.get("available", 0),
            "reserved": stock.get("reserved", 0),
        })

    return {
    "version": product_data.get("version", "unknown"),
    "count": len(enriched),
    "products": enriched,
}

@app.get("/api/inventory")
async def inventory():
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await get_json(client, f"{INVENTORY_URL}/inventory")


@app.get("/api/orders")
async def orders():
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await get_json(client, f"{ORDER_URL}/orders")


@app.get("/api/orders/{order_id}")
async def get_order(order_id: str):
    """Storefront polls this until status is CONFIRMED or PAYMENT_FAILED."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await get_json(client, f"{ORDER_URL}/orders/{order_id}")


@app.post("/api/checkout", status_code=202)
async def checkout(
    body: CheckoutRequest,
    request: Request,
    idempotency_key: str = Header(default=None, alias="Idempotency-Key"),
):
    """Hands the checkout to Order Service and returns 202 immediately.

    The client generates the Idempotency-Key (e.g. one UUID per click of the
    Pay button) and reuses it on retries, so a retry never creates a second
    order or a second charge.
    """
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header is required")

    check_rate_limit(client_ip(request))

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{ORDER_URL}/orders/checkout",
                json=body.model_dump(),
                headers={"Idempotency-Key": idempotency_key},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"Order Service unavailable: {exc}")

    try:
        content = response.json()
    except ValueError:
        content = {"detail": response.text}

    return JSONResponse(status_code=response.status_code, content=content)
