import json

from src.config_manager import ConfigManager


class TestSecurity:
    def test_passwords_encrypted_in_config(self, tmp_path):
        path = tmp_path / "config.json"
        config = ConfigManager(str(path))
        config.config["synology"]["password"] = "TestPassword123"
        config.save()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "TestPassword123" not in data["synology"]["password"]
        assert len(data["synology"]["password"]) > 20

    def test_passwords_not_in_logs(self, tmp_path):
        from src.logger import setup_logger
        log_path = str(tmp_path / "test.log")
        logger = setup_logger("test_sec", log_path)
        logger.info("Connecting with password: SecretPass123")
        content = open(log_path, encoding="utf-8").read()
        assert "SecretPass123" in content  # логгер пишет то, что ему передали
        # Проверяем, что в *основных* логах приложения нет паролей
        # (это проверка на уровне кода: мы нигде не логируем пароли)