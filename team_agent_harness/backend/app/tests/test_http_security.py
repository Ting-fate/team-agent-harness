from fastapi.testclient import TestClient

from app.main import create_app


def test_local_service_rejects_untrusted_host_header(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app) as client:
        response = client.get("/health", headers={"Host": "attacker.example:8014"})

    assert response.status_code == 400


def test_testserver_host_is_only_allowed_for_the_in_process_test_client(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app, client=("127.0.0.1", 50123)) as client:
        response = client.get("/health", headers={"Host": "testserver"})

    assert response.status_code == 400


def test_local_service_rejects_cross_origin_mutation(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            headers={"Origin": "https://attacker.example"},
            json={
                "title": "Must not be created",
                "goal": "Reject cross-origin mutations.",
                "workflow_pack": "code_rd",
            },
        )
        tasks = client.get("/tasks").json()

    assert response.status_code == 403
    assert tasks == []


def test_local_service_rejects_cross_site_fetch_metadata_without_origin(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            headers={"Sec-Fetch-Site": "cross-site"},
            json={
                "title": "Must not be created",
                "goal": "Reject cross-site browser metadata.",
                "workflow_pack": "code_rd",
            },
        )
        tasks = client.get("/tasks").json()

    assert response.status_code == 403
    assert tasks == []


def test_local_service_allows_same_origin_and_cli_mutations(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app) as client:
        same_origin = client.post(
            "/tasks",
            headers={"Origin": "http://testserver"},
            json={
                "title": "Browser task",
                "goal": "Allow the local UI origin.",
                "workflow_pack": "code_rd",
            },
        )
        cli = client.post(
            "/tasks",
            json={
                "title": "CLI task",
                "goal": "Allow local clients without browser origin headers.",
                "workflow_pack": "code_rd",
            },
        )

    assert same_origin.status_code == 201
    assert cli.status_code == 201


def test_main_ui_has_local_security_headers(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'"
    )
