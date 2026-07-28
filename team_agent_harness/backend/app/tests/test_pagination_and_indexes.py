from fastapi.testclient import TestClient

from app.core.models import Run, Task
from app.core.storage import SQLiteStorage
from app.main import create_app


def test_task_and_run_lists_are_bounded_and_page_newest_first(tmp_path) -> None:
    app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )

    with TestClient(app) as client:
        tasks = [
            client.post(
                "/tasks",
                json={"title": f"Task {index}", "goal": "Test pagination", "workflow_pack": "research"},
            ).json()
            for index in range(3)
        ]
        runs = [app.state.harness.storage.create_run(Run(task_id=task["id"])) for task in tasks]

        first_task_page = client.get("/tasks?limit=2").json()
        second_task_page = client.get("/tasks?limit=2&offset=1").json()
        first_run_page = client.get("/runs?limit=2").json()

        assert [task["id"] for task in first_task_page] == [tasks[2]["id"], tasks[1]["id"]]
        assert [task["id"] for task in second_task_page] == [tasks[1]["id"], tasks[0]["id"]]
        assert [run["id"] for run in first_run_page] == [runs[2].id, runs[1].id]
        assert client.get("/tasks?limit=0").status_code == 422
        assert client.get("/runs?limit=1001").status_code == 422


def test_storage_keeps_unbounded_internal_list_behavior(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "harness.sqlite3") as storage:
        storage.init_schema()
        tasks = [
            storage.create_task(Task(title=f"Task {index}", goal="Goal", workflow_pack="research"))
            for index in range(3)
        ]

        assert storage.list_tasks() == tasks
        assert storage.list_tasks(limit=2) == [tasks[2], tasks[1]]


def test_schema_has_run_lookup_indexes(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "harness.sqlite3") as storage:
        storage.init_schema()
        expected = {
            "agent_runs": "idx_agent_runs_run_id",
            "artifacts": "idx_artifacts_run_id",
            "eval_results": "idx_eval_results_run_id",
            "handoffs": "idx_handoffs_run_id",
            "trace_events": "idx_trace_events_run_id",
        }
        for table, index_name in expected.items():
            index_rows = storage.conn.execute(f"PRAGMA index_list('{table}')").fetchall()
            assert index_name in {row["name"] for row in index_rows}
