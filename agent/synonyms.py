"""检索里的同义词：存的是「户号」，问的是「账号」。

只影响**查询**，不改写下内容：用户说「账号」就照原话存，库里仍是「账号」。
检索时把两边（库里的文本和查询）换成同一个规范词，所以两个方向都能命中。

为什么不做「全文同义改写」：把库里的内容也改写会让展示出来的是别的词，
用户会以为记错了；而且家庭场景真正需要互换的词很少，宁缺毋滥——放得太松
（比如把「水费」和「水表」算同义）会把不该命中的行捞出来。

规则：
- 每组里的词在家庭场景下指的是同一个东西
- 只做整词替换，不做单字替换（单字会把「号」这种到处都有的字放开）
- 组里第一个是规范词，只用于检索匹配
"""

# (规范词, 同义说法…)
_GROUPS = (
    ("户号", "账号"),                 # 水费/电费/燃气的缴费户号
    ("燃气", "煤气", "天然气"),
    ("宽带", "网费", "宽带费"),
    ("车牌", "车牌号"),
    ("身份证", "身份证号"),
    ("手机号", "手机号码", "电话号码"),
    ("物业", "物业费"),
    ("水费", "自来水费"),
)

# 词 -> 组；同组互相命中
_VARIANTS = {}
for _group in _GROUPS:
    for _word in _group:
        _VARIANTS[_word] = _group

# 非规范词 -> 规范词；长词先换，避免「车牌号」被「车牌」截断
_REPLACEMENTS = sorted(
    ((word, group[0]) for group in _GROUPS for word in group[1:]),
    key=lambda item: len(item[0]),
    reverse=True,
)


def variants(term):
    """一个检索词的同义说法（含自身，保序）。没有同义词时原样返回。"""
    term = str(term or "")
    if not term:
        return []
    group = _VARIANTS.get(term)
    return list(group) if group else [term]


def canonicalize(text):
    """把文本里的同义说法换成规范词；用于查询串和库里的检索文本。"""
    text = str(text or "")
    for word, head in _REPLACEMENTS:
        if word in text:
            text = text.replace(word, head)
    return text
