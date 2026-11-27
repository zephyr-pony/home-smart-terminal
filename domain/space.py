"""空间（space）隔离的地基（D68）。

为什么按「一套库一个目录」而不是给每张表加 `space_id` 列：
- 家里就几个人，数据量小，每个空间一套 SQLite 文件最省事；
- 现有约一百条 SQL 不用逐条加 `WHERE space_id=?`，漏一处就是串数据，
  而漏一处还看不出来；
- 备份、导出、删掉一个空间都是整目录操作；
- 默认空间继续用现有路径（`DATA_DIR` 下的库），升级无感。

以后接多成员/多设备，只要在请求入口按人/设备换 `space_id`，
再按这里的路径组合建一套 store；上层编排不用改。

注意：`space_id` 只决定「哪套数据」，不参与鉴权；谁能用这个空间由入口层决定。
"""
import os
import re

from config import (
    AGENT_DB_PATH,
    DATA_DIR,
    DIALOGUE_DB_PATH,
    FACTS_DB_PATH,
    ITEMS_DB_PATH,
    MEMORY_DB_PATH,
    PRINT_FILES_DIR,
    PRINTS_DB_PATH,
    PUSH_DB_PATH,
    TASKS_DB_PATH,
)

DEFAULT_SPACE_ID = "default"
MAX_SPACE_ID_CHARS = 32
_SPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def normalize_space_id(value):
    """空 = 默认空间；只允许小写字母/数字/下划线/短横线，最长 32。

    为什么这么严：这个值将来会出现在目录名里，直接拼路径必须挡住 `..`、分隔符
    和大小写混用（Linux 上 `Family` 和 `family` 是两个目录，排查起来很费劲）。
    """
    text = str(value or "").strip().lower()
    if not text:
        return DEFAULT_SPACE_ID
    if len(text) > MAX_SPACE_ID_CHARS or not _SPACE_RE.match(text):
        raise ValueError(
            "space_id 只能是小写字母、数字、下划线或短横线（最长 %d 位）"
            % MAX_SPACE_ID_CHARS
        )
    return text


def space_dir(space_id=DEFAULT_SPACE_ID, data_dir=None):
    space_id = normalize_space_id(space_id)
    root = DATA_DIR if data_dir is None else data_dir
    if space_id == DEFAULT_SPACE_ID:
        return root
    return os.path.join(root, "spaces", space_id)


def space_paths(space_id=DEFAULT_SPACE_ID, data_dir=None):
    """返回一个空间的全部库/目录路径。

    默认空间在没传 `data_dir` 时用配置里的路径（尊重各 `*_DB_PATH` 覆盖）；
    其它空间一律落在 `<DATA_DIR>/spaces/<space_id>/` 下，互不共享文件。
    """
    space_id = normalize_space_id(space_id)
    base = space_dir(space_id, data_dir=data_dir)
    if space_id == DEFAULT_SPACE_ID and data_dir is None:
        return {
            "space_id": space_id,
            "dir": base,
            "memory": MEMORY_DB_PATH,
            "items": ITEMS_DB_PATH,
            "tasks": TASKS_DB_PATH,
            "facts": FACTS_DB_PATH,
            "agent": AGENT_DB_PATH,
            "prints": PRINTS_DB_PATH,
            "push": PUSH_DB_PATH,
            "dialogue": DIALOGUE_DB_PATH,
            "print_files": PRINT_FILES_DIR,
        }
    return {
        "space_id": space_id,
        "dir": base,
        "memory": os.path.join(base, "memory.db"),
        "items": os.path.join(base, "items.db"),
        "tasks": os.path.join(base, "tasks.db"),
        "facts": os.path.join(base, "facts.db"),
        "agent": os.path.join(base, "agent.db"),
        "prints": os.path.join(base, "prints.db"),
        "push": os.path.join(base, "push.db"),
        "dialogue": os.path.join(base, "dialogue.db"),
        "print_files": os.path.join(base, "print"),
    }


def ensure_space(space_id=DEFAULT_SPACE_ID, data_dir=None):
    """建好目录（库文件由各 store 自己建）。返回 paths。"""
    paths = space_paths(space_id, data_dir=data_dir)
    os.makedirs(paths["dir"], exist_ok=True)
    os.makedirs(paths["print_files"], exist_ok=True)
    return paths


def list_spaces(data_dir=None):
    """现有空间：默认空间 + `<DATA_DIR>/spaces/` 下的目录。"""
    root = DATA_DIR if data_dir is None else data_dir
    spaces = [DEFAULT_SPACE_ID]
    folder = os.path.join(root, "spaces")
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name == DEFAULT_SPACE_ID:
                continue
            try:
                spaces.append(normalize_space_id(name))
            except ValueError:
                continue
    return spaces
