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
