"""仅识别有限常见单行执行方式中的受管进程命令，不解析未知复杂结构。"""

from __future__ import annotations

import re

PROCESS_COMMAND_KEYWORDS = (
    "java", "javaw", "python", "py", "pyw", "node", "npm", "npx", "pnpm", "yarn",
    "mvn", "mvnw", "maven", "gradle", "gradlew", "go", "cargo", "dotnet",
    "docker", "podman", "adb", "ffmpeg", "deno", "bun", "php", "ruby", "perl",
)
EXECUTABLE_EXTENSIONS = (".exe", ".bat", ".cmd", ".com")
LINE_WRAPPER_COMMANDS = frozenset({"cmd", "powershell", "pwsh", "bash", "sh"})
UNSUPPORTED_CONTROL_WORDS = frozenset({
    "if", "then", "do", "else", "elif", "fi", "for", "while", "until", "case",
    "esac", "done", "function", "foreach", "switch", "try", "catch",
})
MAX_PARSE_DEPTH = 3
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_VERSION_SUFFIX_PATTERN = re.compile(r"\d+(\.\d+)*")
_PARSE_FAILED = object()


def matched_process_keyword(command_text: str) -> str | None:
    """识别命令文本中处于命令位置的受管进程关键词。"""
    if not isinstance(command_text, str):
        return None
    command_text = command_text.strip()
    if not command_text:
        return None
    if any(character in command_text for character in "\n\r\0"):
        return None
    result = _match_command_line(command_text, 0)
    if result is _PARSE_FAILED:
        return None
    return result


def _split_segments(line: str) -> list[str] | None:
    """拆分支持的单行命令分隔段，并拒绝未知 shell 结构。"""
    segments: list[str] = []
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
        if char == "\\" and i + 1 < len(line) and line[i + 1] in "'\"\\$`&|;(){}<> \t":
            buffer.extend((char, line[i + 1]))
            i += 2
            continue
        if char in "'\"":
            quote = char
            buffer.append(char)
            i += 1
            continue
        if char in "`$<(){}^#":
            return None
        if char in "\n\r":
            return None
        if char == "&":
            if i + 1 >= len(line) or line[i + 1] != "&":
                return None
            segments.append("".join(buffer).strip())
            buffer.clear()
            i += 2
            continue
        if char == "|":
            segments.append("".join(buffer).strip())
            buffer.clear()
            i += 2 if i + 1 < len(line) and line[i + 1] == "|" else 1
            continue
        if char == ";":
            segments.append("".join(buffer).strip())
            buffer.clear()
            i += 1
            continue
        buffer.append(char)
        i += 1
    if quote is not None:
        return None
    segments.append("".join(buffer).strip())
    if any(not segment for segment in segments):
        return None
    return segments


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
    """解析整条支持的单行命令，并在所有命令段确认后返回首个命中。"""
    if depth > MAX_PARSE_DEPTH:
        return _PARSE_FAILED
    segments = _split_segments(line)
    if segments is None:
        return _PARSE_FAILED
    first_match: str | None = None
    for segment in segments:
        result = _match_segment(segment, depth)
        if result is _PARSE_FAILED:
            return _PARSE_FAILED
        if result is not None and first_match is None:
            first_match = result
    return first_match


def _match_segment(segment: str, depth: int) -> str | None | object:
    """检查命令段中的命令位置并解析已知 shell 包装器。"""
    tokens = _tokenize_segment(segment)
    if tokens is None or not tokens:
        return _PARSE_FAILED
    i = 0
    while i < len(tokens) and _ENV_ASSIGNMENT_PATTERN.match(tokens[i][0]):
        i += 1
    if i >= len(tokens):
        return _PARSE_FAILED
    value, _quoted = tokens[i]
    normalized = _normalize_command_word(value)
    if normalized in UNSUPPORTED_CONTROL_WORDS:
        return _PARSE_FAILED
    keyword = _is_process_command_word(value)
    if keyword is not None:
        return keyword
    if normalized not in LINE_WRAPPER_COMMANDS:
        return None
    if i + 2 >= len(tokens):
        return _PARSE_FAILED
    flag_value, flag_quoted = tokens[i + 1]
    allowed_flags = {
        "bash": {"-c"},
        "sh": {"-c"},
        "cmd": {"/c", "/k"},
        "powershell": {"-command"},
        "pwsh": {"-command"},
    }
    if flag_quoted or flag_value.lower() not in allowed_flags[normalized]:
        return _PARSE_FAILED
    script_value, script_quoted = tokens[i + 2]
    if script_quoted:
        if i + 2 != len(tokens) - 1:
            return _PARSE_FAILED
        return _match_command_line(script_value, depth + 1)
    wrapper_line = re.fullmatch(
        r"\s*\S+\s+(?:/c|/k|-c|-command)\s+(.+)",
        segment,
        re.IGNORECASE,
    )
    if wrapper_line is None:
        return _PARSE_FAILED
    return _match_command_line(wrapper_line.group(1), depth + 1)

