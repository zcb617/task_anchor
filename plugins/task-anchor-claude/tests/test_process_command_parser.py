from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import process_command_parser as PARSER


class ProcessCommandParserTests(unittest.TestCase):
    """验证进程命令解析器只拦截命令位置上的进程关键词。"""

    def test_plain_text_keywords_are_allowed(self) -> None:
        """验证普通参数中的进程关键词不会触发拦截。"""
        commands = (
            "echo java",
            'echo "java 是文本"',
            "echo 'java'",
            'git commit -m "fix java NPE"',
            'rg "java" .',
            "echo javascript",
            "pwd && ls",
            "cat pom.xml",
            "echo python node npm",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(PARSER.matched_process_keyword(command))

    def test_command_position_keywords_are_matched(self) -> None:
        """验证有限常见执行方式中的命令位置进程均被识别。"""
        cases = (
            ("java -jar app.jar", "java"),
            ("javaw -jar app.jar", "javaw"),
            (r"C:\jdk\bin\java.exe -jar a.jar", "java"),
            ("/usr/bin/python3 manage.py runserver", "python"),
            ("python3.11 -V", "python"),
            ("dir && java -version", "java"),
            ("echo ok; npm run dev", "npm"),
            ('echo ok | python -c "pass"', "python"),
            ("cd /work && java -jar app.jar", "java"),
            ("cmd /c java -version", "java"),
            ('cmd /c "echo hi && java -version"', "java"),
            ('powershell -Command "java -version"', "java"),
            ('pwsh -Command "node app.js"', "node"),
            ('bash -c "java -jar app.jar"', "java"),
            ("JAVA_HOME=/opt/java mvn compile", "mvn"),
            ("./gradlew build", "gradlew"),
            ("mvnw clean install", "mvnw"),
            ("java -jar app.jar > log.txt", "java"),
            ('node -e "console.log(1)"', "node"),
            ("npm run dev", "npm"),
            ("bun run dev", "bun"),
            ("docker run ubuntu", "docker"),
            ("py -m unittest discover", "py"),
            ("pyw app.py", "pyw"),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(PARSER.matched_process_keyword(command), expected)

    def test_parse_failure_is_allowed(self) -> None:
        """验证解析失败的命令不会回退到全文关键词匹配。"""
        for command in ('echo "java', 'echo "node', 'echo "hello', 'java -jar "broken'):
            with self.subTest(command=command):
                self.assertIsNone(PARSER.matched_process_keyword(command))

    def test_command_substitution_is_not_in_supported_forms(self) -> None:
        """验证命令替换和深层嵌套不属于支持的执行形式。"""
        self.assertIsNone(PARSER.matched_process_keyword("echo $(java -version)"))
        self.assertIsNone(PARSER.matched_process_keyword("echo `java -version`"))
        command = "echo $(echo $(echo $(echo $(echo $(java -version)))))"
        self.assertIsNone(PARSER.matched_process_keyword(command))

    def test_unsupported_scripts_are_allowed(self) -> None:
        """验证未知包装器、控制脚本和多行脚本均放行。"""
        commands = (
            "sudo java -jar app.jar",
            "nohup java -jar app.jar &",
            "if java -version; then echo ok; fi",
            "echo file | xargs java -jar",
            "bash -unknown java",
            "custom_runner java -jar app.jar",
            "echo ok\njava -version",
            "powershell -File java.ps1",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(PARSER.matched_process_keyword(command))

    def test_literal_file_content_is_allowed(self) -> None:
        """验证文档内容、参数文本和转义分隔符中的进程词不会触发拦截。"""
        commands = (
            'cat > demo.md <<\'EOF\'\n```java\nSystem.out.println("hello");\n```\nEOF',
            "cat > demo.md <<'EOF'\njava -jar app.jar\nEOF",
            'printf "%s" "java -jar app.jar" > demo.md',
            "echo Foo.java",
            'rg "node|java" .',
            r"echo \;java",
            r"echo \&\&java",
            "echo java # ; node app.js",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(PARSER.matched_process_keyword(command))

    def test_wrappers_preserve_literal_arguments(self) -> None:
        """验证包装器脚本文本中的普通参数不会被误识别为进程命令。"""
        for command in (
            'cmd /c echo "hi && java -version"',
            "bash -c 'echo \"java -version\"'",
            "powershell -Command \"Write-Output 'java'\"",
        ):
            with self.subTest(command=command):
                self.assertIsNone(PARSER.matched_process_keyword(command))
        self.assertEqual(
            PARSER.matched_process_keyword('cmd /c "echo hi && java -version"'),
            "java",
        )

    def test_known_commands_do_not_override_parse_failure(self) -> None:
        """验证同一命令中的已知命中不会覆盖后续解析失败。"""
        commands = (
            'java -version; echo "broken',
            "java -version &&",
            "java -version; if true",
            "java -version\ncat text",
            "java -version && echo $(node app.js)",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(PARSER.matched_process_keyword(command))
        command = "java -version"
        for _ in range(PARSER.MAX_PARSE_DEPTH + 2):
            command = f"bash -c {command!r}"
        self.assertIsNone(PARSER.matched_process_keyword(command))


if __name__ == "__main__":
    unittest.main()

