import pytest
from PySide6.QtWidgets import QLineEdit

from src.config_manager import ConfigManager
from src.settings_window import SettingsWindow


@pytest.mark.gui
class TestSettingsWindow:
    def test_open_window(self, qtbot, tmp_path):
        config = ConfigManager(str(tmp_path / "c.json"))
        window = SettingsWindow(config)
        qtbot.addWidget(window)
        window.show()
        assert window.isVisible()

    def test_password_masking(self, qtbot, tmp_path):
        config = ConfigManager(str(tmp_path / "c.json"))
        window = SettingsWindow(config)
        qtbot.addWidget(window)
        window.show()
        assert window.password_input.echoMode() == QLineEdit.EchoMode.Password
        window.show_password_btn.setChecked(True)
        assert window.password_input.echoMode() == QLineEdit.EchoMode.Normal

    def test_save_settings(self, qtbot, tmp_path):
        config = ConfigManager(str(tmp_path / "c.json"))
        window = SettingsWindow(config)
        qtbot.addWidget(window)
        window.address_input.setText("192.168.1.200")
        window.username_input.setText("testuser")
        window.save_settings()
        assert config.config["synology"]["address"] == "192.168.1.200"
        assert config.config["synology"]["username"] == "testuser"