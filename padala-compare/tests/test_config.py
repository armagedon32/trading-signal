from app.config import resolve_site_url


def test_site_url_explicit_wins_and_trailing_slash_is_dropped():
    env = {"SITE_URL": "https://padala.example/", "RENDER_EXTERNAL_URL": "https://x.onrender.com"}
    assert resolve_site_url(env) == "https://padala.example"


def test_site_url_detected_on_free_hosts():
    assert resolve_site_url({"RENDER_EXTERNAL_URL": "https://padala.onrender.com"}) == "https://padala.onrender.com"
    assert resolve_site_url({"RAILWAY_PUBLIC_DOMAIN": "padala.up.railway.app"}) == "https://padala.up.railway.app"
    assert resolve_site_url({"KOYEB_PUBLIC_DOMAIN": "padala-me.koyeb.app/"}) == "https://padala-me.koyeb.app"
    assert resolve_site_url({"FLY_APP_NAME": "padala"}) == "https://padala.fly.dev"


def test_site_url_defaults_to_localhost():
    assert resolve_site_url({}) == "http://localhost:8000"
    assert resolve_site_url({"SITE_URL": "   "}) == "http://localhost:8000"
