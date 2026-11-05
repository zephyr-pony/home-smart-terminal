from wake_word import extract_wake_command


def test_requires_wake_word():
    assert extract_wake_command("今天天气不错") is None
    assert extract_wake_command("") is None


def test_wake_word_alone():
    assert extract_wake_command("小马") == ""
    assert extract_wake_command("小马。") == ""


def test_command_in_same_utterance():
    assert extract_wake_command("小马，明天要交电费。") == "明天要交电费"
    assert extract_wake_command("小 马 帮我记一下") == "帮我记一下"


def test_repeated_wake_word():
    assert extract_wake_command("小马小马，打开灯") == "打开灯"


def test_custom_wake_word():
    assert extract_wake_command("Hello 小助手 开灯", wake_word="小助手") == "开灯"


def test_wake_word_at_end():
    assert extract_wake_command("你好，小马") == ""
