from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_vps_service_keeps_editable_config_in_state_directory() -> None:
    service = (ROOT / "deploy" / "polymarket-mm-bot.service").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "deploy" / "install-vps.sh").read_text(encoding="utf-8")

    state_config = "/var/lib/polymarket-mm-bot/config.toml"
    assert f"--config {state_config}" in service
    assert 'STATE_DIR="${POLYMM_STATE_DIR:-/var/lib/polymarket-mm-bot}"' in installer
    assert '"${INSTALL_DIR}/config.toml" "${STATE_DIR}/config.toml"' in installer
    assert 'chown "${SERVICE_USER}":"${SERVICE_USER}" "${STATE_DIR}/config.toml"' in installer
