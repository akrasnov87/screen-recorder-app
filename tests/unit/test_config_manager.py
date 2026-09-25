from unittest.mock import patch

from src.config_manager import ConfigManager


class TestConfigManager:
    def test_load_defaults(self, tmp_path):
        config = ConfigManager(str(tmp_path / "config.json"))
        defaults = config.get_defaults()
        assert "synology" in defaults
        assert "transcribe" in defaults
        assert defaults["storage"]["retention_hours"] == 24

    def test_encrypt_decrypt_password(self, tmp_path):
        config = ConfigManager(str(tmp_path / "config.json"))
        original = "MySecretPassword123"
        encrypted = config.encrypt_password(original)
        decrypted = config.decrypt_password(encrypted)
        assert decrypted == original
        assert encrypted != original

    @patch("keyring.get_password", return_value="keyring_password")
    @patch("keyring.set_password")
    def test_keyring_integration(self, mock_set, mock_get, tmp_path):
        config = ConfigManager(str(tmp_path / "config.json"))
        config.set_keyring_password("test_service", "test_user", "test_pass")
        mock_set.assert_called_once()
        result = config.get_keyring_password("test_service", "test_user")
        assert result == "keyring_password"

    def test_save_and_load(self, tmp_path):
        config = ConfigManager(str(tmp_path / "config.json"))
        config.config["synology"]["address"] = "10.0.0.1:5001"
        config.save()
        config2 = ConfigManager(str(tmp_path / "config.json"))
        assert config2.config["synology"]["address"] == "10.0.0.1:5001"