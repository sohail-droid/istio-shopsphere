import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const money = (value) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(value || 0);

// crypto.randomUUID only exists on https / localhost, so keep a fallback.
function newKey() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const statusStyle = (status) =>
  status === "CONFIRMED"
    ? "bg-green-50 text-green-800"
    : status === "PAYMENT_FAILED"
    ? "bg-red-50 text-red-700"
    : "bg-amber-50 text-amber-800";

function App() {
  const [products, setProducts] = useState([]);
  const [productVersion, setProductVersion] = useState("unknown");
  const [cart, setCart] = useState({});
  const [orders, setOrders] = useState([]);
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [checkoutLoading, setCheckoutLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [simulateFailure, setSimulateFailure] = useState(false);
  const [activeTab, setActiveTab] = useState("store");
  const isV2 = productVersion === "v2";

  // One Idempotency-Key per cart line attempt ("productId:qty:fail" -> key).
  // Kept across retries so a retry can never create a second order or charge.
  const attemptKeys = useRef({});

  const loadProducts = useCallback(async () => {
  const response = await fetch("/api/products");

  if (!response.ok) {
    throw new Error("Unable to load products");
  }

  const data = await response.json();

  setProducts(data.products || []);
  setProductVersion(data.version === "v2" ? "v2" : "v1");
}, []);

  const loadOrders = useCallback(async () => {
    const response = await fetch("/api/orders");
    if (!response.ok) throw new Error("Unable to load orders");
    const data = await response.json();
    setOrders(data.orders || []);
  }, []);

  const loadHealth = useCallback(async () => {
    try {
      const response = await fetch("/api/health");
      if (!response.ok) throw new Error("Health check failed");
      setHealth(await response.json());
    } catch {
      setHealth(null);
    }
  }, []);

  const refreshData = useCallback(async () => {
    setError("");
    try {
      await Promise.all([loadProducts(), loadOrders(), loadHealth()]);
    } catch (err) {
      setError(err.message || "Could not connect to ShopSphere");
    } finally {
      setLoading(false);
    }
  }, [loadProducts, loadOrders, loadHealth]);

  useEffect(() => {
    refreshData();
  }, [refreshData]);

  // Orders now finish asynchronously (payment webhook -> event), so keep the
  // Orders tab fresh while anything is still waiting for payment.
  const hasPending = orders.some((o) => o.status === "PENDING_PAYMENT");
  useEffect(() => {
    if (activeTab !== "orders" || !hasPending) return undefined;
    const timer = setInterval(() => {
      loadOrders().catch(() => {});
    }, 3000);
    return () => clearInterval(timer);
  }, [activeTab, hasPending, loadOrders]);

  const cartItems = useMemo(
    () =>
      products
        .filter((product) => cart[product.id] > 0)
        .map((product) => ({
          ...product,
          quantity: cart[product.id],
        })),
    [products, cart]
  );

  const cartCount = cartItems.reduce((sum, item) => sum + item.quantity, 0);
  const cartTotal = cartItems.reduce(
    (sum, item) => sum + item.price * item.quantity,
    0
  );

  function updateQuantity(product, change) {
    setCart((current) => {
      const next = { ...current };
      const quantity = (next[product.id] || 0) + change;

      if (quantity <= 0) {
        delete next[product.id];
      } else if (quantity <= product.available) {
        next[product.id] = quantity;
      }

      return next;
    });
  }

  // Poll until every order leaves PENDING_PAYMENT (or we give up after ~90s).
  async function waitForOrders(orderIds) {
    let latest = [];
    for (let attempt = 0; attempt < 60; attempt++) {
      latest = await Promise.all(
        orderIds.map(async (id) => {
          const response = await fetch(`/api/orders/${id}`);
          return response.ok ? response.json() : { order_id: id, status: "PENDING_PAYMENT" };
        })
      );
      if (latest.every((o) => o.status !== "PENDING_PAYMENT")) break;
      setNotice(
        `Waiting for payment confirmation (${
          latest.filter((o) => o.status === "PENDING_PAYMENT").length
        } pending)...`
      );
      await sleep(1500);
    }
    return latest;
  }

  async function checkout() {
    if (!cartItems.length) return;

    setCheckoutLoading(true);
    setError("");
    setNotice("");

    try {
      // Current Order Service accepts one product per order.
      // Submit each cart line as an individual order.
      const accepted = [];

      for (const item of cartItems) {
        const attemptId = `${item.id}:${item.quantity}:${simulateFailure}`;
        if (!attemptKeys.current[attemptId]) {
          attemptKeys.current[attemptId] = newKey();
        }

        const response = await fetch("/api/checkout", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": attemptKeys.current[attemptId],
          },
          body: JSON.stringify({
            product_id: item.id,
            quantity: item.quantity,
            simulate_failure: simulateFailure,
          }),
        });

        const data = await response.json();

        // 202 = order accepted, payment still in progress.
        if (response.status !== 202) {
          // Definitive rejection (out of stock, bad request...). Nothing was
          // created, so the next attempt should use a fresh key.
          delete attemptKeys.current[attemptId];
          throw new Error(
            typeof data.detail === "string"
              ? data.detail
              : data.detail?.message || "Checkout failed"
          );
        }

        accepted.push(data.order_id);
      }

      setNotice("Order placed. Waiting for payment confirmation...");
      const finalOrders = await waitForOrders(accepted);

      const confirmed = finalOrders.filter((o) => o.status === "CONFIRMED").length;
      const failed = finalOrders.filter((o) => o.status === "PAYMENT_FAILED").length;
      const pending = finalOrders.length - confirmed - failed;

      setNotice(
        `${finalOrders.length} order(s) placed. ${confirmed} confirmed, ${failed} payment failed` +
          (pending ? `, ${pending} still pending (check Orders).` : ".")
      );

      attemptKeys.current = {}; // whole checkout done: next purchase gets new keys
      setCart({});
      await refreshData();
      setActiveTab("orders");
    } catch (err) {
      // Network errors keep their keys, so clicking "Place order" again
      // safely retries without creating duplicate orders.
      setError(err.message || "Checkout failed. Refresh and check orders.");
      await refreshData();
    } finally {
      setCheckoutLoading(false);
    }
  }

  const serviceEntries = health?.services
    ? Object.entries(health.services)
    : [];

  return (
    <div className="min-h-screen text-slate-900">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4">
          <button
            onClick={() => setActiveTab("store")}
            className="flex items-center gap-3"
          >
            <div className="grid h-10 w-10 place-items-center rounded-xl bg-brand-600 text-xl font-black text-white">
              S
            </div>
            <div className="text-left">
              <div className="text-xl font-black tracking-tight">
                Shop<span className="text-brand-600">Sphere</span>
              </div>
              <div className="text-xs text-slate-500">
                Your everyday technology store
              </div>
            </div>
          </button>

          <nav className="flex items-center gap-2">
            <button
              onClick={() => setActiveTab("store")}
              className={`rounded-lg px-3 py-2 text-sm font-semibold ${
                activeTab === "store"
                  ? "bg-brand-50 text-brand-700"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              Store
            </button>
            <button
              onClick={() => setActiveTab("orders")}
              className={`rounded-lg px-3 py-2 text-sm font-semibold ${
                activeTab === "orders"
                  ? "bg-brand-50 text-brand-700"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              Orders
            </button>
            <button
              onClick={() => setActiveTab("services")}
              className={`rounded-lg px-3 py-2 text-sm font-semibold ${
                activeTab === "services"
                  ? "bg-brand-50 text-brand-700"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              Services
            </button>
            <button
              onClick={() => setActiveTab("store")}
              className="relative ml-1 rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-bold text-white hover:bg-slate-700"
            >
              Cart
              <span className="ml-2 rounded-full bg-brand-500 px-2 py-0.5 text-xs">
                {cartCount}
              </span>
            </button>
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-5 py-8">
        {error && (
          <div className="mb-5 flex items-start justify-between rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            <span>{error}</span>
            <button onClick={() => setError("")} className="font-bold">
              Dismiss
            </button>
          </div>
        )}

        {notice && (
          <div className="mb-5 flex items-start justify-between rounded-xl border border-green-200 bg-green-50 p-4 text-sm text-green-800">
            <span>{notice}</span>
            <button onClick={() => setNotice("")} className="font-bold">
              Dismiss
            </button>
          </div>
        )}

        {activeTab === "store" && (
          <>
            <section className={`mb-9 overflow-hidden rounded-3xl px-7 py-12 text-white md:px-12 ${isV2 ? "bg-gradient-to-br from-blue-950 via-violet-900 to-fuchsia-700" : "bg-gradient-to-r from-slate-950 via-indigo-950 to-brand-700"}`}>
              <div className="max-w-2xl">
                <div className="mb-3 inline-flex rounded-full border border-white/20 bg-white/10 px-3 py-1 text-xs font-semibold tracking-wide">
                  {isV2 ? "V2 CANARY EXPERIENCE" : "THE SHOPSPHERE EXPERIENCE"}
                </div>
                <h1 className="text-3xl font-black leading-tight md:text-5xl">
                  Technology that
                  <br />
                  moves you forward.
                </h1>
                <p className="mt-4 max-w-lg text-sm leading-6 text-indigo-100 md:text-base">
                  Discover your next favourite device. Browse products, check
                  live availability, and place your order in a few clicks.
                </p>
                <button
                  onClick={() =>
                    document
                      .getElementById("products")
                      ?.scrollIntoView({ behavior: "smooth" })
                  }
                  className="mt-7 rounded-xl bg-white px-5 py-3 text-sm font-bold text-brand-700 hover:bg-indigo-50"
                >
                  Explore products
                </button>
              </div>
            </section>

            <div
              id="products"
              className="mb-5 flex items-end justify-between gap-4"
            >
              <div>
                <p className="text-xs font-bold uppercase tracking-widest text-brand-600">
                  Shop our collection
                </p>
                <h2 className="mt-1 text-2xl font-black">Featured products</h2>
                <p className="mt-1 text-sm text-slate-500">
                  Live product and inventory information from our services
                </p>
              </div>
              <button
                onClick={refreshData}
                className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-semibold hover:bg-slate-50"
              >
                ↻ Refresh
              </button>
            </div>

            {loading ? (
              <div className="rounded-2xl bg-white p-12 text-center text-slate-500">
                Loading ShopSphere catalogue...
              </div>
            ) : (
              <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
                {products.map((product, index) => {
                  const quantity = cart[product.id] || 0;
                  const unavailable = product.available <= 0;

                  return (
                    <article
                      key={product.id}
                      className={`overflow-hidden rounded-2xl border bg-white shadow-sm transition hover:-translate-y-1 hover:shadow-lg ${isV2 ? "border-blue-300 shadow-blue-100" : "border-slate-200"}`}
                    >
                      <div
                        className={`grid h-44 place-items-center ${
                          index === 0
                            ? "bg-gradient-to-br from-slate-100 to-indigo-100"
                            : index === 1
                            ? "bg-gradient-to-br from-slate-100 to-cyan-100"
                            : "bg-gradient-to-br from-slate-100 to-violet-100"
                        }`}
                      >
                        <div className="text-7xl">
                          {index === 0 ? "📱" : index === 1 ? "📲" : "🎧"}
                        </div>
                      </div>

                      <div className="p-5">
                        <div className="mb-2 flex items-center justify-between">
                          <span className="rounded-full bg-brand-50 px-2.5 py-1 text-xs font-bold text-brand-700">
                            {product.category}
                          </span>
                          <span
                            className={`text-xs font-semibold ${
                              unavailable ? "text-red-600" : "text-green-700"
                            }`}
                          >
                            {unavailable
                              ? "Out of stock"
                              : `${product.available} available`}
                          </span>
                        </div>

                        <h3 className="text-lg font-bold">{product.name}</h3>
                        <p className="mt-1 min-h-10 text-sm text-slate-500">
                          {product.description}
                        </p>

                        <div className="mt-4 flex items-center justify-between">
                          <span className="text-xl font-black">
                            {money(product.price)}
                          </span>

                          {quantity === 0 ? (
                            <button
                              disabled={unavailable}
                              onClick={() => updateQuantity(product, 1)}
                              className="rounded-xl bg-brand-600 px-4 py-2.5 text-sm font-bold text-white hover:bg-brand-700"
                            >
                              Add to cart
                            </button>
                          ) : (
                            <div className="flex items-center gap-3 rounded-xl bg-slate-100 p-1">
                              <button
                                onClick={() => updateQuantity(product, -1)}
                                className="grid h-8 w-8 place-items-center rounded-lg bg-white font-bold"
                              >
                                −
                              </button>
                              <span className="min-w-4 text-center text-sm font-bold">
                                {quantity}
                              </span>
                              <button
                                disabled={quantity >= product.available}
                                onClick={() => updateQuantity(product, 1)}
                                className="grid h-8 w-8 place-items-center rounded-lg bg-white font-bold"
                              >
                                +
                              </button>
                            </div>
                          )}
                        </div>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}

            <section className="mt-9 grid gap-5 lg:grid-cols-[1fr_360px]">
              <div className="rounded-2xl border border-slate-200 bg-white p-6">
                <h2 className="text-xl font-black">Your shopping cart</h2>
                <p className="mt-1 text-sm text-slate-500">
                  Review your items before checkout.
                </p>

                {cartItems.length === 0 ? (
                  <div className="py-9 text-center text-sm text-slate-400">
                    Your cart is empty. Add a product to get started.
                  </div>
                ) : (
                  <div className="mt-5 divide-y divide-slate-100">
                    {cartItems.map((item) => (
                      <div
                        key={item.id}
                        className="flex items-center justify-between gap-3 py-3"
                      >
                        <div>
                          <p className="font-bold">{item.name}</p>
                          <p className="text-sm text-slate-500">
                            {money(item.price)} × {item.quantity}
                          </p>
                        </div>
                        <div className="font-bold">
                          {money(item.price * item.quantity)}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="h-fit rounded-2xl border border-slate-200 bg-white p-6">
                <h2 className="text-xl font-black">Order summary</h2>
                <div className="mt-5 flex justify-between text-sm text-slate-500">
                  <span>Items ({cartCount})</span>
                  <span>{money(cartTotal)}</span>
                </div>
                <div className="mt-3 flex justify-between border-t border-slate-100 pt-4 text-lg font-black">
                  <span>Total</span>
                  <span>{money(cartTotal)}</span>
                </div>

                <label className="mt-5 flex cursor-pointer items-start gap-3 rounded-xl bg-amber-50 p-3 text-sm">
                  <input
                    type="checkbox"
                    checked={simulateFailure}
                    onChange={(event) =>
                      setSimulateFailure(event.target.checked)
                    }
                    className="mt-1 h-4 w-4 accent-amber-600"
                  />
                  <span>
                    <span className="block font-bold text-amber-900">
                      Simulate payment failure
                    </span>
                    <span className="mt-1 block text-xs text-amber-800">
                      Demo option for testing an unsuccessful payment.
                    </span>
                  </span>
                </label>

                <button
                  onClick={checkout}
                  disabled={!cartItems.length || checkoutLoading}
                  className="mt-5 w-full rounded-xl bg-brand-600 px-4 py-3 font-bold text-white hover:bg-brand-700"
                >
                  {checkoutLoading
                    ? "Processing checkout..."
                    : "Place order"}
                </button>
                <p className="mt-3 text-center text-xs text-slate-400">
                  Payment is simulated for this demonstration.
                </p>
              </div>
            </section>
          </>
        )}

        {activeTab === "orders" && (
          <section>
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <p className="text-xs font-bold uppercase tracking-widest text-brand-600">
                  Your activity
                </p>
                <h1 className="mt-1 text-3xl font-black">Order history</h1>
                <p className="mt-1 text-sm text-slate-500">
                  Orders returned by the ShopSphere Order Service
                </p>
              </div>
              <button
                onClick={refreshData}
                className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-semibold"
              >
                ↻ Refresh orders
              </button>
            </div>

            <div className="mt-6 space-y-4">
              {orders.length === 0 ? (
                <div className="rounded-2xl border border-slate-200 bg-white p-12 text-center text-slate-500">
                  No orders found yet.
                </div>
              ) : (
                [...orders].reverse().map((order) => (
                  <article
                    key={order.order_id}
                    className="rounded-2xl border border-slate-200 bg-white p-5"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="text-xs text-slate-400">ORDER ID</p>
                        <p className="mt-1 break-all font-mono text-sm font-bold">
                          {order.order_id}
                        </p>
                      </div>
                      <span
                        className={`rounded-full px-3 py-1 text-xs font-bold ${statusStyle(
                          order.status
                        )}`}
                      >
                        {order.status || "UNKNOWN"}
                      </span>
                    </div>
                    <div className="mt-4 grid gap-3 border-t border-slate-100 pt-4 sm:grid-cols-3">
                      <div>
                        <p className="text-xs text-slate-400">Product</p>
                        <p className="mt-1 font-semibold">
                          {order.product?.name || `Product #${order.product_id}`}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-slate-400">Quantity</p>
                        <p className="mt-1 font-semibold">{order.quantity}</p>
                      </div>
                      <div>
                        <p className="text-xs text-slate-400">Reservation</p>
                        <p className="mt-1 font-semibold">
                          {order.reservation?.message || "Not available"}
                        </p>
                      </div>
                    </div>
                  </article>
                ))
              )}
            </div>
          </section>
        )}

        {activeTab === "services" && (
          <section>
            <p className="text-xs font-bold uppercase tracking-widest text-brand-600">
              Platform overview
            </p>
            <h1 className="mt-1 text-3xl font-black">Service health</h1>
            <p className="mt-2 text-sm text-slate-500">
              Live health checks collected by the API Gateway.
            </p>

            <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {serviceEntries.map(([name, status]) => (
                <div
                  key={name}
                  className="rounded-2xl border border-slate-200 bg-white p-5"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-bold capitalize">{name} Service</span>
                    <span
                      className={`h-3 w-3 rounded-full ${
                        status === "healthy" ? "bg-green-500" : "bg-red-500"
                      }`}
                    />
                  </div>
                  <p
                    className={`mt-4 text-sm font-bold ${
                      status === "healthy" ? "text-green-700" : "text-red-700"
                    }`}
                  >
                    {status.toUpperCase()}
                  </p>
                </div>
              ))}
            </div>

            {!health && (
              <div className="mt-5 rounded-xl bg-amber-50 p-4 text-sm text-amber-800">
                Gateway health information is currently unavailable.
              </div>
            )}

            <button
              onClick={loadHealth}
              className="mt-5 rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-semibold"
            >
              ↻ Check services again
            </button>
          </section>
        )}
      </main>

      <footer className="mt-12 border-t border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap justify-between gap-2 px-5 py-6 text-xs text-slate-400">
          <span>© 2026 ShopSphere. Demo e-commerce platform.</span>
          <span>Powered by Kubernetes microservices</span>
        </div>
      </footer>
    </div>
  );
}

export default App;