"""一键安装盘的可测部分：改 GRUB、填模板、Range 解析。

不碰 xorriso、不碰 BMC、不打网络——真正打包和安装的动作在
`infra/installer/` 的两个 CLI 里，需要的话手动跑。
"""
import importlib.util
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "infra" / "installer"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        "xiaoma_" + name, INSTALLER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_iso = _load("build_iso")
serve_http = _load("serve_http")

GRUB = """set timeout=30

menuentry "Try or Install Ubuntu Server" {
    set gfxpayload=keep
    linux  /casper/vmlinuz  --- 
    initrd /casper/initrd
}
"""

REPORT = """Drive current: -indev 'ubuntu.iso'
-append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b --interval:local_fs:5707520d-5717815d::'ubuntu.iso'
-appended_part_as_gpt
"""


def _render(**overrides):
    options = {
        "hostname": "xiaoma",
        "username": "xiaoma",
        "password_hash": "$6$abc$def",
        "ssh_keys": ["ssh-ed25519 AAAA test@host"],
        "packages": ["vim", "curl"],
    }
    options.update(overrides)
    template = (INSTALLER / "autoinstall" / "user-data.tmpl").read_text(encoding="utf-8")
    return build_iso.render_user_data(template, **options)


# ---------------------------------------------------------------- GRUB

def test_patch_grub_puts_autoinstall_before_the_separator():
    patched = build_iso.patch_grub(GRUB)
    line = [row for row in patched.splitlines() if "vmlinuz" in row][0]
    assert 'autoinstall ds="nocloud;s=/cdrom/autoinstall/"' in line
    assert line.index("autoinstall") < line.index("---")
    assert "initrd /casper/initrd" in patched


def test_patch_grub_quotes_the_datasource_value():
    """GRUB 把 `;` 当语句分隔符：ds 的值不引起来会被截成 `ds=nocloud`，
    安装器就停在交互界面（第一次真机安装踩到）。"""
    line = [row for row in build_iso.patch_grub(GRUB).splitlines() if "vmlinuz" in row][0]
    before_semicolon = line[:line.index(";")]
    assert before_semicolon.count('"') == 1, line


def test_patch_grub_is_idempotent():
    once = build_iso.patch_grub(GRUB)
    assert build_iso.patch_grub(once) == once


def test_patch_grub_refuses_an_unexpected_layout():
    with pytest.raises(ValueError):
        build_iso.patch_grub('menuentry "x" { true }\n')


# ------------------------------------------------------- El Torito 报告

def test_parse_efi_interval_reads_the_appended_partition():
    assert build_iso.parse_efi_interval(REPORT) == (5707520, 10296)


def test_parse_efi_interval_returns_none_when_efi_is_a_plain_file():
    assert build_iso.parse_efi_interval("-c /boot.catalog\n-b /boot/grub/i386-pc/eltorito.img\n") is None


# --------------------------------------------------------- autoinstall

def test_render_user_data_fills_every_placeholder():
    text = _render()
    assert "{{" not in text
    assert "hostname: xiaoma" in text
    assert 'password: "$6$abc$def"' in text
    assert 'ssh-ed25519 AAAA test@host' in text
    assert "- vim" in text and "- curl" in text


def test_render_user_data_output_is_valid_yaml():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_render())
    autoinstall = data["autoinstall"]
    assert autoinstall["identity"]["hostname"] == "xiaoma"
    assert autoinstall["identity"]["username"] == "xiaoma"
    assert autoinstall["ssh"]["authorized-keys"] == ["ssh-ed25519 AAAA test@host"]
    assert autoinstall["storage"]["layout"]["name"] == "direct"
    assert autoinstall["packages"] == ["vim", "curl"]


def test_network_is_explicit_so_netplan_apply_does_not_fail():
    """autoinstall 不写 network 段时，subiquity 会拿 live 环境的配置去套目标机，
    `netplan apply` 报 1，安装整个中断（真机踩到：停在「Press enter to start a shell」）。
    显式给一段 DHCP 才稳。"""
    yaml = pytest.importorskip("yaml")
    network = yaml.safe_load(_render())["autoinstall"]["network"]
    assert network["version"] == 2
    ethernet = list(network["ethernets"].values())[0]
    assert ethernet["dhcp4"] is True
    assert ethernet["match"]["name"] == "e*"


def test_render_user_data_without_keys_says_so_explicitly():
    text = _render(ssh_keys=[])
    assert "authorized-keys: []" in text


def test_render_user_data_without_packages_drops_the_key():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_render(packages=[]))
    assert "packages" not in data["autoinstall"]


def test_render_user_data_fails_on_a_stale_placeholder():
    with pytest.raises(ValueError):
        build_iso.render_user_data("autoinstall:\n  x: {{NOPE}}\n",
                                   hostname="h", username="u", password_hash="p",
                                   ssh_keys=[])


# ------------------------------------------------------------- Range

def test_parse_range_whole_file():
    assert serve_http.parse_range(None, 1000) == (0, 999, False)


def test_parse_range_reads_the_requested_window():
    assert serve_http.parse_range("bytes=0-99", 1000) == (0, 99, True)
    assert serve_http.parse_range("bytes=900-", 1000) == (900, 999, True)
    assert serve_http.parse_range("bytes=-100", 1000) == (900, 999, True)


def test_parse_range_clamps_the_tail():
    assert serve_http.parse_range("bytes=990-5000", 1000) == (990, 999, True)


def test_parse_range_rejects_out_of_bounds():
    with pytest.raises(ValueError):
        serve_http.parse_range("bytes=1000-", 1000)


def test_random_password_shape():
    first = build_iso.random_password()
    assert len(first) == 20
    assert first != build_iso.random_password()
    assert all(char.isalnum() or char in "-_" for char in first)
