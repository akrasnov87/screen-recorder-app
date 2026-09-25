import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_config(tmp_path):
    from src.config_manager import ConfigManager
    return ConfigManager(str(tmp_path / "config.json"))


@pytest.fixture
def tmp_queue(tmp_path):
    from src.task_queue import TaskQueue
    return TaskQueue(str(tmp_path / "queue.json"))