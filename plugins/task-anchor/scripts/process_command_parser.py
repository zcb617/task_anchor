"""进程命令词解析器：识别命令行中真正处于命令位置的进程关键字。"""

from __future__ import annotations

import re

PROCESS_COMMAND_KEYWORDS = (
    "java", "javaw", "python", "py", "pyw", "node", "npm", "npx", "pnpm", "yarn",
    "mvn", "mvnw", "maven", "gradle", "gradlew", "go", "cargo", "dotnet",
    "docker", "podman", "adb", "ffmpeg", "deno", "bun", "php", "ruby", "perl",
)
EXECUTABLE_EXTENSIONS = (".exe", ".bat", ".cmd", ".com")
LINE_WRAPPER_COMMANDS = frozenset({"cmd", "powershell", "pwsh", "bash", "sh"})
PREFIX_WRAPPER_COMMANDS = frozenset({"sudo", "nohup", "time", "env", "xargs", "call", "start"})
CONTROL_KEYWORDS = frozenset({"if", "then", "do", "else", "elif"})
SCRIPT_FLAGS = frozenset({"/c", "/k", "-c", "-command"})
MAX_PARSE_DEPTH = 3
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_VERSION_SUFFIX_PATTERN = re.compile(r"\d+(\.\d+)*")
_PARSE_FAILED = object()


def matched_process_keyword(command_text: str) -> str | None:
    """识别命令文本中处于命令位置的受管进程关键词。"""
    if not isinstance(command_text, str) or not command_text.strip():
        return None
    result = _match_command_line(command_text, 0)
    if result is _PARSE_FAILED:
        return _substring_fallback(command_text)
    return result


def _substring_fallback(command_text: str) -> str | None:
    """在命令行解析失败时按旧规则保守匹配进程关键词。"""
    normalized = command_text.lower()
    return next(
        (keyword for keyword in PROCESS_COMMAND_KEYWORDS if keyword in normalized),
        None,
    )


def _split_segments(line: str) -> tuple[list[str], list[str]] | None:
    """拆分 shell 命令段并提取命令替换中的嵌套文本。"""
    segments: list[str] = []
    nested: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(line):
        char = line[i]
        if quote == "'":
            buffer.append(char)
            if char == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if char == "\\" and i + 1 < len(line) and line[i + 1] in '"\\$`':
                buffer.extend((char, line[i + 1]))
                i += 2
                continue
            if char == '"':
                quote = None
            buffer.append(char)
            i += 1
            continue
        if char == "\\" and i + 1 < len(line) and line[i + 1] in "'\"\\$`":
            buffer.extend((char, line[i + 1]))
            i += 2
            continue
        if char in "'\"":
            quote = char
            buffer.append(char)
            i += 1
            continue
        if char == "`":
            end = line.find("`", i + 1)
            if end == -1:
                return None
            inner = line[i + 1 : end].strip()
            if inner:
                nested.append(inner)
            buffer.append(" ")
            i = end + 1
            continue
        if char == "$" and i + 1 < len(line) and line[i + 1] == "(":
            depth_p = 1
            inner_quote: str | None = None
            j = i + 2
            while j < len(line) and depth_p > 0:
                inner_char = line[j]
                if inner_quote == "'":
                    if inner_char == "'":
                        inner_quote = None
                    j += 1
                    continue
                if inner_quote == '"':
                    if inner_char == "\\" and j + 1 < len(line):
                        j += 2
                        continue
                    if inner_char == '"':
                        inner_quote = None
                    j += 1
                    continue
                if inner_char in "'\"":
                    inner_quote = inner_char
                elif inner_char == "(":
                    depth_p += 1
                elif inner_char == ")":
                    depth_p -= 1
                j += 1
            if depth_p > 0:
                return None
            inner = line[i + 2 : j - 1].strip()
            if inner:
                nested.append(inner)
            buffer.append(" ")
            i = j
            continue
        if char == "&":
            if i + 1 < len(line) and line[i + 1] == "&":
                i += 1
            segments.append("".join(buffer))
            buffer.clear()
            i += 1
            continue
        if char == "|":
            if i + 1 < len(line) and line[i + 1] == "|":
                i += 1
            segments.append("".join(buffer))
            buffer.clear()
            i += 1
            continue
        if char in ";\n\r":
            segments.append("".join(buffer))
            buffer.clear()
            i += 1
            continue
        buffer.append(char)
        i += 1
    if quote is not None:
        return None
    segments.append("".join(buffer))
    return segments, nested


def _tokenize_segment(segment: str) -> list[tuple[str, bool]] | None:
    """将命令段按空白切成保留引号状态的参数 token。"""
    tokens: list[tuple[str, bool]] = []
    value: list[str] = []
    quote: str | None = None
    quoted = False
    token_started = False
    i = 0
    while i < len(segment):
        char = segment[i]
        if quote == "'":
            if char == "'":
                quote = None
                quoted = True
                token_started = True
            else:
                value.append(char)
                token_started = True
            i += 1
            continue
        if quote == '"':
            if char == "\\" and i + 1 < len(segment) and segment[i + 1] in '"\\$`':
                value.append(segment[i + 1])
                token_started = True
                i += 2
                continue
            if char == '"':
                quote = None
                quoted = True
            else:
                value.append(char)
            token_started = True
            i += 1
            continue
        if char in " \t":
            if token_started:
                tokens.append(("".join(value), quoted))
                value.clear()
                quoted = False
                token_started = False
            i += 1
            continue
        if char == "\\" and i + 1 < len(segment) and segment[i + 1] in "'\"\\$`":
            value.append(segment[i + 1])
            token_started = True
            i += 2
            continue
        if char in "'\"":
            quote = char
            quoted = True
            token_started = True
            i += 1
            continue
        value.append(char)
        token_started = True
        i += 1
    if quote is not None:
        return None
    if token_started:
        tokens.append(("".join(value), quoted))
    return tokens


def _normalize_command_word(token_value: str) -> str:
    """归一化命令 token，去除路径和可执行文件扩展名。"""
    word = token_value.strip().lower()
    word = word.lstrip("(")
    if "/" in word or "\\" in word:
        word = re.split(r"[/\\]", word)[-1]
    for extension in EXECUTABLE_EXTENSIONS:
        if word.endswith(extension):
            word = word[: -len(extension)]
            break
    return word


def _is_process_command_word(token_value: str) -> str | None:
    """判断命令 token 是否为受管进程或带版本号的解释器。"""
    word = _normalize_command_word(token_value)
    if not word:
        return None
    if word in PROCESS_COMMAND_KEYWORDS:
        return word
    for keyword in PROCESS_COMMAND_KEYWORDS:
        suffix = word[len(keyword) :] if word.startswith(keyword) else ""
        if suffix and _VERSION_SUFFIX_PATTERN.fullmatch(suffix) is not None:
            return keyword
    return None


def _match_command_line(line: str, depth: int) -> str | None | object:
    """解析整条命令行并依次检查命令段和嵌套替换。"""
    if depth > MAX_PARSE_DEPTH:
        return None
    parsed = _split_segments(line)
    if parsed is None:
        return _PARSE_FAILED
    segments, nested = parsed
    for segment in segments:
        result = _match_segment(segment, depth)
        if result is _PARSE_FAILED:
            return _PARSE_FAILED
        if result is not None:
            return result
    for inner in nested:
        result = _match_command_line(inner, depth + 1)
        if result is _PARSE_FAILED:
            return _PARSE_FAILED
        if result is not None:
            return result
    return None


def _match_segment(segment: str, depth: int) -> str | None | object:
    """检查命令段中的命令位置并解析已知 shell 包装器。"""
    tokens = _tokenize_segment(segment)
    if tokens is None:
        return _PARSE_FAILED
    i = 0
    while i < len(tokens) and _ENV_ASSIGNMENT_PATTERN.match(tokens[i][0]):
        i += 1
    while i < len(tokens):
        value, quoted = tokens[i]
        keyword = _is_process_command_word(value)
        if keyword is not None:
            return keyword
        normalized = _normalize_command_word(value)
        if normalized in LINE_WRAPPER_COMMANDS:
            flag_index = next(
                (
                    index
                    for index in range(i + 1, len(tokens))
                    if not tokens[index][1] and tokens[index][0].lower() in SCRIPT_FLAGS
                ),
                None,
            )
            if flag_index is None or flag_index + 1 >= len(tokens):
                return None
            if normalized in {"bash", "sh"}:
                script_line = tokens[flag_index + 1][0]
            else:
                script_line = " ".join(token[0] for token in tokens[flag_index + 1 :])
            if not script_line.strip():
                return None
            return _match_command_line(script_line, depth + 1)
        if normalized in PREFIX_WRAPPER_COMMANDS:
            i += 1
            while i < len(tokens) and tokens[i][0].startswith("-") and not tokens[i][1]:
                i += 1
            if normalized == "env":
                while i < len(tokens) and _ENV_ASSIGNMENT_PATTERN.match(tokens[i][0]):
                    i += 1
            if normalized == "start" and i < len(tokens) and tokens[i][1]:
                i += 1
            continue
        if normalized in CONTROL_KEYWORDS:
            i += 1
            continue
        return None
    return None

