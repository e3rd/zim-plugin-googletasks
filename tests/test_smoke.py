def test_module_imports_with_stubbed_zim(gt):
    assert gt.TASK_ANCHOR_SYMBOL == "✋"
    assert hasattr(gt, "GoogletasksController")
    assert hasattr(gt, "GoogletasksPlugin")
    assert hasattr(gt, "Cache")


def test_client_secret_from_env(gt, tmp_path):
    assert gt.CLIENT_SECRET_FILE == str(tmp_path / "client_secret.json")


def test_client_secret_from_config_dir(gt, tmp_path, monkeypatch):
    monkeypatch.delenv(gt.CLIENT_SECRET_ENV)
    assert gt.find_client_secret_file() == gt.BUNDLED_CLIENT_SECRET_FILE
    own = tmp_path / "config" / "zim" / gt.OWN_CLIENT_SECRET_NAME
    own.parent.mkdir(parents=True)
    own.write_text("{}")
    assert gt.find_client_secret_file() == str(own)
    monkeypatch.setenv(gt.CLIENT_SECRET_ENV, "/somewhere/else.json")
    assert gt.find_client_secret_file() == "/somewhere/else.json"
