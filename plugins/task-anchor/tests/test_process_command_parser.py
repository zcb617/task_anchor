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
        """验证命令位置、包装器和嵌套命令中的进程均被识别。"""
        cases = (
            ("java -jar app.jar", "java"),
            ("javaw -jar app.jar", "javaw"),
            (r"C:\jdk\bin\java.exe -jar a.jar", "java"),
            ("/usr/bin/python3 manage.py runserver", "python"),
            ("python3.11 -V", "python"),
            ("dir && java -version", "java"),
            ("echo ok; npm run dev", "npm"),
            ('echo ok | python -c "pass"', "python"),
            ("cmd /c java -version", "java"),
            ('cmd /c "echo hi && java -version"', "java"),
            ('powershell -Command "java -version"', "java"),
            ('bash -c "java -jar app.jar"', "java"),
            ("sudo java -jar app.jar", "java"),
            ("nohup java -jar app.jar &", "java"),
            ("JAVA_HOME=/opt/java mvn compile", "mvn"),
            ("./gradlew build", "gradlew"),
            ("mvnw clean install", "mvnw"),
            ("echo $(java -version)", "java"),
            ("echo `java -version`", "java"),
            ("if java -version; then echo ok; fi", "java"),
            ("java -jar app.jar > log.txt", "java"),
            ('node -e "console.log(1)"', "node"),
            ("npm run dev", "npm"),
            ("bun run dev", "bun"),
            ("echo file | xargs java -jar", "java"),
            ("docker run ubuntu", "docker"),
            ("py -m unittest discover", "py"),
            ("pyw app.py", "pyw"),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(PARSER.matched_process_keyword(command), expected)

    def test_parse_failure_falls_back_to_substring(self) -> None:
        """验证解析失败时恢复旧的子串匹配结果。"""
        for command, expected in (
            ('echo "java', "java"),
            ('echo "node', "node"),
            ('echo "hello', None),
        ):
            with self.subTest(command=command):
                self.assertEqual(PARSER.matched_process_keyword(command), expected)

    def test_nested_substitution_beyond_depth_limit_is_ignored(self) -> None:
        """验证超过递归深度限制的嵌套命令不继续解析。"""
        command = "echo $(echo $(echo $(echo $(echo $(java -version)))))"
        self.assertIsNone(PARSER.matched_process_keyword(command))


if __name__ == "__main__":
    unittest.main()

