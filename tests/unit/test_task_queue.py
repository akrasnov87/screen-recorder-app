from src.task_queue import TaskQueue


class TestTaskQueue:
    def test_add_and_get_next(self, tmp_path):
        q = TaskQueue(str(tmp_path / "q.json"))
        tid = q.add_task({"video_path": "/tmp/v.mp4"})
        task = q.get_next_task()
        assert task is not None
        assert task["task_id"] == tid
        assert task["status"] == "pending"

    def test_update_status(self, tmp_path):
        q = TaskQueue(str(tmp_path / "q.json"))
        tid = q.add_task({"video_path": "/tmp/v.mp4"})
        q.update_task_status(tid, "completed", 100)
        assert q.get_queue_status()["completed"] == 1

    def test_persistence(self, tmp_path):
        path = str(tmp_path / "q.json")
        q1 = TaskQueue(path)
        tid = q1.add_task({"video_path": "/tmp/v.mp4"})
        q1.save_queue()
        q2 = TaskQueue(path)
        assert len(q2.get_queue()) == 1
        assert q2.get_queue()[0]["task_id"] == tid

    def test_recovery_in_progress(self, tmp_path):
        path = str(tmp_path / "q.json")
        q1 = TaskQueue(path)
        tid = q1.add_task({"video_path": "/tmp/v.mp4"})
        q1.update_task_status(tid, "uploading", 80)
        q2 = TaskQueue(path)
        task = q2.get_next_task()
        assert task is not None
        assert task["status"] == "pending"