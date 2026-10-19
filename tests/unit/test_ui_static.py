import re

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_landing_page_is_served_with_strict_csp() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Lantern" in response.text and 'href="/app/"' in response.text
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"


def test_chat_app_lives_under_app_path() -> None:
    page = client.get("/app/")

    assert page.status_code == 200 and "Ask about your documents" in page.text
    assert "/tokens.css" in page.text and "<svg" not in page.text
    assert "forgot-form" in page.text and "reset-form" in page.text
    assert client.get("/app", follow_redirects=False).status_code in (301, 302, 307, 308)
    assert client.get("/app/app.js").status_code == 200
    assert client.get("/app/styles.css").status_code == 200


def test_landing_assets_are_served() -> None:
    assert client.get("/tokens.css").status_code == 200
    assert client.get("/landing.css").status_code == 200
    assert client.get("/landing.js").status_code == 200


def test_api_routes_take_precedence_over_static_mount() -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/documents").status_code == 401


def test_ui_never_writes_markup_from_data() -> None:
    for path in ("/app/app.js", "/landing.js"):
        script = client.get(path).text
        assert "innerHTML" not in script and "insertAdjacentHTML" not in script, path


def test_landing_page_has_no_inline_styles_scripts_or_dash_characters() -> None:
    html = client.get("/").text

    assert not re.search(r"<style|\sstyle=|<script(?![^>]*\ssrc=)", html)
    for path in ("/", "/landing.css", "/landing.js"):
        assert "—" not in client.get(path).text and "–" not in client.get(path).text
