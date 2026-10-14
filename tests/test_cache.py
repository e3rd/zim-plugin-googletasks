import json

import pytest


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "googletasks.cache"


def test_roundtrip(gt, cache_path):
    cache = gt.Cache(cache_path)
    cache.items_ids = {"b", "a"}
    cache.lists = {"Work": "id1"}
    cache.icons = {"Work": "💼"}
    cache.touch_save()
    data = json.loads(cache_path.read_text())
    assert data["version"] == gt.CACHE_SCHEMA_VERSION
    assert data["items_ids"] == ["a", "b"]

    loaded = gt.Cache(cache_path).load()
    assert loaded.items_ids == {"a", "b"}
    assert loaded.lists == {"Work": "id1"}
    assert loaded.last_fetched == cache.last_fetched
    assert loaded.icons == {"Work": "💼"}


def test_old_jsonpickle_format_is_migrated_and_resaved(gt, cache_path):
    cache_path.write_text(json.dumps(
        {"items_ids": {"py/set": ["a", "b"]}, "lists": {"Work": "id1"}, "last_fetched": 1234.5}))
    cache = gt.Cache(cache_path).load()
    assert cache.items_ids == {"a", "b"}
    assert cache.lists == {"Work": "id1"}
    assert cache.last_fetched == 1234.5
    assert json.loads(cache_path.read_text())["version"] == gt.CACHE_SCHEMA_VERSION
    assert gt.Cache(cache_path).load().items_ids == {"a", "b"}  # second load reads the new format


def test_oldest_format_with_date_at(gt, cache_path):
    cache_path.write_text(json.dumps({"items_ids": {"py/set": ["a"]}, "date_at": 99}))
    assert gt.Cache(cache_path).load().last_fetched == 99


@pytest.mark.parametrize("content", ["", "garbage", "[1, 2]", '"str"', "{}", '{"version": 999, "items_ids": ["a"]}',
                                     '{"version": 1, "items_ids": 5}', '{"items_ids": [], "lists": []}'])
def test_unknown_or_corrupt_gives_empty_cache(gt, cache_path, content):
    cache_path.write_text(content)
    cache = gt.Cache(cache_path).load()
    assert cache.items_ids == set()
    assert cache.lists == {}
    assert cache.last_fetched == 0


def test_missing_file(gt, cache_path):
    cache = gt.Cache(cache_path).load()
    assert not cache.exists()
    assert cache.items_ids == set()


def test_reloads_when_another_process_rewrites_the_file(gt, cache_path):
    first = gt.Cache(cache_path)
    first.items_ids = {"a"}
    first.save()
    reader = gt.Cache(cache_path).load()
    other = gt.Cache(cache_path)
    other.items_ids = {"a", "b"}
    other.save()
    import os
    os.utime(cache_path, (1, 1))  # make the mtime differ even on coarse filesystems
    assert reader.load().items_ids == {"a", "b"}
