import pytest

from debtap_mod.version import split_debian_version, to_pacman_name, to_pacman_version


@pytest.mark.parametrize(
    "deb, expected",
    [
        ("26.1002.51308", "26.1002.51308-1"),
        ("1.2.3-1", "1.2.3-1"),
        ("1:2.4~rc1-3ubuntu1", "1:2.4rc1-3"),
        ("2.0-0ubuntu1", "2.0-0"),
        ("1.0+dfsg-2.1", "1.0+dfsg-2.1"),
        ("3.0-beta-1-2", "3.0_beta_1-2"),
        ("1.0~1-1", "1.0.1-1"),
        ("5:1.0-1", "5:1.0-1"),
    ],
)
def test_to_pacman_version(deb, expected):
    assert str(to_pacman_version(deb)) == expected


def test_epoch_is_not_mistaken_for_version():
    # the old bash code used `cut -d:` and turned "1:2.3" into "1"
    assert to_pacman_version("1:2.3-1").pkgver == "2.3"


def test_split_without_revision():
    assert split_debian_version("2.0") == (0, "2.0", "")


@pytest.mark.parametrize("deb, expected", [("ChatGPT", "chatgpt"), ("foo_bar", "foo_bar"), ("a b", "a-b"), ("-x", "x")])
def test_to_pacman_name(deb, expected):
    assert to_pacman_name(deb) == expected
