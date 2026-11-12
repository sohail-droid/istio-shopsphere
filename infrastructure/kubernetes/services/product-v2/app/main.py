import os

from fastapi import FastAPI, HTTPException, Response

VERSION = os.getenv("APP_VERSION", "v2")
POD_NAME = os.getenv("POD_NAME", "unknown")

app = FastAPI(
    title="ShopSphere Product Service",
    description="Manages the ShopSphere product catalogue",
    version=VERSION,
)

products = [
    {"id": 1, "name": "iPhone 17", "description": "Apple smartphone",
     "price": 79999.00, "category": "Electronics", "stock": 25},
    {"id": 2, "name": "Samsung Galaxy S25", "description": "Samsung flagship smartphone",
     "price": 74999.00, "category": "Electronics", "stock": 18},
    {"id": 3, "name": "Sony WH-1000XM6", "description": "Wireless noise-cancelling headphones",
     "price": 29999.00, "category": "Accessories", "stock": 12},
]


# Every response says which version/pod served it, so the split is visible.
@app.middleware("http")
async def add_version_header(request, call_next):
    response = await call_next(request)
    response.headers["X-Service-Version"] = VERSION
    response.headers["X-Served-By"] = POD_NAME
    return response


@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "product-service", "version": VERSION}


@app.get("/version")
def version():
    return {"service": "product-service", "version": VERSION, "pod": POD_NAME}


@app.get("/products")
def get_products():
    return {
        "version": VERSION,   # <- the visible difference vs v1
        "count": len(products),
        "products": products,
    }


@app.get("/products/{product_id}")
def get_product(product_id: int):
    for product in products:
        if product["id"] == product_id:
            return {"version": VERSION, **product}
    raise HTTPException(status_code=404, detail=f"Product {product_id} not found")