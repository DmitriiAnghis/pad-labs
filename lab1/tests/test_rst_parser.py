from preprocessing.rst_parser import RstParser, slugify

RST = """
.. _inventory/wave:

==============
Wave transfers
==============

.. |SO| replace:: sales order

A wave is created from the :guilabel:`Prepare Wave` button of a |SO|.

Configuration
=============

Go to :menuselection:`Inventory app --> Configuration --> Settings`.
See :ref:`inventory/wave` and :doc:`batch`.

.. note::
   The :guilabel:`Batch, Wave & Cluster Transfers` setting is required.

.. image:: wave/settings.png
   :alt: The settings page.

.. list-table::
   :header-rows: 1

   * - Option
     - Meaning
   * - Product
     - Split transfers by product

.. code-block:: python

   print("hello")
"""


def parse(text=RST, **kw):
    kw.setdefault("doc_titles", {"inventory/batch": "Batch picking"})
    kw.setdefault("doc_id", "inventory/wave")
    return RstParser(**kw).parse(text)


def all_text(doc):
    return "\n".join(b["text"] for s in doc.sections for b in s["blocks"])


def test_title_and_sections():
    doc = parse()
    assert doc.title == "Wave transfers"
    paths = [s["heading_path"] for s in doc.sections]
    assert ["Wave transfers", "Configuration"] in paths


def test_roles_are_resolved_to_plain_text():
    text = all_text(parse())
    assert "Prepare Wave" in text and ":guilabel:" not in text
    assert "Inventory app > Configuration > Settings" in text
    assert "sales order" in text  # substitution
    assert "Batch picking" in text  # :doc: resolved to the title of the target document
    assert "`" not in text


def test_admonition_keeps_content_with_prefix():
    blocks = [b for s in parse().sections for b in s["blocks"]]
    notes = [b for b in blocks if b["type"] == "note"]
    assert notes and "Batch, Wave & Cluster Transfers" in notes[0]["text"]


def test_images_removed_and_table_flattened():
    text = all_text(parse())
    assert "settings.png" not in text and "The settings page" not in text
    assert "Option: Product" in text or "Product; Meaning: Split transfers by product" in text or "Split transfers by product" in text


def test_code_block_kept_as_code():
    blocks = [b for s in parse().sections for b in s["blocks"]]
    assert any(b["type"] == "code" and 'print("hello")' in b["text"] for b in blocks)


def test_label_is_registered():
    doc = parse()
    assert "inventory/wave" in doc.labels


def test_slugify():
    assert slugify("Batch & wave transfers") == "batch-wave-transfers"
