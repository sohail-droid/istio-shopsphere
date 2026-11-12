
from fastapi import FastAPI, HTTPException, Query
from threading import Lock

app = FastAPI(
    title="ShopSphere Inventory Service",
    description="Manages stock, reservations, commits and releases",
    version="1.1.0",
)

inventory = {
    1: {"product_id": 1, "available": 25, "reserved": 0},
    2: {"product_id": 2, "available": 18, "reserved": 0},
    3: {"product_id": 3, "available": 12, "reserved": 0},
}

# reservation_id -> reservation details and state
reservations = {}

inventory_lock = Lock()


@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "inventory-service"}


@app.get("/inventory")
def get_inventory():
    with inventory_lock:
        return {
            "count": len(inventory),
            "inventory": list(inventory.values()),
        }


@app.get("/inventory/{product_id}")
def get_product_inventory(product_id: int):
    with inventory_lock:
        item = inventory.get(product_id)
        if item is None:
            raise HTTPException(404, "Inventory record not found")
        return item


@app.post("/inventory/{product_id}/reserve")
def reserve_stock(
    product_id: int,
    quantity: int = Query(gt=0),
    reservation_id: str = Query(min_length=1),
):
    with inventory_lock:
        item = inventory.get(product_id)

        if item is None:
            raise HTTPException(404, "Inventory record not found")

        existing = reservations.get(reservation_id)

        if existing:
            if (
                existing["product_id"] != product_id
                or existing["quantity"] != quantity
            ):
                raise HTTPException(
                    409, "Reservation ID reused with different details"
                )

            if existing["status"] != "RESERVED":
                raise HTTPException(
                    409, f"Reservation is already {existing['status']}"
                )

            return {
                "message": "Existing reservation returned",
                **existing,
            }

        if item["available"] < quantity:
            raise HTTPException(409, "Insufficient stock")

        item["available"] -= quantity
        item["reserved"] += quantity

        reservation = {
            "reservation_id": reservation_id,
            "product_id": product_id,
            "quantity": quantity,
            "status": "RESERVED",
        }

        reservations[reservation_id] = reservation

        return {
            "message": "Stock reserved successfully",
            **reservation,
            "available": item["available"],
            "reserved": item["reserved"],
        }


@app.post("/inventory/{product_id}/commit")
def commit_reservation(
    product_id: int,
    reservation_id: str = Query(min_length=1),
):
    with inventory_lock:
        reservation = reservations.get(reservation_id)

        if reservation is None or reservation["product_id"] != product_id:
            raise HTTPException(404, "Reservation not found")

        if reservation["status"] == "COMMITTED":
            return {
                "message": "Reservation was already committed",
                **reservation,
            }

        if reservation["status"] == "RELEASED":
            raise HTTPException(409, "Released reservation cannot be committed")

        item = inventory[product_id]

        item["reserved"] -= reservation["quantity"]
        reservation["status"] = "COMMITTED"

        return {
            "message": "Stock reservation committed",
            **reservation,
            "available": item["available"],
            "reserved": item["reserved"],
        }


@app.post("/inventory/{product_id}/release")
def release_reservation(
    product_id: int,
    reservation_id: str = Query(min_length=1),
):
    with inventory_lock:
        reservation = reservations.get(reservation_id)

        if reservation is None or reservation["product_id"] != product_id:
            raise HTTPException(404, "Reservation not found")

        if reservation["status"] == "RELEASED":
            return {
                "message": "Reservation was already released",
                **reservation,
            }

        if reservation["status"] == "COMMITTED":
            raise HTTPException(409, "Committed reservation cannot be released")

        item = inventory[product_id]

        item["reserved"] -= reservation["quantity"]
        item["available"] += reservation["quantity"]

        reservation["status"] = "RELEASED"

        return {
            "message": "Reserved stock released successfully",
            **reservation,
            "available": item["available"],
            "reserved": item["reserved"],
        }
