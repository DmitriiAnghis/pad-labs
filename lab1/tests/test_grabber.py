"""Grabber change detection with a fake git source (no network)."""
from grabber.git_source import RemoteFile
from grabber.grabber import Grabber, active_documents

PREFIX = "content/applications/inventory_and_mrp/"


class FakeSource:
    def __init__(self, files: dict[str, tuple[str, str]]):
        self.files = files  # path -> (blob_sha, text)

    def sync(self):
        return "ok"

    def head_commit(self):
        return {"sha": "abc123", "date": "2026-01-01"}

    def list_files(self):
        return [RemoteFile(p, sha) for p, (sha, _) in self.files.items()]

    def read(self, path):
        if self.files[path][1] is None:
            raise OSError("broken file")
        return self.files[path][1].encode()


def run(settings, config, files):
    return Grabber(settings, config, source=FakeSource(files)).run()


def test_incremental_sync(settings, config):
    files = {
        PREFIX + "inventory/a.rst": ("sha-a", "A\n=\n\ntext a"),
        PREFIX + "inventory/b.rst": ("sha-b", "B\n=\n\ntext b"),
    }
    r1 = run(settings, config, files)
    assert sorted(r1.new) == ["inventory/a", "inventory/b"] and r1.unchanged == 0

    r2 = run(settings, config, files)
    assert r2.new == [] and r2.updated == [] and r2.unchanged == 2  # nothing downloaded again

    files[PREFIX + "inventory/a.rst"] = ("sha-a2", "A\n=\n\nnew text a")
    del files[PREFIX + "inventory/b.rst"]
    files[PREFIX + "barcode/c.rst"] = ("sha-c", "C\n=\n\ntext c")
    r3 = run(settings, config, files)
    assert r3.updated == ["inventory/a"] and r3.deleted == ["inventory/b"] and r3.new == ["barcode/c"]

    g = Grabber(settings, config, source=FakeSource(files))
    manifest = g.load_manifest()
    assert manifest["documents"]["inventory/b"]["status"] == "deleted"
    assert not g.raw_path("inventory/b").exists()
    assert g.raw_path("inventory/a").read_text() == "A\n=\n\nnew text a"
    assert manifest["documents"]["barcode/c"]["module"] == "barcode"
    assert {d["doc_id"] for d in active_documents(manifest)} == {"inventory/a", "barcode/c"}


def test_duplicates_and_broken_files(settings, config):
    files = {
        PREFIX + "inventory/a.rst": ("sha-a", "same text"),
        PREFIX + "inventory/copy.rst": ("sha-copy", "same text\n"),
        PREFIX + "inventory/broken.rst": ("sha-x", None),
    }
    r = run(settings, config, files)
    assert r.duplicates == ["inventory/copy"]
    assert len(r.errors) == 1 and r.errors[0]["doc_id"] == "inventory/broken"
    manifest = Grabber(settings, config, source=FakeSource(files)).load_manifest()
    assert manifest["documents"]["inventory/copy"]["duplicate_of"] == "inventory/a"
    assert {d["doc_id"] for d in active_documents(manifest)} == {"inventory/a"}
