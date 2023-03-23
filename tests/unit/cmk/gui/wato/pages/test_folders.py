#!/usr/bin/env python3
# Copyright (C) 2021 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
import flask
import pytest

from tests.unit.cmk.gui.conftest import WebTestAppForCMK

from cmk.utils.livestatus_helpers.testing import MockLiveStatusConnection

from cmk.gui.http import Request


@pytest.mark.usefixtures("suppress_license_expiry_header")
def test_ajax_call(logged_in_wsgi_app: WebTestAppForCMK) -> None:
    ajax_page = "/NO_SITE/check_mk/ajax_popup_move_to_folder.py"
    app = logged_in_wsgi_app
    resp = app.get(
        f"{ajax_page}?ident=test2&what=folder&_ajaxid=1611222306&back_url=wato.py", status=400
    )
    assert "Move this folder to" in resp.text, resp.text
    assert "No Setup folder test2." in resp.text, resp.text

    resp = app.get(f"{ajax_page}?ident=test2&what=folder&back_url=wato.py", status=400)
    assert "Move this folder to" in resp.text, resp.text
    assert "No Setup folder test2." in resp.text, resp.text

    app.get(f"{ajax_page}/{ajax_page}?ident=test2&what=folder&back_url=wato.py", status=404)


@pytest.mark.usefixtures("suppress_license_expiry_header")
def test_ajax_call_2(
    flask_app: flask.Flask,
    mock_livestatus: MockLiveStatusConnection,
    auth_request: Request,
) -> None:
    ajax_page = "/NO_SITE/check_mk/ajax_popup_move_to_folder.py"
    with flask_app.test_client() as client:
        client.get(auth_request)  # to get the cookie

        resp = client.get(f"{ajax_page}/{ajax_page}?ident=test2&what=folder&back_url=wato.py")
        assert resp.status_code == 404, resp.location
