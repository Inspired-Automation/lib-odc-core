import time

import pytest

from odc_core import login_failure, password_change as pc


# --- pure signals (no browser) -----------------------------------------------

@pytest.mark.parametrize("url,hit", [
    ("https://mygateway.example/#/change-expired-password", True),
    ("https://portal.example/Account/ChangePassword", True),
    ("https://portal.example/password-expired", True),
    ("https://portal.example/user/update_password?x=1", True),
    ("https://portal.example/#/login/", False),
    ("https://portal.example/#/view/dashboard", False),
    ("https://portal.example/forgot-password", False),
    ("https://portal.example/ForgottenPassword/reset-password", False),
    ("https://portal.example/login?returnUrl=/ChangePassword", False),
    ("", False),
    (None, False),
])
def test_url_signal(url, hit):
    assert (pc.url_signal(url) is not None) is hit


def test_url_signal_supplier_pattern():
    assert pc.url_signal("https://p.example/#/security/step-7", [r"security/step-7"])
    assert pc.url_signal("https://p.example/#/security/step-7") is None


@pytest.mark.parametrize("count,desc,text,hit", [
    (3, "", "Change Password Username Old Password Password: Confirm Password: Confirm", True),
    (2, "newPassword | confirmNewPassword", "", True),
    (3, "current-password | new-password | new-password", "", True),
    (2, "", "Please choose a New Password", True),
    (1, "", "Old Password New Password", False),
    (3, "password | password | confirmPassword", "Sign in Register Confirm password", False),
    (0, "", "Change Password", False),
])
def test_form_signal(count, desc, text, hit):
    assert (pc.form_signal(count, desc, text) is not None) is hit


def test_message_signal_ignored_on_sign_in_form():
    text = "Sign in. If your password has expired please contact us."
    assert pc.message_signal(1, text) is None
    assert pc.message_signal(0, text) is not None
    assert pc.message_signal(2, text) is not None


@pytest.mark.parametrize("text", [
    "You are required to change your password every 90 days",
    "Your password has expired",
    "Your password must be changed",
    "Please set a new password to continue",
])
def test_password_change_phrase(text):
    assert login_failure.password_change_phrase(text)
    assert login_failure.classify_portal_message(text) == login_failure.LoginFailureKind.PASSWORD_CHANGE_REQUIRED


def test_detect_never_raises_on_broken_page():
    class Broken:
        @property
        def url(self):
            raise RuntimeError("closed")

        @property
        def frames(self):
            raise RuntimeError("closed")

        def evaluate(self, js):
            raise RuntimeError("closed")

        def locator(self, sel):
            raise RuntimeError("closed")

    assert pc.detect_password_change(Broken(), extra_selectors=["text=x"]) is None


# --- mock pages in a real browser ---------------------------------------------

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def page():
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:  # chromium not installed on this machine
        pytest.skip(f"chromium unavailable: {exc}")
    pg = browser.new_page()
    yield pg
    browser.close()
    pw.stop()


TGP_CHANGE = """
<h2>Change Password</h2>
<div><span>Username</span><input placeholder="Enter Username"></div>
<div><span>Old Password</span><input type="password" placeholder="Enter Password"></div>
<div><span>Password:</span><input type="password" placeholder="Enter Password"></div>
<div><span>Confirm Password:</span><input type="password" placeholder="Enter Password"></div>
<button>Confirm</button>
"""
CROWN_MESSAGE = """
<h1>Change Password</h1>
<p>You are required to change your password every 90 days.</p>
<a href="#">Continue</a>
"""
GENERIC_NEW = """
<form><label for="a">New password</label><input id="a" type="password">
<label for="b">Re-enter password</label><input id="b" type="password">
<button>Save</button></form>
"""
FRAMED = '<iframe srcdoc="' + GENERIC_NEW.replace('"', "&quot;") + '"></iframe>'
LOGGED_IN_NAV = """
<nav>DASHBOARD | SITES | USERS | <a href="#">CHANGE PASSWORD</a></nav><h1>Dashboard</h1>
"""
SIGN_IN = """
<h1>Sign In to your account</h1>
<input name="username"><input name="password" type="password">
<a href="#">Forgotten your password?</a>
<p>If your password has expired, please contact support.</p>
"""
SIGN_IN_AND_REGISTER = """
<form><input name="u"><input name="p" type="password"><button>Sign in</button></form>
<form><input name="e"><label for="r1">Password</label><input id="r1" type="password">
<label for="r2">Confirm password</label><input id="r2" type="password"><button>Register</button></form>
"""
HIDDEN_FORM = '<div style="display:none">' + GENERIC_NEW + "</div><h1>Welcome</h1>"


@pytest.mark.parametrize("html,hit", [
    (TGP_CHANGE, True),
    (CROWN_MESSAGE, True),
    (GENERIC_NEW, True),
    (FRAMED, True),
    (LOGGED_IN_NAV, False),
    (SIGN_IN, False),
    (SIGN_IN_AND_REGISTER, False),
    (HIDDEN_FORM, False),
])
def test_detect_on_mock_pages(page, html, hit):
    page.set_content(html)
    if html is FRAMED:
        page.wait_for_timeout(300)
    reason = pc.detect_password_change(page)
    assert (reason is not None) is hit, reason


def test_extra_selector(page):
    page.set_content(LOGGED_IN_NAV)
    assert pc.detect_password_change(page) is None
    assert pc.detect_password_change(page, extra_selectors=["text=CHANGE PASSWORD"])


def test_wait_catches_late_redirect(page):
    page.set_content(SIGN_IN + """<script>
      setTimeout(() => { document.body.innerHTML = %r; }, 1200);
    </script>""" % TGP_CHANGE)
    started = time.monotonic()
    reason = pc.wait_for_password_change(page, timeout_s=5)
    assert reason is not None
    assert time.monotonic() - started < 5


def test_wait_stops_when_done(page):
    page.set_content(SIGN_IN)
    started = time.monotonic()
    assert pc.wait_for_password_change(page, done=lambda: True, timeout_s=5) is None
    assert time.monotonic() - started < 1


def test_wait_times_out_on_plain_failure(page):
    page.set_content(SIGN_IN)
    assert pc.wait_for_password_change(page, timeout_s=1) is None
