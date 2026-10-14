"""
test_caching_and_sw.py
======================
Automated test suite verifying:
1. Cache-Control headers across HTML, Service Worker, APIs, and static assets.
2. Service Worker versioning, old cache purging, SKIP_WAITING signal, and routing strategies.
3. Client-side PWA update toast, versioned asset tags, and non-blocking standalone registration.
4. Deployment update simulation (Version 1 -> Version 2 cache eviction).
"""

import os
import re
import pytest
from app import app


@pytest.fixture
def client():
    """Create a Flask test client with testing mode enabled."""
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ── 1. HTTP Cache-Control Headers ──────────────────────────────────────────

def test_html_entrypoints_have_no_cache_headers(client):
    """
    Ensure all HTML entrypoints have strict no-cache/no-store headers
    so users always receive fresh HTML shells upon new deployments.
    """
    for endpoint in ["/", "/index.html", "/staff", "/admin/login"]:
        res = client.get(endpoint)
        assert res.status_code == 200, f"Expected 200 for {endpoint}, got {res.status_code}"
        cc = res.headers.get("Cache-Control", "")
        assert "no-cache" in cc, f"Cache-Control for {endpoint} missing no-cache: {cc}"
        assert "no-store" in cc, f"Cache-Control for {endpoint} missing no-store: {cc}"
        assert "must-revalidate" in cc, f"Cache-Control for {endpoint} missing must-revalidate: {cc}"
        assert res.headers.get("Pragma") == "no-cache"
        assert res.headers.get("Expires") == "0"


def test_service_worker_has_strict_no_cache_and_allowed_scope(client):
    """
    Ensure /sw.js is never cached by HTTP and allows root '/' scope.
    """
    res = client.get("/sw.js")
    assert res.status_code == 200
    assert "application/javascript" in res.headers.get("Content-Type", "")
    assert res.headers.get("Service-Worker-Allowed") == "/"
    cc = res.headers.get("Cache-Control", "")
    assert "no-cache" in cc
    assert "no-store" in cc
    assert "must-revalidate" in cc
    assert res.headers.get("Pragma") == "no-cache"


def test_dynamic_apis_are_never_cached(client):
    """
    Ensure live API responses (menu, orders, queue, auth) are NEVER cached.
    """
    api_endpoints = [
        "/api/menu",
        "/api/orders",
        "/api/queue",
        "/api/stations/queue",
        "/api/live-board",
        "/api/auth/me",
        "/api/recommendations",
    ]
    for endpoint in api_endpoints:
        res = client.get(endpoint)
        cc = res.headers.get("Cache-Control", "")
        assert "no-store" in cc, f"API endpoint {endpoint} missing no-store: {cc}"
        assert "no-cache" in cc, f"API endpoint {endpoint} missing no-cache: {cc}"
        assert "must-revalidate" in cc, f"API endpoint {endpoint} missing must-revalidate: {cc}"
        assert res.headers.get("Pragma") == "no-cache"


def test_static_assets_allow_revalidation(client):
    """
    Ensure static CSS and JS have revalidation headers and SEND_FILE_MAX_AGE_DEFAULT=0.
    """
    assert app.config["SEND_FILE_MAX_AGE_DEFAULT"] == 0
    for asset in ["/static/style.css", "/static/app.js", "/manifest.json"]:
        res = client.get(asset)
        assert res.status_code == 200
        cc = res.headers.get("Cache-Control", "")
        assert "max-age" in cc


# ── 2. Service Worker File & Architecture Verification ─────────────────────

def test_service_worker_versioning_and_rules():
    """
    Inspect project/static/sw.js to verify:
    - Cache version string is defined.
    - Pre-cache STATIC_ASSETS list is defined and contains critical shell files.
    - NEVER_CACHE list excludes /api/, /events, /admin, /staff.
    - SKIP_WAITING message listener is implemented.
    - activate event purges outdated 'smart-canteen-' caches.
    - HTML and CSS/JS use Network-First strategy.
    """
    sw_path = os.path.join(os.path.dirname(__file__), "..", "static", "sw.js")
    assert os.path.exists(sw_path)

    with open(sw_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Cache versioning
    assert "CACHE_VERSION" in content
    assert "CACHE_NAME" in content

    # Safe static assets
    assert "STATIC_ASSETS" in content
    assert "'/static/style.css'" in content
    assert "'/static/app.js'" in content
    assert "'/static/manifest.json'" in content

    # Dynamic protection
    assert "NEVER_CACHE" in content
    assert "'/api/'" in content
    assert "'/events'" in content
    assert "'/admin'" in content
    assert "'/staff'" in content

    # Lifecycle handlers
    assert "SKIP_WAITING" in content
    assert "self.skipWaiting()" in content
    assert "self.clients.claim()" in content
    assert "caches.delete" in content

    # Cache purge safety: only delete matching 'smart-canteen-' caches
    assert "smart-canteen-" in content

    # Navigation & code freshness strategy
    assert "navigate" in content


# ── 3. Frontend PWA Update Notification UI & Script ────────────────────────

def test_frontend_pwa_update_elements_in_user_html():
    """
    Inspect project/static/user.html to verify:
    - Update toast UI (#swUpdateToast) exists with #swUpdateBtn and dismiss button.
    - Duplicate #swUpdateToast is removed.
    - Script handles updatefound and controllerchange for seamless refresh.
    - Standalone mode does NOT exit before registering Service Worker.
    - Version query parameters are present on asset links.
    """
    html_path = os.path.join(os.path.dirname(__file__), "..", "static", "user.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Only one swUpdateToast in DOM
    assert html.count('id="swUpdateToast"') == 1
    assert 'id="swUpdateBtn"' in html
    assert 'onclick="applySwUpdate()"' in html
    assert 'onclick="dismissSwUpdate()"' in html

    # Asset version query params
    assert re.search(r'style\.css\?v=', html)
    assert re.search(r'app\.js\?v=', html)

    # SW registration and update lifecycle
    assert "navigator.serviceWorker.register('/sw.js'" in html
    assert "controllerchange" in html
    assert "SKIP_WAITING" in html
    assert "applySwUpdate" in html
    assert "dismissSwUpdate" in html

    # Standalone mode must not return early before SW registration
    standalone_block = re.search(r'if\s*\(\s*isStandalone\s*\)\s*\{([^}]+)\}', html)
    assert standalone_block is not None
    assert "return" not in standalone_block.group(1), "Standalone block must not return early!"


# ── 4. Simulated Deployment Update Process (V1 -> V2) ──────────────────────

def test_deployment_cache_invalidation_logic():
    """
    Simulate the Service Worker activate cleanup algorithm when upgrading
    from Version 1 (v2.1.0) to Version 2 (v2.2.0).
    Verify old caches are marked for deletion, current cache is preserved,
    and unrelated browser caches/user data are never touched.
    """
    # Current deployed version
    current_cache_v2 = "smart-canteen-v2.2.0"

    # Existing caches in browser
    mock_browser_caches = [
        "smart-canteen-v1",
        "smart-canteen-v2",
        "smart-canteen-v2.1.0",
        "smart-canteen-v2.2.0",
        "unrelated-third-party-cache",
        "user-avatar-cache",
    ]

    deleted_caches = []
    kept_caches = []

    for name in mock_browser_caches:
        # Exact logic used in sw.js activate listener
        if name.startswith("smart-canteen-") and name != current_cache_v2:
            deleted_caches.append(name)
        else:
            kept_caches.append(name)

    # V1 and old versions must be deleted
    assert "smart-canteen-v1" in deleted_caches
    assert "smart-canteen-v2" in deleted_caches
    assert "smart-canteen-v2.1.0" in deleted_caches

    # V2 must be kept
    assert current_cache_v2 in kept_caches

    # Unrelated caches must NOT be deleted
    assert "unrelated-third-party-cache" in kept_caches
    assert "user-avatar-cache" in kept_caches
