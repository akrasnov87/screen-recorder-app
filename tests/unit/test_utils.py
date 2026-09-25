from src.utils import format_file_size, generate_task_id, parse_date_from_filename


class TestUtils:
    def test_format_file_size(self):
        assert "B" in format_file_size(100)
        assert "KB" in format_file_size(2048)
        assert "MB" in format_file_size(5 * 1024 * 1024)

    def test_generate_task_id(self):
        a = generate_task_id()
        b = generate_task_id()
        assert a != b
        assert len(a) > 10

    def test_parse_date_from_filename(self):
        result = parse_date_from_filename("2026-08-21_15-30-00_video.mp4")
        assert result is not None
        assert result.year == 2026
        assert result.month == 8