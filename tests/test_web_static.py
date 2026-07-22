from pathlib import Path


def test_runtime_log_module_matches_predictfun_interaction_pattern() -> None:
    static_dir = Path(__file__).parents[1] / "poly_mm" / "web_static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")

    assert 'data-view="logs-view"' in html
    assert 'id="logs" class="full-log" tabindex="0"' in html
    assert 'id="dashboard-logs" class="log-preview" tabindex="0"' in html
    assert "selectionIsInsideLogs" in javascript
    assert "logInteractionPaused" in javascript
    assert "点击日志外恢复" in javascript
    assert "fetch('/api/logs'" in javascript


def test_quote_settings_replace_misleading_read_only_summary() -> None:
    static_dir = Path(__file__).parents[1] / "poly_mm" / "web_static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")

    assert 'id="setup-form"' in html
    assert 'id="add-market-button"' in html
    assert 'id="market-template"' in html
    assert 'name="run_duration_enabled"' in html
    assert "当前 config.toml 中生效的只读配置" not in html
    assert "风险与刷新" not in html
    assert "fetch('/api/resolve-market'" in javascript
    assert "action('/api/setup'" in javascript
