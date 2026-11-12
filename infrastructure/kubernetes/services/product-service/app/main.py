from fastapi import FastAPI, HTTPException

app = FastAPI(
    title="ShopSphere Product Service",
    description="Manages the ShopSphere product catalogue",
    version="1.0.0",
)

# Temporary sample data.
# We will replace this with PostgreSQL later.
products = [
    {
        "id": 1,
        "name": "iPhone 17",
        "description": "Apple smartphone",
        "price": 79999.00,
        "category": "Electronics",
        "stock": 25,
    },
    {
        "id": 2,
        "name": "Samsung Galaxy S25",
        "description": "Samsung flagship smartphone",
        "price": 74999.00,
        "category": "Electronics",
        "stock": 18,
    },
    {
        "id": 3,
        "name": "Sony WH-1000XM6",
        "description": "Wireless noise-cancelling headphones",
        "price": 29999.00,
        "category": "Accessories",
        "stock": 12,
    },
]


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "product-service",
    }


@app.get("/products")
def get_products():
    return {
        "count": len(products),
        "products": products,
    }


@app.get("/products/{product_id}")
def get_product(product_id: int):
    for product in products:
        if product["id"] == product_id:
            return product

    raise HTTPException(
        status_code=404,
        detail=f"Product {product_id} not found",
    )
