from zir.paths import config_dir, migrate_legacy


def test_legacy_config_moves_to_the_new_folder(tmp_path):
    old = tmp_path / "config" / "serverdeck"
    old.mkdir(parents=True)
    (old / "hosts.toml").write_text("[settings]\n")

    migrate_legacy()

    assert (config_dir() / "hosts.toml").read_text(encoding="utf-8") == "[settings]\n"
    assert config_dir() == tmp_path / "config" / "zir"
    assert not old.exists()


def test_existing_new_folder_is_left_alone(tmp_path):
    old = tmp_path / "config" / "serverdeck"
    old.mkdir(parents=True)
    (old / "hosts.toml").write_text("old\n")
    config_dir().mkdir(parents=True)
    (config_dir() / "hosts.toml").write_text("new\n")

    migrate_legacy()
    migrate_legacy()

    assert (config_dir() / "hosts.toml").read_text(encoding="utf-8") == "new\n"
    assert (old / "hosts.toml").read_text(encoding="utf-8") == "old\n"
