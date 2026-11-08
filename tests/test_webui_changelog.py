"""更新日志数据：版本顺序、去重与渲染标注。"""

from tg_signer.webui.changelog import RELEASES, releases_markdown


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_releases_sorted_descending_without_duplicates():
    versions = [note.version for note in RELEASES]
    assert versions == sorted(versions, key=_version_key, reverse=True)
    assert len(versions) == len(set(versions))


def test_every_release_has_non_empty_items():
    for note in RELEASES:
        assert note.items, f"v{note.version} 缺少变更条目"
        assert all(item.strip() for item in note.items)


def test_markdown_marks_current_version_only():
    current = RELEASES[0].version
    text = releases_markdown(current)
    assert f"### v{current}（当前版本）" in text
    assert text.count("（当前版本）") == 1


def test_markdown_lists_every_item():
    text = releases_markdown("9.9.9")
    for note in RELEASES:
        assert f"### v{note.version}" in text
        for item in note.items:
            assert f"- {item}" in text
