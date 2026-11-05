"""Check the native URL boundary without opening any provider application."""

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / "MacApp.swift").read_text()
    validator = source[source.index("func existingChatURL("):source.index("protocol AgentBoardLoginService")]
    local = "local_11111111-1111-4111-8111-111111111111"
    cases = [
        ("codex://threads/fixture-chat_123", "codex", True),
        ("codex://threads/new-thread", "codex", True),
        ("claude://code/continue?session=" + local, "claude", True),
        ("claude://code/project/chan_Fixture123", "claude", True),
        ("claude://code/project/chan_Fixture123?thread=cmsg_Fixture123", "claude", True),
        ("codex://threads/new", "codex", False),
        ("codex://threads/chat?prompt=hello", "codex", False),
        ("codex://threads/chat#fragment", "codex", False),
        ("codex://threads/chat/other", "codex", False),
        ("codex://user@threads/chat", "codex", False),
        ("codex://threads:123/chat", "codex", False),
        ("codex://threads/fixture%2Fchat", "codex", False),
        ("codex://threads/fixture\n", "codex", False),
        ("claude://code/continue?session=" + local + "&prompt=hello", "claude", False),
        ("claude://code/continue?session=" + local + "&session=" + local, "claude", False),
        ("claude://code/continue?session=" + local + "#fragment", "claude", False),
        ("claude://code/project/chan_Fixture123?thread=cmsg_Fixture123&prompt=hello", "claude", False),
        ("claude://code/project/chan_Fixture123?thread=cmsg_Fixture123&thread=cmsg_Fixture456", "claude", False),
        ("claude://code/project/chan_Fixture123/other", "claude", False),
        ("claude://code/continue?session=" + local, "codex", False),
        ("https://example.com", "claude", False),
        ("file:///etc/passwd", "claude", False),
        ("codex://threads/chat", "unknown", False),
        ("codex://threads/" + "a" * 1001, "codex", False),
    ]
    assertions = []
    for index, (value, provider, expected) in enumerate(cases):
        assertions.append("if (existingChatURL(" + json.dumps(value) + ", provider: " + json.dumps(provider)
                          + ") != nil) != " + str(expected).lower() + ' { fatalError("URL case ' + str(index) + ' failed") }')
    qa = ROOT / ".local/app-store-local-hardening/native-chat-links"
    qa.mkdir(parents=True, exist_ok=True)
    swift = qa / "main.swift"
    swift.write_text("import Foundation\n" + validator + "\n".join(assertions) + '\nprint("PASS: 24 native existing-chat URL cases, no application opened")\n')
    binary = qa / "native-chat-links"
    subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(ROOT / "build/swift-cache"), str(swift), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
