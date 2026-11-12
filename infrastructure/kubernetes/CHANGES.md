# Async checkout refactor (production-style flow)

## Flow
Client -> **Gateway** (rate limit, Idempotency-Key check, routing only)
       -> **Order Service** `POST /orders/checkout`: verify product, reserve stock,
          save order `PENDING_PAYMENT`, ask Payment to create a payment, reply **202**
       -> **Payment Service**: creates payment (`PENDING`); the PSP later calls the
          signed webhook `POST /webhooks/psp`; Payment publishes
          `payment.succeeded` / `payment.failed` to Redis Stream `payments.events`
       -> **Order Service** consumes the event: commit/release stock,
          order -> `CONFIRMED` / `PAYMENT_FAILED`
Storefront polls `GET /api/orders/{id}` for the final status.

## What changed
- api-gateway: no checkout logic, never calls payment for checkout; per-IP rate limit; requires `Idempotency-Key`.
- order-service: new `POST /orders/checkout`; event consumer (Redis consumer group, ack/retry/reclaim); reconciler for orders stuck in `PENDING_PAYMENT`. Removed public `POST /orders` and `POST /orders/{id}/status` (status now changes only via events/reconciler).
- payment-service: `POST /payments` returns `PENDING`; HMAC-verified webhook; duplicate-safe; webhook returns 503 if the bus is down so the PSP retries. A simulated PSP stands in for Razorpay/Stripe.
- Idempotency key now comes from the client; payment key is `pay-<order_id>`, so retries never double charge.
- New: redis.yaml, payment-deployment.yaml, payment-service.yaml, docker-compose.yml, scripts/.

## Deploy (GKE)
    ./scripts/build_push.sh 1.2.0
    kubectl -n shopsphere create secret generic psp-webhook --from-literal=secret="$(openssl rand -hex 32)"
    kubectl apply -f redis.yaml -f payment-service.yaml -f payment-deployment.yaml \
                  -f order-deployment.yaml -f api-gateway-deploy.yaml
## Local
    docker compose up --build && python scripts/e2e_test.py http://localhost:8000

## Still not production-grade (known)
- Orders/payments/idempotency are in memory: keep replicas at 1 until a real DB (Cloud SQL/Firestore) is added.
- Redis here is a single non-persistent pod; use Memorystore or Pub/Sub in production.
- Real PSP: replace the simulator with Razorpay/Stripe SDK calls and point their webhook at `/webhooks/psp` (use their signature scheme).
- `services/order-service/app/main.py.backup` is left untouched; delete it.
