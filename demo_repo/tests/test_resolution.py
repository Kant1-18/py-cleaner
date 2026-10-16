from types import SimpleNamespace

import pytest

from catalog.sync import normalize_name, resolve_supplier

SUPPLIERS = [SimpleNamespace(name="Café Léon"), SimpleNamespace(name="Brasserie du Nord")]


def test_normalize_name_strips_accents_and_punctuation():
    assert normalize_name("  Café-Léon !") == "cafe leon"


def test_resolve_supplier_tolerates_small_differences():
    assert resolve_supplier("cafe leon", SUPPLIERS).name == "Café Léon"


def test_resolve_supplier_raises_when_unknown():
    with pytest.raises(LookupError):
        resolve_supplier("Fromagerie Martin", SUPPLIERS)
