"""End-to-end checks for the async checkout flow.

Usage:  python scripts/e2e_test.py [BASE_URL]      (default http://localhost:8000)
"""
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
client = httpx.Client(base_url=BASE, timeout=15.0)
failures = []


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {name} {detail}")
    if not condition:
        failures.append(name)


def stock(product_id):
    items = client.get("/api/inventory").json()["inventory"]
    return next(i for i in items if i["product_id"] == product_id)


def wait_final(order_id, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        order = client.get(f"/api/orders/{order_id}").json()
        if order["status"] != "PENDING_PAYMENT":
            return order
        time.sleep(1)
    return order


def checkout(body, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/api/checkout", json=body, headers=headers)


# 0. health
health = client.get("/api/health").json()
check("all services healthy", all(v == "healthy" for v in health["services"].values()), health["services"])

# 1. happy path: 202 immediately, confirmed later via webhook + event
before = stock(1)
start = time.time()
r = checkout({"product_id": 1, "quantity": 2}, key=str(uuid.uuid4()))
check("checkout returns 202", r.status_code == 202, r.status_code)
order = r.json()
check("returns fast, not blocked on payment", time.time() - start < 2.5, f"{time.time()-start:.2f}s")
check("order starts PENDING_PAYMENT", order["status"] == "PENDING_PAYMENT", order["status"])
check("payment created as PENDING", order.get("payment", {}).get("status") == "PENDING")
final = wait_final(order["order_id"])
check("order becomes CONFIRMED via event", final["status"] == "CONFIRMED", final["status"])
check("settled by event (not reconciler)", final.get("settled_by") == "event", final.get("settled_by"))
after = stock(1)
check("stock committed (reserved back to 0)", after["reserved"] == before["reserved"] and after["available"] == before["available"] - 2, after)

# 2. payment declined -> stock released
before = stock(2)
r = checkout({"product_id": 2, "quantity": 3, "simulate_failure": True}, key=str(uuid.uuid4()))
final = wait_final(r.json()["order_id"])
check("declined payment -> PAYMENT_FAILED", final["status"] == "PAYMENT_FAILED", final["status"])
check("stock released after failure", stock(2) == before, stock(2))

# 3. client retry with same key -> same order, stock taken once
before = stock(3)
key = str(uuid.uuid4())
a = checkout({"product_id": 3, "quantity": 1}, key=key).json()
b = checkout({"product_id": 3, "quantity": 1}, key=key).json()
check("retry returns same order", a["order_id"] == b["order_id"])
wait_final(a["order_id"])
check("stock taken only once", stock(3)["available"] == before["available"] - 1, stock(3))

# 4. guards
check("same key, different body -> 409", checkout({"product_id": 3, "quantity": 5}, key=key).status_code == 409)
check("missing Idempotency-Key -> 400", checkout({"product_id": 1, "quantity": 1}).status_code == 400)
check("unknown product -> 404", checkout({"product_id": 999, "quantity": 1}, key=str(uuid.uuid4())).status_code == 404)
check("insufficient stock -> 409", checkout({"product_id": 1, "quantity": 9999}, key=str(uuid.uuid4())).status_code == 409)

print()
print("ALL PASSED" if not failures else f"FAILED: {failures}")
sys.exit(1 if failures else 0)
