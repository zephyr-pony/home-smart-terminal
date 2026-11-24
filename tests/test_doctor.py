"""启动前自检：只测判定逻辑，不真的起服务、不发网络请求。"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

DOCTOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "doctor.py"


def _load():
    spec = importlib.util.spec_from_file_location("xiaoma_doctor", DOCTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


doctor = _load()


def test_missing_llm_keys_are_errors():
    report = doctor.Report()
    doctor.check_llm(report, "", "sk-x", "")

    assert report.failed
    detail = " ".join(item["detail"] for item in report.items)
    assert "LLM_BASE_URL" in detail and "LLM_MODEL" in detail


def test_exposure_blocks_lan_without_token():
    report = doctor.Report()
    doctor.check_exposure(report, "0.0.0.0", "")
    assert report.failed

    ok = doctor.Report()
    doctor.check_exposure(ok, "127.0.0.1", "")
    doctor.check_exposure(ok, "0.0.0.0", "secret")
    assert not ok.failed


def test_data_dir_unwritable_is_an_error(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    report = doctor.Report()
    doctor.check_data_dir(report, str(blocker / "data"))

    assert report.failed


def test_optional_modules_must_be_self_consistent():
    report = doctor.Report()
    doctor.check_optional(
        report,
        push_enabled=True, vapid_private="",
        home_token="", home_devices="light.keting",
        mcp_token="", stt_enabled=False,
    )

    titles = " ".join(item["title"] for item in report.items if item["level"] == "error")
    assert "Web Push" in titles
    assert "白名单" in titles


def test_stt_enabled_without_package_is_an_error(monkeypatch):
    """Docker 镜像默认没装 funasr；.env 里留着 STT_ENABLED=1 要提前拦住。"""
    monkeypatch.setattr(doctor, "_has_module", lambda name: False)

    report = doctor.Report()
    doctor.check_optional(
        report,
        push_enabled=False, vapid_private="",
        home_token="", home_devices="",
        mcp_token="", stt_enabled=True,
    )

    assert report.failed
    detail = " ".join(item["detail"] for item in report.items)
    assert "requirements-stt" in detail


def test_run_with_minimal_config_is_ok(tmp_path, monkeypatch):
    data = tmp_path / "data"
    fake = SimpleNamespace(
        LLM_BASE_URL="http://127.0.0.1:8081/v1",
        LLM_API_KEY="sk-test",
        LLM_MODEL="test-model",
        SERVER_HOST="127.0.0.1",
        SERVER_PORT=0,          # 0 不会真的连上谁
        API_TOKEN="",
        DATA_DIR=str(data),
        MEMORY_DB_PATH=str(data / "memory.db"),
        ITEMS_DB_PATH=str(data / "items.db"),
        AGENT_DB_PATH=str(data / "agent.db"),
        TASKS_DB_PATH=str(data / "tasks.db"),
        FACTS_DB_PATH=str(data / "facts.db"),
        PUSH_DB_PATH=str(data / "push.db"),
        PUSH_ENABLED=False,
        VAPID_PRIVATE_KEY="",
        HOME_TOKEN="",
        HOME_DEVICES="",
        MCP_TOKEN="",
        STT_ENABLED=False,
    )

    report = doctor.run(fake)

    assert not report.failed
    assert os.path.isdir(str(data))
    text = doctor.format_text(report)
    assert "结果：通过" in text


def test_text_report_counts_errors():
    report = doctor.Report()
    report.error("坏消息", "修一下")
    report.warn("提醒", "")

    text = doctor.format_text(report)
    assert "1 个错误、1 个提醒" in text
