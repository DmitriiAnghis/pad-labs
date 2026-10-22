import numpy as np

from vectorstore.store import FaissStore


def test_faiss_store_roundtrip_in_non_ascii_folder(tmp_path):
    # faiss.write_index fails on Windows for paths like "4_Курс": the store must handle them
    folder = tmp_path / "4_Курс" / "индекс"
    folder.mkdir(parents=True)
    vectors = np.eye(4, dtype=np.float32)
    store = FaissStore(dim=4)
    store.add(vectors)
    store.save(folder)

    loaded = FaissStore.load(folder, dim=4, index_type="flat")
    assert loaded.size == 4
    assert loaded.search(vectors[2], 1)[0][0] == 2
