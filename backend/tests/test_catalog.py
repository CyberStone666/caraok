from pathlib import Path

import pytest

from app.catalog import Catalog


def _write_seed(tmp_path: Path, body: str) -> Path:
    seed = tmp_path / "tracks.yaml"
    seed.write_text(body, encoding="utf-8")
    return seed


def test_yaml_seed_with_disallowed_host_starts_empty(tmp_path: Path):
    seed = _write_seed(
        tmp_path,
        """
categories:
  - id: music
    name: Music
tracks:
  - id: one
    title: One
    category_id: music
    url: https://not-allowed.example/one.mp3
""",
    )
    catalog = Catalog(tmp_path / "catalog.db", frozenset({"media.example.com"}), yaml_seed=seed)
    # The rejected seed must not poison the catalog: it starts empty and usable.
    snapshot = catalog.snapshot()
    assert snapshot.tracks == []
    assert snapshot.categories == []


def test_yaml_seed_with_disallowed_cover_host_starts_empty(tmp_path: Path):
    seed = _write_seed(
        tmp_path,
        """
categories:
  - id: music
    name: Music
tracks:
  - id: one
    title: One
    category_id: music
    url: https://media.example.com/one.mp3
    cover_url: https://images.not-allowed.example/cover.jpg
""",
    )
    catalog = Catalog(tmp_path / "catalog.db", frozenset({"media.example.com"}), yaml_seed=seed)
    assert catalog.snapshot().tracks == []


def test_replace_document_rejects_disallowed_host(tmp_path: Path):
    from app.catalog import Category, Track

    catalog = Catalog(tmp_path / "catalog.db", frozenset({"media.example.com"}))
    bad = Track(id="bad", title="Bad", category_id="music", url="https://elsewhere.example/bad.mp3")
    categories = [Category(id="music", name="Music")]
    with pytest.raises(ValueError, match="not in AUDIO_ALLOWED_HOSTS"):
        catalog.replace_document(categories, [bad])
    # The rejected write must leave the previous catalog intact.
    assert catalog.snapshot().tracks == []


def test_replace_document_roundtrip(tmp_path: Path):
    catalog = Catalog(tmp_path / "catalog.db", frozenset({"media.example.com"}))
    from app.catalog import Category, Track

    categories = [Category(id="music", name="Music")]
    tracks = [
        Track(id="one", title="One", category_id="music", url="https://media.example.com/one.mp3"),
        Track(id="two", title="Two", category_id="music", url="https://media.example.com/two.mp3", enabled=False),
    ]
    catalog.replace_document(categories, tracks)
    snapshot = catalog.snapshot()
    assert [track.id for track in snapshot.tracks] == ["one", "two"]
    # get_track honours the enabled flag.
    assert catalog.get_track("one") is not None
    assert catalog.get_track("two") is None
    assert catalog.get_category("music") is not None


def test_yaml_seed_migrates_once_and_wins_over_empty(tmp_path: Path):
    seed = _write_seed(
        tmp_path,
        """
categories:
  - id: music
    name: Music
tracks:
  - id: one
    title: One
    category_id: music
    url: https://media.example.com/one.mp3
""",
    )
    db = tmp_path / "catalog.db"
    catalog = Catalog(db, frozenset({"media.example.com"}), yaml_seed=seed)
    assert [track.id for track in catalog.snapshot().tracks] == ["one"]
    # Second open against the same DB must not duplicate the seed.
    reopened = Catalog(db, frozenset({"media.example.com"}), yaml_seed=seed)
    assert [track.id for track in reopened.snapshot().tracks] == ["one"]
