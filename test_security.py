"""Credential destination checks exercise the real transport with no network."""
import base64
import os
from unittest.mock import MagicMock, patch

import urd
import webapp


def test_setup_prefills_the_trusted_hostname():
    with patch.dict(os.environ, {"URD_JIRA_HOST": "trusted.invalid"}):
        response = webapp.create_app(None).test_client().get("/setup")
    assert 'name="site" value="trusted.invalid"' in response.get_data(as_text=True)


def test_setup_cannot_send_the_process_token_to_another_host():
    # Removing the final transport check would send this dummy token to the
    # submitted host even though the attacker never knew the token itself.
    app = webapp.create_app(None)
    with patch.dict(os.environ, {"URD_JIRA_HOST": "trusted.invalid", "URD_TOKEN": "dummy-token"}), \
            patch.object(urd._OPENER, "open") as opened:
        response = app.test_client().post(
            "/setup", headers={"Host": "localhost", "Sec-Fetch-Site": "same-origin"},
            environ_overrides={"REMOTE_ADDR": "203.0.113.4"},
            data={"site": "collector.invalid", "email": "person@example.invalid",
                  "project": "DEMO", "since": "2026-01-01"})
    assert not opened.called, "setup sent the process credential to an untrusted host"
    assert response.status_code == 200
    assert "URD_JIRA_HOST" in response.get_data(as_text=True)


def test_setup_reports_malformed_sites_without_a_server_error():
    with patch.dict(os.environ, {"URD_JIRA_HOST": "trusted.invalid", "URD_TOKEN": "dummy-token"}), \
            patch.object(urd._OPENER, "open") as opened:
        response = webapp.create_app(None).test_client().post(
            "/setup", headers={"Sec-Fetch-Site": "same-origin"},
            data={"site": "trusted.invalid/path", "email": "person@example.invalid",
                  "project": "DEMO", "since": "2026-01-01"})
    assert response.status_code == 200, "invalid setup input must remain an inline form error"
    assert "bare trusted hostname" in response.get_data(as_text=True)
    assert not opened.called


def test_live_requests_require_a_valid_independent_trusted_host():
    bad_sites = ("", "https://trusted.invalid", "trusted.invalid:443",
                 "trusted.invalid/path", "trusted.invalid@collector.invalid",
                 "trusted.invalid?query", "trusted.invalid#fragment",
                 "trusted.invalid\n", "trusted..invalid")
    for trusted in bad_sites:
        with patch.dict(os.environ, {"URD_JIRA_HOST": trusted}), \
                patch.object(urd._OPENER, "open") as opened:
            try:
                urd.Jira("trusted.invalid", "person@example.invalid", "dummy-token").get("/myself")
            except SystemExit as exc:
                assert "URD_JIRA_HOST" in str(exc)
            else:
                raise AssertionError(f"accepted invalid trusted host {trusted!r}")
            assert not opened.called


def test_site_syntax_cannot_change_the_credential_destination():
    sites = ("trusted.invalid@collector.invalid", "trusted.invalid/path",
             "trusted.invalid:443", "trusted.invalid?query", "trusted.invalid#fragment",
             "trusted.invalid\n", "https://trusted.invalid", "trusted..invalid")
    with patch.dict(os.environ, {"URD_JIRA_HOST": "trusted.invalid"}), \
            patch.object(urd._OPENER, "open") as opened:
        for site in sites:
            try:
                urd.Jira(site, "person@example.invalid", "dummy-token").get("/myself")
            except SystemExit:
                pass
            else:
                raise AssertionError(f"accepted malformed site {site!r}")
        assert not opened.called


def test_matching_hostname_sends_credentials_and_still_blocks_redirects():
    response = MagicMock()
    response.__enter__.return_value.status = 200
    response.__enter__.return_value.read.return_value = b'{"displayName":"Example"}'
    with patch.dict(os.environ, {"URD_JIRA_HOST": "TRUSTED.invalid"}), \
            patch.object(urd._OPENER, "open", return_value=response) as opened:
        result = urd.Jira("Trusted.Invalid", "person@example.invalid", "dummy-token").get("/myself")
    assert result == {"displayName": "Example"}
    request = opened.call_args.args[0]
    assert request.full_url == "https://trusted.invalid/rest/api/3/myself"
    assert request.get_method() == "GET"
    assert base64.b64decode(request.get_header("Authorization").split()[1]) == \
        b"person@example.invalid:dummy-token"
    try:
        urd._NoRedirect().redirect_request(request, None, 302, "Found", {},
                                           "https://collector.invalid/")
    except SystemExit:
        pass
    else:
        raise AssertionError("redirects must remain blocked")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
