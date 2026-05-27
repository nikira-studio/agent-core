"""Integration tests for the Transmission adapter manifest."""

import json
from unittest.mock import MagicMock


from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine


def make_ct(backend_json: dict) -> dict:
    return {"id": "transmission", "backend_json": json.dumps(backend_json)}


def make_cred(raw: str = "", fields: dict | None = None) -> Credential:
    return Credential(raw=raw, fields=fields or {}, reference_name="test-cref")


class TestTransmissionAdapterManifest:
    def test_transmission_manifest_loads_and_runs_list_torrents(self):
        engine = HttpEngine(
            make_ct(
                {
                    "base_url": {"from": "config", "field": "base_url"},
                    "auth": {
                        "type": "basic",
                        "apply": {
                            "target": "request_header",
                            "name": "Authorization",
                            "template": "Basic {{ cred.base64_credentials }}",
                        },
                    },
                    "session": {
                        "type": "challenge_retry",
                        "trigger": {"http_status": 409},
                        "capture": {
                            "source": "response_header",
                            "name": "X-Transmission-Session-Id",
                            "as": "session_id",
                        },
                        "apply": {
                            "target": "request_header",
                            "name": "X-Transmission-Session-Id",
                            "from": "session_id",
                        },
                        "max_retries": 1,
                    },
                    "requests": {
                        "list_torrents": {
                            "method": "POST",
                            "path": "/transmission/rpc",
                            "body": {
                                "template": {
                                    "method": "torrent-get",
                                    "arguments": {
                                        "fields": [
                                            "id",
                                            "name",
                                            "status",
                                            "percentDone",
                                            "uploadRatio",
                                        ],
                                        "ids": "{{ params.ids | default([], as=list) }}",
                                    },
                                }
                            },
                            "response": {
                                "success_when": "$.result == 'success'",
                                "extract": "$.arguments.torrents",
                            },
                        },
                        "remove_torrent": {
                            "method": "POST",
                            "path": "/transmission/rpc",
                            "body": {
                                "template": {
                                    "method": "torrent-remove",
                                    "arguments": {
                                        "ids": "{{ params.ids }}",
                                        "delete-local-data": "{{ params.delete_data | default(false, as=bool) }}",
                                    },
                                }
                            },
                            "response": {"success_when": "$.result == 'success'"},
                        },
                    },
                }
            )
        )

        calls = []
        engine._send = MagicMock(
            side_effect=lambda req, config: calls.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(
                    return_value=json.dumps(
                        {
                            "result": "success",
                            "arguments": {
                                "torrents": [
                                    {
                                        "id": 1,
                                        "name": "debian.iso",
                                        "status": 6,
                                        "percentDone": 0.95,
                                        "uploadRatio": 0.0,
                                    },
                                    {
                                        "id": 2,
                                        "name": "ubuntu.iso",
                                        "status": 6,
                                        "percentDone": 0.5,
                                        "uploadRatio": 1.2,
                                    },
                                ]
                            },
                        }
                    ).encode()
                ),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "list_torrents",
            {"ids": [1, 2]},
            Credential(raw=None, fields={"username": "admin", "password": "secret"}),
            '{"base_url": "http://localhost:9091"}',
            session=None,
        )

        assert result["success"] is True
        assert len(result["body"]) == 2
        assert result["body"][0]["name"] == "debian.iso"

        call = calls[0]
        body = json.loads(json.dumps(call["body"]))
        assert body["method"] == "torrent-get"
        assert body["arguments"]["ids"] == [1, 2]
        assert "Basic " in call["headers"]["Authorization"]

    def test_transmission_remove_torrent_destructive(self):
        engine = HttpEngine(
            make_ct(
                {
                    "base_url": {"from": "config", "field": "base_url"},
                    "auth": {"type": "basic"},
                    "session": {
                        "type": "challenge_retry",
                        "trigger": {"http_status": 409},
                        "capture": {
                            "source": "response_header",
                            "name": "X-Transmission-Session-Id",
                            "as": "session_id",
                        },
                        "apply": {
                            "target": "request_header",
                            "name": "X-Transmission-Session-Id",
                            "from": "session_id",
                        },
                        "max_retries": 1,
                    },
                    "requests": {
                        "remove_torrent": {
                            "method": "POST",
                            "path": "/transmission/rpc",
                            "body": {
                                "template": {
                                    "method": "torrent-remove",
                                    "arguments": {
                                        "ids": "{{ params.ids }}",
                                        "delete-local-data": "{{ params.delete_data | default(false, as=bool) }}",
                                    },
                                }
                            },
                            "response": {"success_when": "$.result == 'success'"},
                        },
                    },
                }
            )
        )

        calls = []
        engine._send = MagicMock(
            side_effect=lambda req, config: calls.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(return_value=json.dumps({"result": "success"}).encode()),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "remove_torrent",
            {"ids": [1], "delete_data": True},
            Credential(raw=None, fields={"username": "admin", "password": "secret"}),
            '{"base_url": "http://localhost:9091"}',
            session=None,
        )

        assert result["success"] is True

        body = calls[0]["body"]
        assert body["method"] == "torrent-remove"
        assert body["arguments"]["ids"] == [1]
        assert body["arguments"]["delete-local-data"] is True

    def test_transmission_session_challenge_409_then_success(self):
        engine = HttpEngine(
            make_ct(
                {
                    "base_url": {"from": "config", "field": "base_url"},
                    "auth": {"type": "basic"},
                    "session": {
                        "type": "challenge_retry",
                        "trigger": {"http_status": 409},
                        "capture": {
                            "source": "response_header",
                            "name": "X-Transmission-Session-Id",
                            "as": "session_id",
                        },
                        "apply": {
                            "target": "request_header",
                            "name": "X-Transmission-Session-Id",
                            "from": "session_id",
                        },
                        "max_retries": 1,
                    },
                    "requests": {
                        "list_torrents": {
                            "method": "POST",
                            "path": "/transmission/rpc",
                            "body": {
                                "template": {
                                    "method": "torrent-get",
                                    "arguments": {"fields": ["id"]},
                                }
                            },
                            "response": {
                                "success_when": "$.result == 'success'",
                                "extract": "$.arguments.torrents",
                            },
                        },
                    },
                }
            )
        )

        send_calls = []
        responses = [
            MagicMock(
                status=409,
                headers={"X-Transmission-Session-Id": "fresh-session-xyz"},
                read=MagicMock(return_value=b'{"result": "success"}'),
            ),
            MagicMock(
                status=200,
                headers={},
                read=MagicMock(
                    return_value=json.dumps(
                        {"result": "success", "arguments": {"torrents": [{"id": 1}]}}
                    ).encode()
                ),
            ),
        ]

        def fake_send(req, config):
            send_calls.append((req, config))
            return responses.pop(0)

        engine._send = fake_send
        engine._raise_on_errors = MagicMock()


        result = engine.execute(
            "list_torrents",
            {},
            Credential(raw=None, fields={"username": "admin", "password": "secret"}),
            '{"base_url": "http://localhost:9091"}',
            session=None,
        )

        assert len(send_calls) == 2
        first_req = send_calls[0][0]
        assert "X-Transmission-Session-Id" not in first_req["headers"]

        second_req = send_calls[1][0]
        assert (
            second_req["headers"].get("X-Transmission-Session-Id")
            == "fresh-session-xyz"
        )

        assert result["success"] is True
        assert result["body"] == [{"id": 1}]

    def test_transmission_add_torrent_action(self):
        engine = HttpEngine(
            make_ct(
                {
                    "base_url": {"from": "config", "field": "base_url"},
                    "auth": {"type": "basic"},
                    "requests": {
                        "add_torrent": {
                            "method": "POST",
                            "path": "/transmission/rpc",
                            "body": {
                                "template": {
                                    "method": "torrent-add",
                                    "arguments": {
                                        "filename": "{{ params.filename }}",
                                        "download-dir": "{{ params.download_dir | default('', as=str) }}",
                                    },
                                }
                            },
                            "response": {"success_when": "$.result == 'success'"},
                        },
                    },
                }
            )
        )

        calls = []
        engine._send = MagicMock(
            side_effect=lambda req, config: calls.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(return_value=json.dumps({"result": "success"}).encode()),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "add_torrent",
            {
                "filename": "http://example.com/torrent.torrent",
                "download_dir": "/tmp/downloads",
            },
            Credential(raw=None, fields={"username": "admin", "password": "secret"}),
            '{"base_url": "http://localhost:9091"}',
            session=None,
        )

        assert result["success"] is True
        body = calls[0]["body"]
        assert body["method"] == "torrent-add"
        assert body["arguments"]["filename"] == "http://example.com/torrent.torrent"
        assert body["arguments"]["download-dir"] == "/tmp/downloads"

    def test_transmission_get_session_stats(self):
        engine = HttpEngine(
            make_ct(
                {
                    "base_url": {"from": "config", "field": "base_url"},
                    "auth": {"type": "basic"},
                    "requests": {
                        "get_session_stats": {
                            "method": "POST",
                            "path": "/transmission/rpc",
                            "body": {
                                "template": {"method": "session-stats", "arguments": {}}
                            },
                            "response": {
                                "success_when": "$.result == 'success'",
                                "extract": "$.arguments",
                            },
                        },
                    },
                }
            )
        )

        calls = []
        engine._send = MagicMock(
            side_effect=lambda req, config: calls.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(
                    return_value=json.dumps(
                        {
                            "result": "success",
                            "arguments": {"torrentCount": 5, "downloadSpeed": 1024},
                        }
                    ).encode()
                ),
            )
        )
        engine._raise_on_errors = MagicMock()

        result = engine.execute(
            "get_session_stats",
            {},
            Credential(raw=None, fields={"username": "admin", "password": "secret"}),
            '{"base_url": "http://localhost:9091"}',
            session=None,
        )

        assert result["success"] is True
        assert result["body"]["torrentCount"] == 5
        assert result["body"]["downloadSpeed"] == 1024

    def test_transmission_test_connection_uses_health_action(self):
        engine = HttpEngine(
            make_ct(
                {
                    "base_url": {"from": "config", "field": "base_url"},
                    "auth": {"type": "basic"},
                    "session": {
                        "type": "challenge_retry",
                        "trigger": {"http_status": 409},
                        "capture": {
                            "source": "response_header",
                            "name": "X-Transmission-Session-Id",
                            "as": "session_id",
                        },
                        "apply": {
                            "target": "request_header",
                            "name": "X-Transmission-Session-Id",
                            "from": "session_id",
                        },
                        "max_retries": 1,
                    },
                    "requests": {
                        "get_session_stats": {
                            "method": "POST",
                            "path": "/transmission/rpc",
                            "body": {
                                "template": {"method": "session-stats", "arguments": {}}
                            },
                            "response": {
                                "success_when": "$.result == 'success'",
                                "extract": "$.arguments",
                            },
                        },
                    },
                }
            )
        )

        calls = []
        engine._raise_on_errors = MagicMock()

        responses = [
            MagicMock(
                status=409,
                headers={"X-Transmission-Session-Id": "abc123"},
                read=MagicMock(
                    return_value=json.dumps({"result": "session-id-required"}).encode()
                ),
            ),
            MagicMock(
                status=200,
                headers={},
                read=MagicMock(
                    return_value=json.dumps(
                        {
                            "result": "success",
                            "arguments": {"torrentCount": 0, "downloadSpeed": 0},
                        }
                    ).encode()
                ),
            ),
        ]

        def fake_send(req, config):
            calls.append(req)
            return responses[len(calls) - 1]

        engine._send = MagicMock(side_effect=fake_send)

        result = engine.test_connection(
            Credential(raw=None, fields={"username": "admin", "password": "secret"}),
            '{"base_url": "http://localhost:9091"}',
        )

        assert result["success"] is True
        assert result["status"] == 200
        assert calls[0]["headers"]["Authorization"].startswith("Basic ")
        assert calls[1]["headers"]["X-Transmission-Session-Id"] == "abc123"


class TestTransmissionSessionRefresh:
    def test_refresh_session_captures_session_id(self):
        engine = HttpEngine(
            make_ct(
                {
                    "base_url": {"from": "config", "field": "base_url"},
                    "auth": {"type": "basic"},
                    "session": {
                        "type": "challenge_retry",
                        "trigger": {"http_status": 409},
                        "capture": {
                            "source": "response_header",
                            "name": "X-Transmission-Session-Id",
                            "as": "session_id",
                        },
                        "apply": {
                            "target": "request_header",
                            "name": "X-Transmission-Session-Id",
                            "from": "session_id",
                        },
                        "max_retries": 1,
                    },
                    "requests": {
                        "list_torrents": {
                            "method": "POST",
                            "path": "/transmission/rpc",
                            "body": {
                                "template": {"method": "torrent-get", "arguments": {}}
                            },
                            "response": {"success_when": "$.result == 'success'"},
                        },
                    },
                }
            )
        )

        engine._send = MagicMock(
            return_value=MagicMock(
                status=200,
                headers={"X-Transmission-Session-Id": "refreshed-session-abc"},
                read=MagicMock(return_value=b'{"result": "success"}'),
            )
        )

        result = engine.refresh_session(
            Credential(raw=None, fields={"username": "admin", "password": "secret"}),
            '{"base_url": "http://localhost:9091"}',
            None,
        )

        assert result["session"]["session_id"] == "refreshed-session-abc"
