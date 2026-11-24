"""同义词表：只收真正能互换的说法，别把「水费/水表」这类相关词混进来。"""
from agent.synonyms import _GROUPS, canonicalize, variants


def test_variants_are_symmetric():
    assert set(variants("户号")) == {"户号", "账号"}
    assert set(variants("账号")) == {"户号", "账号"}
    assert set(variants("煤气")) == {"燃气", "煤气", "天然气"}


def test_unknown_word_returns_itself():
    assert variants("牛奶") == ["牛奶"]
    assert variants("") == []


def test_canonicalize_replaces_synonyms():
    assert canonicalize("水费账号") == "水费户号"
    assert canonicalize("煤气费") == "燃气费"
    assert canonicalize("车牌号是京A") == "车牌是京A"
    assert canonicalize("牛奶") == "牛奶"


def test_no_single_char_groups():
    """单字替换会把「号」「费」这种到处都是的字放开，宁可不做。"""
    assert all(len(word) >= 2 for group in _GROUPS for word in group)
