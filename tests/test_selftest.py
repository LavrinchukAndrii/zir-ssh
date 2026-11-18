"""ZIR_SELF_TEST=1: the check the build runs on every binary."""

from zir import selftest


def test_self_test_starts_the_ui_and_the_files_section(capsys):
    assert selftest.run() == 0
    assert "self test ok" in capsys.readouterr().out
