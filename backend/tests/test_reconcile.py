from __future__ import annotations

from uuid import uuid4

from app.core.reconcile import reconcile
from tests.fakes import FakeLoader, FakeSource


def _loader_with(mid, keys):
    loader = FakeLoader(mid)
    for k in keys:
        loader.docs[("dest._default.items", k)] = {"v": 1, "_migration": {"migration_id": str(mid)}}
    return loader


async def test_removes_documents_deleted_at_source():
    mid = uuid4()
    source = FakeSource(captures_deletes=False)
    for k in ("a", "b", "c"):
        source.write("items", k, {"v": 1})
    loader = _loader_with(mid, ["a", "b", "c", "gone"])
    outcome = await reconcile(source, loader, ["items"])
    assert outcome.deleted == 1
    assert ("dest._default.items", "gone") not in loader.docs
    assert ("dest._default.items", "a") in loader.docs


async def test_empty_source_listing_never_deletes():
    mid = uuid4()
    loader = _loader_with(mid, ["a", "b"])
    outcome = await reconcile(FakeSource(), loader, ["items"])
    assert outcome.deleted == 0
    assert len(loader.docs) == 2
    assert any("safety" in n for n in outcome.notes)


async def test_large_deletion_is_held_until_forced():
    mid = uuid4()
    source = FakeSource()
    source.write("items", "k0", {"v": 1})
    loader = _loader_with(mid, [f"k{i}" for i in range(500)])
    outcome = await reconcile(source, loader, ["items"])
    assert outcome.deleted == 0 and outcome.held_for_confirmation == {"items": 499}
    forced = await reconcile(source, loader, ["items"], force=True)
    assert forced.deleted == 499
